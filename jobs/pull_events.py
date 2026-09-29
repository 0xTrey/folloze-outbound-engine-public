"""
Job: pull_events

Polls Smartlead campaigns for reply and bounce events.
Classifies replies using LLMGateway. Updates:
  - sends table (reply_type, delivery_status)
  - suppressions table (unsubscribes and hard bounces)

Runs on a 15-30 minute cron schedule.
No Smartlead SmartAgents required — all classification lives here.

Reply types:
  interested   — positive reply, wants to learn more or book a meeting
  objection    — pushback: not interested, wrong timing, wrong budget, wrong person
  ooo          — out-of-office auto-reply
  unsubscribe  — explicit request to stop contact
  referral     — forwarded to a different contact or suggested someone else
  not_relevant — spam-like, phishing-like, or truly unclassifiable
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import psycopg2.extras

from connectors.crm_client import CRMClient
from connectors.smartlead_client import SmartleadClient
from db.connection import db_conn
from llm_gateway import LLMGateway
from runtime_config import notify

VALID_REPLY_TYPES = frozenset({
    "interested", "objection", "ooo", "unsubscribe", "referral", "not_relevant"
})

# Smartlead email_status values that have something to process
ACTIONABLE_STATUSES = frozenset({"REPLIED", "BOUNCED", "UNSUBSCRIBED"})

# Smartlead campaign statuses we actively poll
ACTIVE_CAMPAIGN_STATUSES = frozenset({"ACTIVE", "RUNNING"})
LOOP_EVENTS_FILE = Path(__file__).resolve().parent.parent / "logs" / "loop-events.jsonl"
LOOP_STATUS_MAP = {
    "interested": "interested",
    "objection": "not_now",
    "ooo": "not_now",
    "unsubscribe": "disqualified",
    "referral": "reengage_candidate",
    "not_relevant": "disqualified",
}


# ---------------------------------------------------------------------------
# Reply classification
# ---------------------------------------------------------------------------

def _classify_reply(reply_text: str, contact_name: str, company: str) -> str:
    """
    Classify an email reply using LLMGateway.
    Returns one of VALID_REPLY_TYPES. Falls back to 'not_relevant' on failure.
    """
    system = (
        "You classify cold email replies for a B2B sales team. "
        "Return ONLY a single JSON object: {\"type\": \"<classification>\"}\n\n"
        "Classifications:\n"
        "  interested   — positive: wants to learn more, book a call, or get more info\n"
        "  objection    — real engagement but negative: not interested, wrong timing, "
        "wrong budget, wrong person, already has a solution\n"
        "  ooo          — out-of-office auto-reply (contains date ranges, return date, "
        "'I am out of office', 'I will return', or auto-reply markers)\n"
        "  unsubscribe  — explicit request to unsubscribe, stop contact, remove from list\n"
        "  referral     — forwarded to someone else or suggested a different contact\n"
        "  not_relevant — spam, phishing-like, no clear intent, or impossible to classify\n\n"
        "Rules:\n"
        "- Output ONLY the JSON object. No explanation, no preamble.\n"
        "- 'Remove me from your list' = unsubscribe, not objection.\n"
        "- Genuine questions about the product = interested.\n"
        "- 'Not now / maybe next quarter' = objection (not interested currently).\n"
        "- If genuinely unclear after careful reading: not_relevant."
    )
    user = (
        f"Contact: {contact_name} at {company}\n\n"
        f"Reply:\n{reply_text[:1500]}"
    )
    for profile in ("strategic", "workhorse"):
        try:
            gw = LLMGateway(profile=profile)
            raw = gw.chat([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ]).strip()
            if raw.startswith("```"):
                raw = re.sub(r"^```[a-z]*\n?", "", raw)
                raw = re.sub(r"\n?```$", "", raw.strip())
            parsed = json.loads(raw)
            result = parsed.get("type", "").lower().strip()
            if result in VALID_REPLY_TYPES:
                return result
        except Exception as e:
            print(f"  [pull_events] classify failed on profile={profile}: {e}")
    return "not_relevant"


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _lookup_contact_id(conn, email: str) -> str | None:
    with conn.cursor() as cur:
        cur.execute("SELECT contact_id FROM contacts WHERE email = %s LIMIT 1", (email,))
        row = cur.fetchone()
        return str(row[0]) if row else None


def _get_processed_lead_ids(conn, campaign_id: str) -> set[str]:
    """
    Return smartlead_lead_ids already recorded in sends for this campaign.
    A lead is 'processed' if it has a delivery_status set (i.e., not still pending).
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT smartlead_lead_id FROM sends "
            "WHERE smartlead_campaign_id = %s AND delivery_status IS NOT NULL",
            (campaign_id,),
        )
        return {str(row[0]) for row in cur.fetchall() if row[0]}


def _record_send(conn, data: dict):
    """
    Insert a new send record or update delivery_status + reply_type on existing record.
    Avoids requiring a unique constraint on the sends table.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT send_id FROM sends "
            "WHERE smartlead_campaign_id = %s AND smartlead_lead_id = %s LIMIT 1",
            (data["smartlead_campaign_id"], data["smartlead_lead_id"]),
        )
        existing = cur.fetchone()

        if existing:
            cur.execute(
                "UPDATE sends SET delivery_status = %s, reply_type = %s, bounce_type = %s "
                "WHERE send_id = %s",
                (data["delivery_status"], data.get("reply_type"), data.get("bounce_type"), str(existing[0])),
            )
        else:
            cur.execute("""
                INSERT INTO sends (
                    contact_id, smartlead_campaign_id, smartlead_lead_id,
                    mailbox, domain, step_number, sent_at,
                    delivery_status, bounce_type, reply_type
                )
                VALUES (%(contact_id)s, %(smartlead_campaign_id)s, %(smartlead_lead_id)s,
                        %(mailbox)s, %(domain)s, %(step_number)s, %(sent_at)s,
                        %(delivery_status)s, %(bounce_type)s, %(reply_type)s)
            """, data)


def _add_suppression(conn, email: str, domain: str, reason: str, source: str):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO suppressions (email, domain, reason, source) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
            (email, domain, reason, source),
        )


# Minimum seconds between polls for the same campaign (avoids double-processing
# when a manual run overlaps the 20-min LaunchAgent)
MIN_POLL_INTERVAL_SECONDS = 900  # 15 minutes


def _should_poll_campaign(conn, campaign_id: str) -> bool:
    """Return True if MIN_POLL_INTERVAL_SECONDS have elapsed since last poll."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM kv_config WHERE key = %s",
            (f"pull_events_polled_{campaign_id}",),
        )
        row = cur.fetchone()
    if not row:
        return True
    try:
        last = datetime.fromisoformat(row[0])
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - last).total_seconds() >= MIN_POLL_INTERVAL_SECONDS
    except (ValueError, TypeError):
        return True


def _mark_campaign_polled(conn, campaign_id: str):
    """Record that we just finished polling this campaign."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO kv_config (key, value)
            VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (f"pull_events_polled_{campaign_id}", datetime.now(timezone.utc).isoformat()))


def _notify_interested(lead: dict, campaign_id: str):
    """Alert Mason about an interested reply via the shared notify() path."""
    name = f"{lead.get('first_name', '')} {lead.get('last_name', '')}".strip() or "(unknown)"
    email = lead.get("email", "")
    company = lead.get("company_name", "") or lead.get("organization_name", "")

    message = "\n".join([
        f"Interested reply from {name} <{email}> @ {company or '(unknown company)'}",
        f"Campaign: {campaign_id}",
        "Action: Review in Smartlead and respond.",
    ])

    print(f"\n[pull_events] Interested reply from {name} <{email}> — notifying Mason...")
    notify("Interested reply", message)
    print("[pull_events] Mason notified via shared notify().")


def _loop_status(email_status: str, reply_type: str | None = None) -> str:
    email_status = (email_status or "").upper()
    if email_status in {"BOUNCED", "UNSUBSCRIBED"}:
        return "disqualified"
    if reply_type:
        return LOOP_STATUS_MAP.get(reply_type, "review_pending")
    return "review_pending"


def _append_loop_event(event: dict) -> None:
    LOOP_EVENTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOOP_EVENTS_FILE, "a") as handle:
        handle.write(json.dumps(event) + "\n")


# ---------------------------------------------------------------------------
# Per-lead processing
# ---------------------------------------------------------------------------

def _process_lead(
    sl_client: SmartleadClient,
    conn,
    campaign_id: str,
    lead: dict,
    dry_run: bool,
) -> str:
    """
    Process one actionable lead. Returns a short status string for logging.
    """
    lead_id = str(lead.get("id") or lead.get("lead_id") or "")
    email = lead.get("email") or ""
    domain = email.split("@")[-1] if "@" in email else ""
    first = lead.get("first_name") or ""
    last = lead.get("last_name") or ""
    company = lead.get("company_name") or lead.get("organization_name") or ""
    email_status = (lead.get("email_status") or "").upper()
    mailbox = lead.get("sender_email") or lead.get("from_email") or ""

    base_send = {
        "smartlead_campaign_id": campaign_id,
        "smartlead_lead_id": lead_id,
        "mailbox": mailbox,
        "domain": domain,
        "step_number": lead.get("sent_count") or 1,
        "sent_at": datetime.now(timezone.utc),
    }

    # ---- BOUNCED --------------------------------------------------------
    if email_status == "BOUNCED":
        if dry_run:
            return "would:bounced→suppress"
        contact_id = _lookup_contact_id(conn, email)
        _record_send(conn, {
            **base_send,
            "contact_id": contact_id,
            "delivery_status": "bounced",
            "bounce_type": "hard",
            "reply_type": None,
        })
        _add_suppression(conn, email, domain, "bounce", "smartlead")
        _append_loop_event({
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "campaign_id": campaign_id,
            "lead_id": lead_id,
            "email": email,
            "company": company,
            "event_type": "bounce",
            "reply_type": None,
            "loop_status": _loop_status(email_status),
        })
        return "bounced→suppressed"

    # ---- UNSUBSCRIBED ---------------------------------------------------
    if email_status == "UNSUBSCRIBED":
        if dry_run:
            return "would:unsubscribed→suppress"
        contact_id = _lookup_contact_id(conn, email)
        _record_send(conn, {
            **base_send,
            "contact_id": contact_id,
            "delivery_status": "delivered",
            "bounce_type": None,
            "reply_type": "unsubscribe",
        })
        _add_suppression(conn, email, domain, "unsubscribe", "smartlead")
        _append_loop_event({
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "campaign_id": campaign_id,
            "lead_id": lead_id,
            "email": email,
            "company": company,
            "event_type": "unsubscribe",
            "reply_type": "unsubscribe",
            "loop_status": _loop_status(email_status, "unsubscribe"),
        })
        return "unsubscribed→suppressed"

    # ---- REPLIED: get history, classify ---------------------------------
    if email_status == "REPLIED":
        try:
            history = sl_client.get_lead_message_history(campaign_id, lead_id)
        except Exception as e:
            print(f"  [pull_events] message history failed for {email}: {e}")
            return "error:history_fetch"

        # Find the prospect's reply (not our outbound messages)
        reply_text = ""
        for msg in reversed(history or []):
            msg_type = (msg.get("type") or "").upper()
            msg_from = msg.get("from") or msg.get("sender") or ""
            if msg_type == "REPLY" or (email and email.lower() in msg_from.lower()):
                reply_text = (msg.get("message") or msg.get("body") or "").strip()
                if reply_text:
                    break

        if not reply_text:
            print(f"  [pull_events] reply text not found for {email} (lead_id={lead_id})")
            return "skipped:no_reply_text"

        reply_type = _classify_reply(reply_text, f"{first} {last}".strip(), company)

        if dry_run:
            return f"would:replied→{reply_type}"

        contact_id = _lookup_contact_id(conn, email)
        _record_send(conn, {
            **base_send,
            "contact_id": contact_id,
            "delivery_status": "delivered",
            "bounce_type": None,
            "reply_type": reply_type,
        })

        if reply_type == "unsubscribe":
            _add_suppression(conn, email, domain, "unsubscribe", "smartlead")

        crm_result = None
        if reply_type == "interested":
            _notify_interested(lead, campaign_id)
            try:
                crm_result = CRMClient().record_interested_reply(
                    email=email,
                    name=f"{first} {last}".strip(),
                    company=company,
                    reply_snippet=reply_text[:500],
                    campaign_id=campaign_id,
                )
                print(
                    "[pull_events] Salesforce write-back complete: "
                    f"{crm_result.get('action')} {crm_result.get('record_id')}"
                )
            except Exception as exc:
                crm_result = {"action": "crm_error", "error": str(exc)}
                print(f"[pull_events] Salesforce write-back failed: {exc}")

        _append_loop_event({
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "campaign_id": campaign_id,
            "lead_id": lead_id,
            "email": email,
            "company": company,
            "event_type": "reply",
            "reply_type": reply_type,
            "loop_status": _loop_status(email_status, reply_type),
            "crm_result": crm_result,
        })

        return f"replied→{reply_type}"

    return "skipped:status_not_actionable"


# ---------------------------------------------------------------------------
# Job entry point
# ---------------------------------------------------------------------------

def run(dry_run: bool = False):
    print(f"[pull_events] Starting. dry_run={dry_run}")

    sl = SmartleadClient()

    total = 0
    by_type: dict[str, int] = {}

    with db_conn() as conn:
        campaigns = sl.list_campaigns()
        active = [
            c for c in campaigns
            if (c.get("status") or "").upper() in ACTIVE_CAMPAIGN_STATUSES
        ]
        print(f"[pull_events] {len(active)} active campaigns")

        for campaign in active:
            campaign_id = str(campaign.get("id") or campaign.get("campaign_id") or "")
            campaign_name = campaign.get("name") or campaign_id
            if not campaign_id:
                continue

            # Skip if polled recently (prevents double-processing on cron/manual overlap)
            if not dry_run and not _should_poll_campaign(conn, campaign_id):
                print(f"  '{campaign_name}': polled recently, skipping")
                continue

            # Paginate all leads
            all_leads = []
            offset = 0
            while True:
                batch = sl.get_leads(campaign_id, offset=offset, limit=100)
                if not batch:
                    break
                all_leads.extend(batch)
                if len(batch) < 100:
                    break
                offset += 100

            # Filter to actionable statuses
            actionable = [
                l for l in all_leads
                if (l.get("email_status") or "").upper() in ACTIONABLE_STATUSES
            ]
            if not actionable:
                continue

            # Skip leads we've already processed (dedup by smartlead_lead_id)
            if not dry_run:
                processed = _get_processed_lead_ids(conn, campaign_id)
                to_process = [
                    l for l in actionable
                    if str(l.get("id") or l.get("lead_id") or "") not in processed
                ]
            else:
                to_process = actionable

            if not to_process:
                if not dry_run:
                    _mark_campaign_polled(conn, campaign_id)
                    conn.commit()
                continue

            print(f"  '{campaign_name}': {len(to_process)} new events")

            for lead in to_process:
                status = _process_lead(sl, conn, campaign_id, lead, dry_run)
                total += 1
                by_type[status] = by_type.get(status, 0) + 1
                email = lead.get("email") or ""
                print(f"    {email} → {status}")

            if not dry_run:
                _mark_campaign_polled(conn, campaign_id)
                conn.commit()

    print(f"\n[pull_events] Done. total={total}")
    for status, count in sorted(by_type.items()):
        print(f"  {status}: {count}")
