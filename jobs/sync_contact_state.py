"""
Job: sync_contact_state

Build a durable, queryable snapshot of each contact's current outbound state by
combining local DB state with live Smartlead campaign/lead telemetry.

Usage:
    python main.py sync_contact_state
    python main.py sync_contact_state --dry-run
    python main.py sync_contact_state --export /tmp/folloze-contact-state.csv
"""
from __future__ import annotations

import csv
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg2.extras

from connectors.smartlead_client import SmartleadClient
from db.connection import db_conn
from runtime_config import configure_logging

logger = logging.getLogger(__name__)

SNAPSHOT_COLUMNS = [
    "contact_id",
    "assignment_id",
    "email",
    "company_name",
    "sequence_family",
    "eligibility_status",
    "payload_status",
    "research_status",
    "smartlead_campaign_id",
    "smartlead_campaign_status",
    "smartlead_campaign_lead_map_id",
    "smartlead_prospect_id",
    "smartlead_lead_status",
    "current_step_number",
    "current_step_subject",
    "delivery_status",
    "reply_type",
    "current_activity",
    "activity_detail",
    "last_sent_at",
    "last_opened_at",
    "last_clicked_at",
    "last_replied_at",
    "last_activity_at",
]

EXPORT_COLUMNS = [
    "contact_id",
    "email",
    "first_name",
    "last_name",
    "company_name",
    "title",
    "persona_type",
    "source",
    "sequence_family",
    "eligibility_status",
    "payload_status",
    "research_status",
    "current_activity",
    "activity_detail",
    "smartlead_campaign_id",
    "smartlead_campaign_status",
    "smartlead_campaign_lead_map_id",
    "smartlead_prospect_id",
    "smartlead_lead_status",
    "current_step_number",
    "current_step_subject",
    "delivery_status",
    "reply_type",
    "last_sent_at",
    "last_opened_at",
    "last_clicked_at",
    "last_replied_at",
    "last_activity_at",
    "cycle_number",
    "ineligibility_reason",
]


def _parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _dtmax(*values: Any) -> datetime | None:
    parsed = [item for item in (_parse_ts(v) for v in values) if item is not None]
    return max(parsed) if parsed else None


def _iso(value: Any) -> str | None:
    parsed = _parse_ts(value)
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc).isoformat()


def _normalize_snapshot(snapshot: dict[str, Any]) -> dict[str, Any]:
    normalized = {}
    for key, value in snapshot.items():
        if isinstance(value, datetime):
            normalized[key] = _iso(value)
        else:
            normalized[key] = value
    return normalized


def _table_exists(conn, table_name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT to_regclass(%s)", (f"public.{table_name}",))
        row = cur.fetchone()
    return bool(row and row[0])


def _fetch_contact_rows(conn) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                c.contact_id::text,
                c.first_name,
                c.last_name,
                c.title,
                c.persona_type,
                c.source,
                c.email,
                LOWER(NULLIF(c.email, '')) AS email_key,
                c.created_at,
                c.last_enriched_at,
                c.last_sent_at AS contact_last_sent_at,
                a.company_name,
                a.domain,
                la.assignment_id::text,
                la.sequence_family,
                la.eligibility_status,
                la.ineligibility_reason,
                la.cycle_number,
                la.processing_at,
                la.scheduled_at,
                lp.qa_status AS payload_status,
                cr.research_id IS NOT NULL AS has_research,
                ls.smartlead_campaign_id AS last_send_smartlead_campaign_id,
                ls.smartlead_lead_id AS last_send_smartlead_lead_id,
                ls.sequence_family AS last_send_sequence_family,
                ls.step_number AS last_send_step_number,
                ls.sent_at AS last_send_sent_at,
                ls.delivery_status AS last_send_delivery_status,
                ls.reply_type AS last_send_reply_type
            FROM contacts c
            LEFT JOIN accounts a
              ON a.account_id = c.account_id
            LEFT JOIN LATERAL (
                SELECT
                    ca.assignment_id,
                    ca.sequence_family,
                    ca.eligibility_status,
                    ca.ineligibility_reason,
                    ca.cycle_number,
                    ca.processing_at,
                    ca.scheduled_at,
                    ca.created_at
                FROM campaign_assignments ca
                WHERE ca.contact_id = c.contact_id
                ORDER BY COALESCE(ca.scheduled_at, ca.processing_at, ca.created_at) DESC,
                         ca.created_at DESC
                LIMIT 1
            ) la ON TRUE
            LEFT JOIN LATERAL (
                SELECT pp.qa_status, pp.approved_at, pp.created_at
                FROM personalization_payloads pp
                WHERE pp.contact_id = c.contact_id
                ORDER BY COALESCE(pp.approved_at, pp.created_at) DESC,
                         pp.created_at DESC
                LIMIT 1
            ) lp ON TRUE
            LEFT JOIN LATERAL (
                SELECT cr.research_id
                FROM contact_research cr
                WHERE cr.contact_id = c.contact_id
                LIMIT 1
            ) cr ON TRUE
            LEFT JOIN LATERAL (
                SELECT
                    s.smartlead_campaign_id,
                    s.smartlead_lead_id,
                    s.sequence_family,
                    s.step_number,
                    s.sent_at,
                    s.delivery_status,
                    s.reply_type,
                    s.created_at
                FROM sends s
                WHERE s.contact_id = c.contact_id
                ORDER BY COALESCE(s.sent_at, s.created_at) DESC,
                         s.created_at DESC
                LIMIT 1
            ) ls ON TRUE
            ORDER BY COALESCE(ls.sent_at, la.scheduled_at, la.processing_at, c.created_at) DESC NULLS LAST,
                     c.created_at DESC
            """
        )
        return [dict(row) for row in cur.fetchall()]


def _fetch_family_campaign_map(conn) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT family_name, smartlead_campaign_id
            FROM sequence_families
            WHERE smartlead_campaign_id IS NOT NULL
            """
        )
        return {str(row[0]): str(row[1]) for row in cur.fetchall()}


def _fetch_latest_snapshots(conn) -> dict[str, dict[str, Any]]:
    if not _table_exists(conn, "contact_state_snapshots"):
        return {}

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT DISTINCT ON (contact_id)
                contact_id::text,
                raw_snapshot
            FROM contact_state_snapshots
            ORDER BY contact_id, created_at DESC
            """
        )
        return {
            str(row["contact_id"]): (row["raw_snapshot"] or {})
            for row in cur.fetchall()
        }


def _fetch_all_campaign_leads(client: SmartleadClient, campaign_id: str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = client.get_leads(campaign_id, offset=offset, limit=100)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < 100:
            break
        offset += 100
    return rows


def _aggregate_stat_row(
    aggregated: dict[str, dict[str, Any]],
    row: dict[str, Any],
    sequence_map: dict[int, dict[str, Any]],
) -> None:
    email_key = (row.get("lead_email") or "").strip().lower()
    if not email_key:
        return

    entry = aggregated.setdefault(
        email_key,
        {
            "current_step_number": None,
            "current_step_subject": None,
            "last_sent_at": None,
            "last_opened_at": None,
            "last_clicked_at": None,
            "last_replied_at": None,
            "delivery_status": None,
            "open_count": 0,
            "click_count": 0,
        },
    )

    seq_id = row.get("email_campaign_seq_id")
    sequence_info = sequence_map.get(int(seq_id), {}) if seq_id is not None else {}
    step_number = row.get("sequence_number") or sequence_info.get("seq_number")
    sent_at = _parse_ts(row.get("sent_time"))

    if step_number is not None:
        if entry["current_step_number"] is None or int(step_number) >= int(entry["current_step_number"]):
            entry["current_step_number"] = int(step_number)
            entry["current_step_subject"] = row.get("email_subject") or sequence_info.get("subject")

    entry["last_sent_at"] = _dtmax(entry["last_sent_at"], sent_at)
    entry["last_opened_at"] = _dtmax(entry["last_opened_at"], row.get("open_time"))
    entry["last_clicked_at"] = _dtmax(entry["last_clicked_at"], row.get("click_time"))
    entry["last_replied_at"] = _dtmax(entry["last_replied_at"], row.get("reply_time"))
    entry["open_count"] = max(int(entry["open_count"] or 0), int(row.get("open_count") or 0))
    entry["click_count"] = max(int(entry["click_count"] or 0), int(row.get("click_count") or 0))

    if row.get("is_bounced"):
        entry["delivery_status"] = "bounced"
    elif row.get("is_unsubscribed"):
        entry["delivery_status"] = "unsubscribed"
    elif sent_at and entry["delivery_status"] is None:
        entry["delivery_status"] = "delivered"


def _fetch_campaign_statistics_for_emails(
    client: SmartleadClient,
    campaign_id: str,
    sequence_map: dict[int, dict[str, Any]],
    email_filter: set[str],
) -> dict[str, dict[str, Any]]:
    aggregated: dict[str, dict[str, Any]] = {}
    offset = 0
    total = None

    while True:
        page = client.get_campaign_statistics_page(campaign_id, offset=offset, limit=100)
        rows = page.get("data") or []
        if total is None:
            try:
                total = int(page.get("total_stats") or 0)
            except (TypeError, ValueError):
                total = 0

        for row in rows:
            email_key = (row.get("lead_email") or "").strip().lower()
            if email_key and email_key in email_filter:
                _aggregate_stat_row(aggregated, row, sequence_map)

        if not rows:
            break
        offset += len(rows)
        if total and offset >= total:
            break
        if len(rows) < 100:
            break

    return aggregated


def _fetch_live_state_by_campaign(
    client: SmartleadClient,
    campaign_id: str,
    campaign_status: str,
    email_filter: set[str],
) -> dict[str, dict[str, Any]]:
    if not email_filter:
        return {}

    sequences = client.get_sequences(campaign_id)
    sequence_map = {
        int(seq["id"]): {
            "seq_number": seq.get("seq_number"),
            "subject": seq.get("subject"),
        }
        for seq in sequences
        if seq.get("id") is not None
    }

    leads = _fetch_all_campaign_leads(client, campaign_id)
    by_email: dict[str, dict[str, Any]] = {}

    for item in leads:
        nested = item.get("lead") or {}
        email_key = (nested.get("email") or item.get("lead_email") or "").strip().lower()
        if not email_key or email_key not in email_filter:
            continue
        by_email[email_key] = {
            "smartlead_campaign_id": str(campaign_id),
            "smartlead_campaign_status": campaign_status,
            "smartlead_campaign_lead_map_id": str(item.get("campaign_lead_map_id") or ""),
            "smartlead_prospect_id": str(nested.get("id") or ""),
            "smartlead_lead_status": item.get("status"),
        }

    stats_by_email = _fetch_campaign_statistics_for_emails(
        client,
        campaign_id,
        sequence_map,
        email_filter,
    )

    for email_key, stats in stats_by_email.items():
        base = by_email.setdefault(
            email_key,
            {
                "smartlead_campaign_id": str(campaign_id),
                "smartlead_campaign_status": campaign_status,
                "smartlead_campaign_lead_map_id": None,
                "smartlead_prospect_id": None,
                "smartlead_lead_status": None,
            },
        )
        base.update(stats)

    return by_email


def _derive_current_activity(contact: dict[str, Any], live: dict[str, Any]) -> tuple[str, str | None]:
    eligibility = (contact.get("eligibility_status") or "").strip().lower()
    payload_status = (contact.get("payload_status") or "").strip().lower()
    has_research = bool(contact.get("has_research"))
    email = (contact.get("email") or "").strip()
    lead_status = (live.get("smartlead_lead_status") or "").strip().lower()
    reply_type = (live.get("reply_type") or contact.get("last_send_reply_type") or "").strip().lower()
    delivery_status = (
        live.get("delivery_status")
        or contact.get("last_send_delivery_status")
        or ""
    ).strip().lower()

    if eligibility == "processing":
        return "queued_for_smartlead", None

    if eligibility == "ineligible":
        reason = contact.get("ineligibility_reason")
        if not email:
            return "needs_email", reason or "missing email"
        return "ineligible", reason

    if eligibility in {"pending", "eligible"}:
        if not email:
            return "needs_email", "missing email"
        if not has_research:
            return "awaiting_research", None
        if payload_status == "approved":
            return "ready_to_push", None
        if payload_status == "failed":
            return "payload_failed_qa", None
        if not payload_status:
            return "awaiting_payload", None
        return "pending_review", payload_status

    if eligibility == "sent":
        if delivery_status == "bounced":
            return "bounced", None
        if reply_type:
            return f"replied_{reply_type}", None
        if delivery_status == "unsubscribed":
            return "unsubscribed", None
        if lead_status in {"active", "in_progress", "inprogress", "started"}:
            step = live.get("current_step_number")
            return "in_sequence", f"step {step}" if step else None
        if lead_status == "completed":
            step = live.get("current_step_number")
            return "sequence_completed", f"step {step}" if step else None
        if lead_status in {"paused", "stopped", "blocked"}:
            return "sequence_stopped", None
        return "sent", lead_status or None

    return "untracked", None


def _build_snapshot(contact: dict[str, Any], live: dict[str, Any]) -> dict[str, Any]:
    current_activity, activity_detail = _derive_current_activity(contact, live)
    current_step_number = live.get("current_step_number") or contact.get("last_send_step_number")
    last_sent_at = _dtmax(
        live.get("last_sent_at"),
        contact.get("last_send_sent_at"),
        contact.get("contact_last_sent_at"),
    )
    last_opened_at = _parse_ts(live.get("last_opened_at"))
    last_clicked_at = _parse_ts(live.get("last_clicked_at"))
    last_replied_at = _parse_ts(live.get("last_replied_at"))
    last_activity_at = _dtmax(
        last_replied_at,
        last_clicked_at,
        last_opened_at,
        last_sent_at,
        contact.get("scheduled_at"),
        contact.get("processing_at"),
        contact.get("last_enriched_at"),
        contact.get("created_at"),
    )

    return {
        "contact_id": contact.get("contact_id"),
        "assignment_id": contact.get("assignment_id"),
        "email": contact.get("email"),
        "company_name": contact.get("company_name"),
        "sequence_family": contact.get("sequence_family") or contact.get("last_send_sequence_family"),
        "eligibility_status": contact.get("eligibility_status"),
        "payload_status": contact.get("payload_status"),
        "research_status": "present" if contact.get("has_research") else "missing",
        "smartlead_campaign_id": live.get("smartlead_campaign_id") or contact.get("last_send_smartlead_campaign_id"),
        "smartlead_campaign_status": live.get("smartlead_campaign_status"),
        "smartlead_campaign_lead_map_id": live.get("smartlead_campaign_lead_map_id"),
        "smartlead_prospect_id": live.get("smartlead_prospect_id"),
        "smartlead_lead_status": live.get("smartlead_lead_status"),
        "current_step_number": current_step_number,
        "current_step_subject": live.get("current_step_subject"),
        "delivery_status": live.get("delivery_status") or contact.get("last_send_delivery_status"),
        "reply_type": live.get("reply_type") or contact.get("last_send_reply_type"),
        "current_activity": current_activity,
        "activity_detail": activity_detail,
        "last_sent_at": last_sent_at,
        "last_opened_at": last_opened_at,
        "last_clicked_at": last_clicked_at,
        "last_replied_at": last_replied_at,
        "last_activity_at": last_activity_at,
    }


def _insert_snapshot(conn, snapshot: dict[str, Any], normalized: dict[str, Any]) -> None:
    values = [snapshot.get(column) for column in SNAPSHOT_COLUMNS]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO contact_state_snapshots (
                {", ".join(SNAPSHOT_COLUMNS)},
                raw_snapshot
            )
            VALUES (
                {", ".join(["%s"] * len(SNAPSHOT_COLUMNS))},
                %s
            )
            """,
            values + [psycopg2.extras.Json(normalized)],
        )


def _write_export(rows: list[dict[str, Any]], export_path: Path) -> None:
    export_path.parent.mkdir(parents=True, exist_ok=True)
    with export_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column) for column in EXPORT_COLUMNS})


def run(*, dry_run: bool = False, export_path: str | None = None) -> None:
    configure_logging("sync_contact_state")

    with db_conn() as conn:
        if not dry_run and not _table_exists(conn, "contact_state_snapshots"):
            raise RuntimeError(
                "contact_state_snapshots table is missing. Run `python main.py migrate` first."
            )

        contacts = _fetch_contact_rows(conn)
        family_campaign_map = _fetch_family_campaign_map(conn)
        latest_snapshots = _fetch_latest_snapshots(conn)

    logger.info("Loaded %s contacts", len(contacts))

    campaign_email_sets: dict[str, set[str]] = {}
    for contact in contacts:
        email_key = contact.get("email_key")
        if not email_key:
            continue
        campaign_id = contact.get("last_send_smartlead_campaign_id") or family_campaign_map.get(
            contact.get("sequence_family") or ""
        )
        if campaign_id:
            campaign_email_sets.setdefault(str(campaign_id), set()).add(str(email_key))

    client = SmartleadClient()
    campaign_status_map = {
        str(item.get("id") or item.get("campaign_id")): item.get("status")
        for item in client.list_campaigns()
    }

    live_state_by_campaign: dict[str, dict[str, dict[str, Any]]] = {}
    for campaign_id, email_filter in sorted(campaign_email_sets.items()):
        live_state_by_campaign[campaign_id] = _fetch_live_state_by_campaign(
            client,
            campaign_id,
            str(campaign_status_map.get(campaign_id) or "unknown"),
            email_filter,
        )
        logger.info(
            "Fetched Smartlead state for campaign %s (%s tracked emails)",
            campaign_id,
            len(live_state_by_campaign[campaign_id]),
        )

    changed = 0
    unchanged = 0
    counts = Counter()
    export_rows: list[dict[str, Any]] = []

    with db_conn() as conn:
        for contact in contacts:
            campaign_id = contact.get("last_send_smartlead_campaign_id") or family_campaign_map.get(
                contact.get("sequence_family") or ""
            )
            live = {}
            if campaign_id and contact.get("email_key"):
                live = (
                    live_state_by_campaign.get(str(campaign_id), {}).get(str(contact["email_key"]))
                    or {}
                )

            snapshot = _build_snapshot(contact, live)
            normalized = _normalize_snapshot(snapshot)
            previous = latest_snapshots.get(str(contact.get("contact_id"))) or {}

            if normalized == previous:
                unchanged += 1
            else:
                changed += 1
                if not dry_run:
                    _insert_snapshot(conn, snapshot, normalized)

            counts[snapshot["current_activity"]] += 1

            export_row = {
                **{key: contact.get(key) for key in ["contact_id", "email", "first_name", "last_name", "company_name", "title", "persona_type", "source", "cycle_number", "ineligibility_reason"]},
                **_normalize_snapshot(snapshot),
            }
            export_rows.append(export_row)

        if not dry_run:
            conn.commit()

    logger.info(
        "Contact state sync complete. changed=%s unchanged=%s dry_run=%s",
        changed,
        unchanged,
        dry_run,
    )
    for activity, count in sorted(counts.items()):
        logger.info("  %s: %s", activity, count)

    if export_path:
        path = Path(export_path).expanduser()
        _write_export(export_rows, path)
        logger.info("Exported %s rows to %s", len(export_rows), path)
