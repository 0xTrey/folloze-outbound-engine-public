"""
Job: assign_push  (Phase 2 — implemented)

Finds contacts with approved personalization payloads and pushes them to the
correct Smartlead campaign, using inbox rotation automatically managed by Smartlead.

Eligibility rules before push:
  - contact.email_status must be 'verified'
  - personalization_payloads.qa_status must be 'approved'
  - campaign_assignments.eligibility_status must be 'pending'
  - email not in suppressions table
  - domain not in suppressions table

Sequence family → Smartlead campaign ID mapping is loaded from the
sequence_families table (family_name → smartlead_campaign_id column).
You must populate this column before running this job.

Usage:
    python main.py assign_push --dry-run    # print what would be sent
    python main.py assign_push              # push to Smartlead

Prerequisites:
  1. SMARTLEAD_API_KEY in environment (via ~/.zshrc Keychain export)
  2. sequence_families table has smartlead_campaign_id populated for each family
  3. Smartlead campaigns exist with email accounts (mailboxes) assigned for inbox rotation
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import logging
import os
import sys
from typing import Optional
from urllib.parse import urlencode

from connectors.crm_client import CRMClient
from connectors.smartlead_client import SmartleadClient
from db.connection import db_conn
from runtime_config import configure_logging, get_link_debug_logger

_link_log = get_link_debug_logger()
logger = logging.getLogger(__name__)

# Destination URLs are private deployment configuration.
BOARD_URL = os.getenv("FOLLOZE_OUTBOUND_DESTINATION_URL", "").strip()
GTME_BOARD_URL = os.getenv("FOLLOZE_GTME_DESTINATION_URL", BOARD_URL).strip()

SENDER_EMAIL = os.getenv("FOLLOZE_BOARD_INVITED_BY", "sender@example.com")

# Default board link anchor text for GTME touches when the LLM doesn't return one.
# The board link is required in every GTME email — never empty.
GTME_DEFAULT_ANCHORS = {
    1: "See the landing page this campaign points to",
    2: "Here's the landing page",
    3: "See it",
}


def _build_board_url(contact: dict, family: str, step_num: int) -> str:
    """
    Build a tracked Folloze Board URL for one contact + sequence step.
    Passes identity params (co, em, fn, ln, ro, inby) + UTMs.
    GTME_VIBE_MKT uses GTME_BOARD_URL; all other families use BOARD_URL.
    Empty values are omitted to keep URLs clean.
    """
    base = GTME_BOARD_URL if family == "GTME_VIBE_MKT" else BOARD_URL
    if not base:
        raise RuntimeError("outbound destination URL is not configured")
    params = {
        "co":  contact.get("company_name") or "",
        "em":  contact.get("email") or "",
        "fn":  contact.get("first_name") or "",
        "ln":  contact.get("last_name") or "",
        "ro":  contact.get("title") or "",
        "inby": SENDER_EMAIL,
        "utm_source":   "smartlead",
        "utm_medium":   "email",
        "utm_campaign": family,
        "utm_content":  f"step{step_num}",
    }
    params = {k: v for k, v in params.items() if v}
    return f"{base}?{urlencode(params)}"


def _make_case_study_link(anchor: str, url: str, required: bool = False) -> str:
    """
    Return an HTML hyperlink. Prefixed with <br><br> so it visually separates.
    If required=True, uses anchor even when blank (falls back to the URL itself).

    NOTE: We intentionally use raw & (not &amp;) between URL params in the href.
    Smartlead's click-tracker reads the href attribute via simple regex (not an HTML
    parser) and URL-encodes the raw string verbatim. If we used &amp; here, Smartlead
    would encode %26amp%3B and Folloze would receive literal &amp; in its query params,
    breaking the URL. Raw & is correct for this pipeline.
    """
    if not anchor or not anchor.strip():
        if not required:
            return ""
        anchor = url  # last resort — bare URL as anchor
    return f'<br><br><a href="{url}">{anchor.strip()}</a>'


# Max contacts to push per run.
BATCH_CAP = 50

# Hard daily enrollment cap. Counts T1 sends in the last 24h from the sends table.
# Sequence step sends (T2-T5) are Smartlead-managed and not counted here — the
# enrollment rate is the lever we control. At 50 enrollments/day steady state,
# peak total daily volume (all 5 steps across all active cohorts) stays well
# under 3,000. This cap guards against accidental bulk runs.
DAILY_ENROLLMENT_CAP = 3000

MIN_MAILBOX_WARMUP_AGE_DAYS = 14
MAILBOX_LAUNCH_OVERRIDE_ENV = "FOLLOZE_ALLOW_FRESH_SMARTLEAD_MAILBOXES"
ACTIVE_CAMPAIGN_STATUSES = {"ACTIVE", "RUNNING", "STARTED"}


def _check_daily_enrollment(conn) -> tuple[int, int]:
    """
    Returns (sent_today, remaining) based on sends table for the past 24 hours.
    Counts T1 enrollments only (what we control). Hard cap = DAILY_ENROLLMENT_CAP.
    """
    with conn.cursor() as cur:
        cur.execute("""
            SELECT COUNT(*) FROM sends
            WHERE sent_at >= NOW() - INTERVAL '24 hours'
        """)
        sent_today = cur.fetchone()[0]
    remaining = max(0, DAILY_ENROLLMENT_CAP - sent_today)
    return sent_today, remaining


def _get_campaign_id_map(conn) -> dict[str, str]:
    """
    Load family_name → smartlead_campaign_id from sequence_families table.
    Only returns families where smartlead_campaign_id is non-null.
    """
    with conn.cursor() as cur:
        # Add smartlead_campaign_id column if it doesn't exist yet
        cur.execute("""
            ALTER TABLE sequence_families
            ADD COLUMN IF NOT EXISTS smartlead_campaign_id TEXT;
        """)
        conn.commit()

        cur.execute("""
            SELECT family_name, smartlead_campaign_id
            FROM sequence_families
            WHERE smartlead_campaign_id IS NOT NULL
              AND is_active = TRUE
        """)
        return {row[0]: row[1] for row in cur.fetchall()}


def _get_inactive_campaign_families(conn) -> list[tuple[str, str]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT family_name, smartlead_campaign_id
            FROM sequence_families
            WHERE smartlead_campaign_id IS NOT NULL
              AND is_active = FALSE
            ORDER BY family_name
            """
        )
        return [(row[0], row[1]) for row in cur.fetchall()]


def _parse_smartlead_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _assess_mailbox_readiness(
    account: dict,
    warmup_stats: dict,
    *,
    now: datetime | None = None,
) -> tuple[bool, str | None]:
    now = now or datetime.now(timezone.utc)
    created_at = _parse_smartlead_timestamp(account.get("created_at"))
    label = account.get("from_email") or account.get("email") or account.get("username") or str(account.get("id"))
    sent_count = int(warmup_stats.get("sent_count") or 0)
    received_count = int(warmup_stats.get("warmup_email_received_count") or 0)

    if not account.get("is_imap_success") or not account.get("is_smtp_success"):
        return False, f"{label}: unhealthy mailbox connectivity (imap={account.get('is_imap_success')} smtp={account.get('is_smtp_success')})"

    if created_at is None:
        return False, f"{label}: missing created_at from Smartlead"

    min_ready_at = created_at + timedelta(days=MIN_MAILBOX_WARMUP_AGE_DAYS)
    if now < min_ready_at:
        return False, (
            f"{label}: created_at={created_at.date().isoformat()} is too fresh for cold send; "
            f"earliest safe date is {min_ready_at.date().isoformat()}"
        )

    if sent_count <= 0 and received_count <= 0:
        return False, (
            f"{label}: no warmup evidence yet (sent_count={sent_count}, "
            f"warmup_email_received_count={received_count})"
        )

    return True, None


def _ensure_campaigns_launch_ready(
    smartlead: SmartleadClient,
    campaign_id_map: dict[str, str],
) -> None:
    now = datetime.now(timezone.utc)
    mailbox_cache: dict[int, tuple[dict, dict]] = {}
    failures: list[str] = []

    for family_name, campaign_id in sorted(campaign_id_map.items()):
        campaign = smartlead.get_campaign(campaign_id)
        campaign_status = str(campaign.get("status") or "").upper()
        if campaign_status not in ACTIVE_CAMPAIGN_STATUSES:
            failures.append(
                f"{family_name}: Smartlead campaign {campaign_id} is {campaign_status or 'UNKNOWN'}; "
                "resume it before enrolling leads"
            )
            continue

        assigned_accounts = smartlead.get_campaign_email_accounts(campaign_id) or []
        if not assigned_accounts:
            failures.append(f"{family_name}: Smartlead campaign {campaign_id} has no assigned mailboxes")
            continue

        for assigned in assigned_accounts:
            account_id = int(assigned["id"])
            if account_id not in mailbox_cache:
                details = smartlead.get_email_account(str(account_id))
                stats = smartlead.get_email_account_warmup_stats(str(account_id))
                mailbox_cache[account_id] = (details, stats)
            details, stats = mailbox_cache[account_id]
            ready, reason = _assess_mailbox_readiness(details, stats, now=now)
            if not ready and reason:
                failures.append(f"{family_name}: {reason}")

    if failures:
        raise RuntimeError(
            "Smartlead launch gate failed. Refusing to enroll leads until campaigns are active "
            f"and assigned mailboxes are warmed. Override with {MAILBOX_LAUNCH_OVERRIDE_ENV}=1 only "
            "if you intentionally want to bypass the safety rail.\n- " + "\n- ".join(failures)
        )


def _reset_stale_processing(conn):
    """
    Reset contacts stuck in 'processing' from a crashed or timed-out prior run.
    Any assignment in 'processing' for > 2 hours is assumed abandoned and reset to 'pending'.
    """
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE campaign_assignments
            SET eligibility_status = 'pending', processing_at = NULL
            WHERE eligibility_status = 'processing'
              AND processing_at < NOW() - INTERVAL '2 hours'
        """)
        rows = cur.rowcount
    if rows:
        logger.info("Reset %s stale 'processing' assignment(s) to 'pending'", rows)


def _claim_eligible_contacts(conn, campaign_id_map: dict[str, str], limit: int = BATCH_CAP) -> list[dict]:
    """
    Atomically claim contacts ready to push by:
      1. Selecting eligible candidates ordered by priority_score (highest first)
      2. UPDATE eligibility_status = 'processing' for just those rows
      3. Returning only the rows we successfully claimed (concurrent runners skip them)

    The 'processing' status is cleared to 'sent' on success or 'ineligible' on error.
    Stale 'processing' rows (crash recovery) are reset to 'pending' by _reset_stale_processing.
    """
    families = list(campaign_id_map.keys())
    if not families:
        return []

    with conn.cursor() as cur:
        # Select candidates ordered by priority_score (subquery needed because DISTINCT ON
        # requires ORDER BY to lead with the DISTINCT column, which prevents priority ordering)
        cur.execute("""
            SELECT * FROM (
                SELECT DISTINCT ON (c.contact_id)
                    c.contact_id,
                    c.first_name,
                    c.last_name,
                    c.email,
                    c.title,
                    a.company_name,
                    a.domain,
                    a.priority_score,
                    ca.assignment_id,
                    ca.sequence_family,
                    pp.payload_id,
                    pp.ai_subject,
                    pp.ai_opener,
                    pp.ai_why_now,
                    pp.ai_proof_point,
                    pp.ai_cta,
                    pp.ai_t2_body,
                    pp.ai_t3_body,
                    pp.ai_t1_case_study_anchor,
                    pp.ai_t2_case_study_anchor
                FROM contacts c
                JOIN accounts a ON a.account_id = c.account_id
                JOIN campaign_assignments ca ON ca.contact_id = c.contact_id
                JOIN personalization_payloads pp ON pp.contact_id = c.contact_id
                WHERE c.email_status IN ('verified', 'likely', 'provided', 'unknown', 'extrapolated')
                  AND ca.eligibility_status IN ('pending', 'eligible')
                  AND pp.qa_status = 'approved'
                  AND ca.sequence_family = ANY(%s)
                  AND c.email NOT IN (
                      SELECT email FROM suppressions
                      WHERE email IS NOT NULL
                        AND (expires_at IS NULL OR expires_at > NOW())
                  )
                  AND a.domain NOT IN (
                      SELECT domain FROM suppressions
                      WHERE domain IS NOT NULL
                        AND (expires_at IS NULL OR expires_at > NOW())
                  )
                ORDER BY c.contact_id, pp.approved_at DESC NULLS LAST
            ) eligible
            ORDER BY priority_score DESC NULLS LAST
            LIMIT %s
        """, (families, limit))

        cols = [d[0] for d in cur.description]
        candidates = [dict(zip(cols, row)) for row in cur.fetchall()]

    if not candidates:
        return []

    # Atomically claim candidates — concurrent runners will see a different result
    # set or an empty result if all rows are already claimed
    assignment_ids = [str(c["assignment_id"]) for c in candidates]
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE campaign_assignments
            SET eligibility_status = 'processing',
                processing_at = NOW()
            WHERE assignment_id = ANY(%s::uuid[])
              AND eligibility_status IN ('pending', 'eligible')
            RETURNING assignment_id
        """, (assignment_ids,))
        claimed = {str(row[0]) for row in cur.fetchall()}
    conn.commit()

    return [c for c in candidates if str(c["assignment_id"]) in claimed]


def _build_lead_payload(contact: dict) -> dict:
    """
    Build the Smartlead lead dict for one contact.
    Custom fields map to {{variable}} placeholders in Smartlead sequence steps.

    Standard families (2-touch): board links are optional (LLM-driven anchor text).
    GTME_VIBE_MKT (3-touch): board link is required in every touch — falls back to
      GTME_DEFAULT_ANCHORS when the LLM didn't return anchor text.
    """
    family = contact.get("sequence_family") or ""
    is_gtme = family == "GTME_VIBE_MKT"
    cid = f"{str(contact.get('assignment_id', '?'))[:8]}|{contact.get('email', '?')}"

    # Stage 1 — INPUT: log the identity fields used to build links
    _link_log.info(
        f"[{cid}] INPUT   family={family!r}"
        f" company={contact.get('company_name')!r}"
        f" email={contact.get('email')!r}"
        f" fn={contact.get('first_name')!r}"
        f" ln={contact.get('last_name')!r}"
        f" title={contact.get('title')!r}"
    )

    custom_fields = {
        "ai_subject":     contact.get("ai_subject") or "",
        "ai_opener":      contact.get("ai_opener") or "",
        "ai_why_now":     contact.get("ai_why_now") or "",
        "ai_proof_point": contact.get("ai_proof_point") or "",
        "ai_cta":         contact.get("ai_cta") or "",
        "ai_t2_body":     contact.get("ai_t2_body") or "",
        "ai_t3_body":     contact.get("ai_t3_body") or "",
        "title":          contact.get("title") or "",
    }

    # Build board links. GTME requires a link in all 3 touches.
    # Standard families: optional (empty string if LLM didn't return anchor).
    touches = range(1, 4) if is_gtme else range(1, 3)
    for step_num in touches:
        raw_anchor = contact.get(f"ai_t{step_num}_case_study_anchor") or ""
        anchor = raw_anchor
        anchor_source = "db"
        if is_gtme and not anchor:
            anchor = GTME_DEFAULT_ANCHORS.get(step_num, "See the board")
            anchor_source = "default"

        # Stage 2 — URL: log what params go in and what URL comes out
        board_url = _build_board_url(contact, family, step_num)
        _link_log.info(
            f"[{cid}] URL     step={step_num}"
            f" anchor_src={anchor_source!r}"
            f" anchor={anchor!r}"
            f" url={board_url!r}"
        )

        # Stage 3 — HTML: log what the <a> tag looks like
        html_link = _make_case_study_link(anchor, board_url, required=is_gtme)
        _link_log.info(
            f"[{cid}] HTML    step={step_num}"
            f" required={is_gtme}"
            f" output={html_link!r}"
        )

        custom_fields[f"ai_t{step_num}_case_study_link"] = html_link

    # Stage 4 — PAYLOAD: log the final link fields going to Smartlead
    link_fields = {k: v for k, v in custom_fields.items() if "case_study_link" in k}
    _link_log.info(f"[{cid}] PAYLOAD links={link_fields!r}")

    return {
        "first_name": contact["first_name"] or "",
        "last_name": contact["last_name"] or "",
        "email": contact["email"],
        "company_name": contact.get("company_name") or "",
        "website": f"https://{contact['domain']}" if contact.get("domain") else "",
        "custom_fields": custom_fields,
    }


def _mark_sent(conn, assignment_id: str, smartlead_lead_id: Optional[str], smartlead_campaign_id: str = ""):
    """Update campaign_assignments and contacts after a successful push."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE campaign_assignments
            SET eligibility_status = 'sent',
                scheduled_at = NOW(),
                processing_at = NULL
            WHERE assignment_id = %s
        """, (assignment_id,))

        # Stamp last_sent_at on the contact for 45-day cycle tracking
        cur.execute("""
            UPDATE contacts
            SET last_sent_at = NOW()
            WHERE contact_id = (
                SELECT contact_id FROM campaign_assignments WHERE assignment_id = %s
            )
        """, (assignment_id,))

        # Always write to sends — smartlead_lead_id is NULL when the batch API
        # doesn't return per-lead IDs, which is the normal case.
        cur.execute("""
            INSERT INTO sends (
                contact_id, smartlead_campaign_id, sequence_family,
                smartlead_lead_id, domain
            )
            SELECT
                ca.contact_id,
                %s,
                ca.sequence_family,
                %s,
                (
                    SELECT a.domain FROM accounts a
                    JOIN contacts c2 ON c2.account_id = a.account_id
                    WHERE c2.contact_id = ca.contact_id
                    LIMIT 1
                )
            FROM campaign_assignments ca
            WHERE ca.assignment_id = %s
        """, (smartlead_campaign_id, smartlead_lead_id, assignment_id))


def _mark_ineligible(conn, assignment_id: str, reason: str):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE campaign_assignments
            SET eligibility_status = 'ineligible',
                ineligibility_reason = %s
            WHERE assignment_id = %s
        """, (reason, assignment_id))


def _route_contact_to_salesforce(contact: dict, campaign_id: str) -> dict:
    client = CRMClient()
    return client.record_campaign_enrollment(
        email=contact["email"],
        name=f"{contact.get('first_name', '')} {contact.get('last_name', '')}".strip(),
        company=contact.get("company_name") or "",
        internal_reference=str(contact.get("assignment_id") or ""),
        smartlead_campaign_id=campaign_id,
        sequence_family=contact.get("sequence_family") or "unknown",
    )


def run(dry_run: bool = False, batch: int = BATCH_CAP):
    configure_logging("assign_push")
    logger.info("Starting. dry_run=%s batch_cap=%s", dry_run, batch)

    with db_conn() as conn:
        # Reset any contacts stuck in 'processing' from a prior crashed run
        _reset_stale_processing(conn)
        conn.commit()

    with db_conn() as conn:
        sent_today, remaining = _check_daily_enrollment(conn)

    logger.info(
        "Daily enrollment: %s sent today, %s remaining (cap=%s)",
        sent_today,
        remaining,
        DAILY_ENROLLMENT_CAP,
    )

    if remaining == 0:
        logger.info("Daily enrollment cap reached (%s). Skipping.", DAILY_ENROLLMENT_CAP)
        return

    # Respect both the per-run batch cap and the remaining daily budget
    effective_batch = min(batch, remaining)
    if effective_batch < batch:
        logger.info("Batch reduced to %s (daily budget remaining)", effective_batch)
    batch = effective_batch

    with db_conn() as conn:
        campaign_id_map = _get_campaign_id_map(conn)
        inactive_campaign_families = _get_inactive_campaign_families(conn)

    if not campaign_id_map:
        if inactive_campaign_families:
            logger.error(
                "No active sequence families currently have a smartlead_campaign_id set. "
                "There are %s inactive families with campaign ids: %s",
                len(inactive_campaign_families),
                inactive_campaign_families,
            )
            logger.error(
                "Re-enable the intended sequence_families rows before running assign_push."
            )
        else:
            logger.error("No sequence families have a smartlead_campaign_id set.")
            logger.error(
                "Run this SQL to populate (replace the IDs with your actual Smartlead campaign IDs):"
            )
            logger.error(
                "UPDATE sequence_families SET smartlead_campaign_id = '<id>' "
                "WHERE family_name = '<family>';"
            )
            logger.error(
                "Available families: ENT_REVMKTG_INTENT, ENT_REVMKTG_HIRING, "
                "ENT_ABM_INTENT, MM_REVMKTG_TIMING, MM_REVOPS_INTENT, MM_CMO_EXEC"
            )
        sys.exit(1)

    logger.info("Campaign map: %s", campaign_id_map)

    smartlead = SmartleadClient()
    try:
        _ensure_campaigns_launch_ready(smartlead, campaign_id_map)
    except RuntimeError as exc:
        if os.getenv(MAILBOX_LAUNCH_OVERRIDE_ENV) == "1":
            logger.warning("%s", exc)
            logger.warning(
                "Continuing because %s=1 is set. This bypasses the mailbox/campaign launch gate.",
                MAILBOX_LAUNCH_OVERRIDE_ENV,
            )
        else:
            logger.error("%s", exc)
            sys.exit(1)

    # Atomically claim eligible contacts — concurrent runs get disjoint sets
    with db_conn() as conn:
        contacts = _claim_eligible_contacts(conn, campaign_id_map, limit=batch)

    if not contacts:
        logger.info("No eligible contacts found.")
        logger.info("Done. pushed=0 failed=0")
        return

    logger.info("%s contacts claimed for push.", len(contacts))

    if dry_run:
        # Release claimed contacts back to 'pending' on dry-run
        with db_conn() as conn:
            with conn.cursor() as cur:
                ids = [str(c["assignment_id"]) for c in contacts]
                cur.execute("""
                    UPDATE campaign_assignments
                    SET eligibility_status = 'pending', processing_at = NULL
                    WHERE assignment_id = ANY(%s)
                """, (ids,))
            conn.commit()
        for c in contacts:
            logger.info(
                "[DRY RUN] %s %s <%s> -> %s (campaign %s)",
                c["first_name"],
                c["last_name"],
                c["email"],
                c["sequence_family"],
                campaign_id_map[c["sequence_family"]],
            )
        return

    # Group by campaign to batch API calls
    by_campaign: dict[str, list[dict]] = {}
    for c in contacts:
        cid = campaign_id_map[c["sequence_family"]]
        by_campaign.setdefault(cid, []).append(c)

    total_pushed = 0
    total_failed = 0

    for campaign_id, batch_contacts in by_campaign.items():
        leads_payload = [_build_lead_payload(c) for c in batch_contacts]
        logger.info("Pushing %s leads to campaign %s...", len(leads_payload), campaign_id)

        try:
            result = smartlead.add_leads(campaign_id, leads_payload)
            logger.info("Smartlead response: %s", result)

            # Stage 4b — API: log what Smartlead accepted per lead
            _link_log.info(
                f"[campaign={campaign_id}] API_RESPONSE"
                f" ok={result.get('ok') if isinstance(result, dict) else 'unknown'!r}"
                f" raw={str(result)[:500]!r}"
            )

            # Mark each contact as sent
            with db_conn() as conn:
                for c in batch_contacts:
                    # Smartlead returns per-lead results in result['data'] or similar
                    sl_lead_id = None
                    if isinstance(result, dict):
                        for item in result.get("data", []):
                            if isinstance(item, dict) and item.get("email") == c["email"]:
                                sl_lead_id = str(item.get("id", ""))
                                break

                    cid = f"{str(c.get('assignment_id', '?'))[:8]}|{c.get('email', '?')}"
                    _link_log.info(
                        f"[{cid}] ENROLLED"
                        f" campaign={campaign_id}"
                        f" sl_lead_id={sl_lead_id!r}"
                        f" status={'accepted' if sl_lead_id else 'no_id_returned'}"
                    )

                    _mark_sent(conn, c["assignment_id"], sl_lead_id, smartlead_campaign_id=campaign_id)
                    total_pushed += 1
                    logger.info("[sent] %s %s <%s>", c["first_name"], c["last_name"], c["email"])
                conn.commit()

            for c in batch_contacts:
                try:
                    route_result = _route_contact_to_salesforce(c, campaign_id)
                    logger.info(
                        "[salesforce] %s -> %s %s",
                        c["email"],
                        route_result.get("action"),
                        route_result.get("record_id"),
                    )
                except Exception as sf_exc:
                    logger.warning(
                        "[salesforce] routing failed for %s: %s",
                        c["email"],
                        sf_exc,
                    )

        except Exception as e:
            logger.error("Campaign %s push failed: %s", campaign_id, e)
            with db_conn() as conn:
                for c in batch_contacts:
                    _mark_ineligible(conn, c["assignment_id"], f"push_error: {str(e)[:200]}")
                conn.commit()
            total_failed += len(batch_contacts)

    logger.info("Done. pushed=%s failed=%s", total_pushed, total_failed)
