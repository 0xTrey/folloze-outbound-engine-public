"""
Job: build_cutover_package

Refresh the live outbound state and export the artifacts needed for a Smartlead
sender-domain transfer.

Usage:
    python main.py build_cutover_package
    python main.py build_cutover_package --output-dir /tmp/folloze-domain-transfer-2026-03-28
    python main.py build_cutover_package --no-sync
"""
from __future__ import annotations

import csv
import json
import logging
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from connectors.smartlead_client import SmartleadClient
from db.connection import db_conn
from jobs.sync_contact_state import run as sync_contact_state_run
from runtime_config import configure_logging

logger = logging.getLogger(__name__)

CONTACT_STATE_COLUMNS = [
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

MAILBOX_COLUMNS = [
    "account_id",
    "username",
    "from_email",
    "from_name",
    "domain",
    "message_per_day",
    "daily_sent_count",
    "campaign_count",
    "is_connected_to_campaign",
    "custom_tracking_domain",
    "smtp_ok",
    "imap_ok",
    "warmup_status",
    "warmup_blocked",
    "warmup_reputation",
    "warmup_max_per_day",
]

CAMPAIGN_MAILBOX_COLUMNS = [
    "campaign_id",
    "campaign_name",
    "campaign_status",
    "account_id",
    "username",
    "from_email",
    "domain",
    "custom_tracking_domain",
    "warmup_status",
    "warmup_blocked",
]


def _today_stamp() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _default_output_dir() -> Path:
    return Path("/tmp") / f"folloze-domain-transfer-{_today_stamp()}"


def _write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field) for field in fieldnames})


def _read_contact_state_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="") as handle:
        return list(csv.DictReader(handle))


def _is_in_flight(row: dict[str, str]) -> bool:
    activity = (row.get("current_activity") or "").strip().lower()
    eligibility = (row.get("eligibility_status") or "").strip().lower()
    if eligibility != "sent":
        return False
    if activity in {"sequence_completed", "bounced", "unsubscribed"}:
        return False
    if activity.startswith("replied_"):
        return False
    return True


def _is_pending_queue(row: dict[str, str]) -> bool:
    return (row.get("eligibility_status") or "").strip().lower() in {
        "pending",
        "eligible",
        "processing",
    }


def _counter_dict(rows: list[dict[str, Any]], key: str) -> dict[str, int]:
    counter = Counter((row.get(key) or "unknown") for row in rows)
    return dict(sorted(counter.items(), key=lambda item: item[0]))


def _domain_of(email: str | None) -> str | None:
    if not email or "@" not in email:
        return None
    return email.split("@", 1)[1].lower()


def _normalize_mailbox(account: dict[str, Any]) -> dict[str, Any]:
    warmup = account.get("warmup_details") or {}
    username = account.get("username") or account.get("from_email")
    return {
        "account_id": str(account.get("id") or ""),
        "username": username,
        "from_email": account.get("from_email"),
        "from_name": account.get("from_name"),
        "domain": _domain_of(username),
        "message_per_day": account.get("message_per_day"),
        "daily_sent_count": account.get("daily_sent_count"),
        "campaign_count": account.get("campaign_count"),
        "is_connected_to_campaign": account.get("is_connected_to_campaign"),
        "custom_tracking_domain": account.get("custom_tracking_domain"),
        "smtp_ok": account.get("is_smtp_success"),
        "imap_ok": account.get("is_imap_success"),
        "warmup_status": warmup.get("status"),
        "warmup_blocked": warmup.get("is_warmup_blocked"),
        "warmup_reputation": warmup.get("warmup_reputation"),
        "warmup_max_per_day": warmup.get("max_email_per_day"),
    }


def _fetch_sequence_families(conn) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT family_name, smartlead_campaign_id, is_active
            FROM sequence_families
            ORDER BY family_name
            """
        )
        rows = []
        for family_name, smartlead_campaign_id, is_active in cur.fetchall():
            rows.append(
                {
                    "family_name": family_name,
                    "smartlead_campaign_id": str(smartlead_campaign_id) if smartlead_campaign_id else None,
                    "is_active": bool(is_active),
                }
            )
        return rows


def _render_support_brief(
    *,
    generated_at: str,
    sequence_families: list[dict[str, Any]],
    mailboxes: list[dict[str, Any]],
    contact_rows: list[dict[str, str]],
    inflight_rows: list[dict[str, str]],
    pending_rows: list[dict[str, str]],
) -> str:
    domain_counts = Counter(row.get("domain") or "unknown" for row in mailboxes)
    state_counts = Counter((row.get("current_activity") or "unknown") for row in contact_rows)
    inflight_counts = Counter((row.get("sequence_family") or "unknown") for row in inflight_rows)
    pending_counts = Counter((row.get("sequence_family") or "unknown") for row in pending_rows)

    lines = [
        "# Smartlead Domain Transfer Support Brief",
        "",
        f"Generated: {generated_at}",
        "",
        "## Current Workspace Scope",
        "",
        f"- Sender inboxes: {len(mailboxes)}",
        "- Sender domains:",
    ]
    for domain, count in sorted(domain_counts.items()):
        lines.append(f"  - `{domain}`: {count}")

    lines.extend(
        [
            "",
            "## Sequence Families To Freeze / Rebuild",
            "",
        ]
    )
    for row in sequence_families:
        lines.append(
            f"- `{row['family_name']}` -> Smartlead campaign `{row['smartlead_campaign_id']}`"
            f" | local active=`{row['is_active']}`"
        )

    lines.extend(
        [
            "",
            "## Current Contact State",
            "",
        ]
    )
    for activity, count in sorted(state_counts.items()):
        lines.append(f"- `{activity}`: {count}")

    lines.extend(
        [
            "",
            f"## In-Flight Contacts ({len(inflight_rows)})",
            "",
        ]
    )
    for family, count in sorted(inflight_counts.items()):
        lines.append(f"- `{family}`: {count}")

    lines.extend(
        [
            "",
            f"## Pending / Pre-Send Queue ({len(pending_rows)})",
            "",
        ]
    )
    for family, count in sorted(pending_counts.items()):
        lines.append(f"- `{family}`: {count}")

    lines.extend(
        [
            "",
            "## Requests For Smartlead Support",
            "",
            "- Confirm whether mailbox objects will survive the domain ownership transfer or be recreated.",
            "- Confirm whether mailbox-to-lead associations for in-flight leads can be preserved during the move.",
            "- If mailbox-to-lead associations cannot be preserved, export that mapping before cutover.",
            "- Confirm every custom tracking domain that must be recreated in Cloudflare.",
            "- Confirm whether post-transfer recovery should use mailbox re-association or mailbox re-addition.",
            "",
            "## Package Files",
            "",
            "- `contact-state.csv` — full current-state export for all contacts",
            "- `in-flight.csv` — contacts currently mid-sequence or otherwise still in sent state",
            "- `pending-queue.csv` — contacts not yet sent but sitting in local queue state",
            "- `mailboxes.csv` — sender mailbox inventory with warmup / connectivity fields",
            "- `campaign-mailboxes.csv` — campaign-to-mailbox assignments",
            "- `campaigns.json` — raw campaign inventory from Smartlead",
            "- `summary.json` — machine-readable rollup of this package",
            "",
            "## Important Note",
            "",
            "- The current API export does not provide a guaranteed mailbox-to-lead association table for every in-flight lead. Ask Smartlead support to export or preserve that mapping before transfer.",
            "",
        ]
    )
    return "\n".join(lines)


def run(*, output_dir: str | None = None, sync_state: bool = True) -> Path:
    configure_logging("build_cutover_package")

    out_dir = Path(output_dir).expanduser() if output_dir else _default_output_dir()
    out_dir.mkdir(parents=True, exist_ok=True)

    generated_at = datetime.now(timezone.utc).isoformat()
    contact_state_path = out_dir / "contact-state.csv"

    if sync_state:
        logger.info("Refreshing contact-state snapshot before export")
        sync_contact_state_run(dry_run=False, export_path=str(contact_state_path))
    elif not contact_state_path.exists():
        raise RuntimeError(
            f"{contact_state_path} does not exist. Re-run without --no-sync or provide a prepared export."
        )

    contact_rows = _read_contact_state_rows(contact_state_path)
    inflight_rows = [row for row in contact_rows if _is_in_flight(row)]
    pending_rows = [row for row in contact_rows if _is_pending_queue(row)]

    _write_csv(out_dir / "in-flight.csv", inflight_rows, CONTACT_STATE_COLUMNS)
    _write_csv(out_dir / "pending-queue.csv", pending_rows, CONTACT_STATE_COLUMNS)

    with db_conn() as conn:
        sequence_families = _fetch_sequence_families(conn)

    client = SmartleadClient()
    campaigns = client.list_campaigns()
    campaigns_by_id = {
        str(item.get("id") or item.get("campaign_id")): item
        for item in campaigns
        if item.get("id") or item.get("campaign_id")
    }

    mailbox_rows = sorted(
        (_normalize_mailbox(account) for account in client.list_email_accounts()),
        key=lambda row: (row.get("domain") or "", row.get("username") or ""),
    )
    _write_csv(out_dir / "mailboxes.csv", mailbox_rows, MAILBOX_COLUMNS)

    campaign_mailbox_rows: list[dict[str, Any]] = []
    for campaign_id, campaign in sorted(campaigns_by_id.items(), key=lambda item: int(item[0])):
        try:
            assigned_accounts = client.get_campaign_email_accounts(campaign_id)
        except Exception as exc:
            logger.warning("Failed to fetch campaign mailboxes for %s: %s", campaign_id, exc)
            continue

        for account in assigned_accounts:
            normalized = _normalize_mailbox(account)
            campaign_mailbox_rows.append(
                {
                    "campaign_id": campaign_id,
                    "campaign_name": campaign.get("name"),
                    "campaign_status": campaign.get("status"),
                    "account_id": normalized.get("account_id"),
                    "username": normalized.get("username"),
                    "from_email": normalized.get("from_email"),
                    "domain": normalized.get("domain"),
                    "custom_tracking_domain": normalized.get("custom_tracking_domain"),
                    "warmup_status": normalized.get("warmup_status"),
                    "warmup_blocked": normalized.get("warmup_blocked"),
                }
            )

    _write_csv(
        out_dir / "campaign-mailboxes.csv",
        campaign_mailbox_rows,
        CAMPAIGN_MAILBOX_COLUMNS,
    )

    campaigns_json_path = out_dir / "campaigns.json"
    campaigns_json_path.write_text(json.dumps(campaigns, indent=2, default=str))

    summary = {
        "generated_at": generated_at,
        "output_dir": str(out_dir),
        "sequence_families": sequence_families,
        "mailbox_domain_counts": _counter_dict(mailbox_rows, "domain"),
        "contact_state_counts": _counter_dict(contact_rows, "current_activity"),
        "in_flight_count": len(inflight_rows),
        "in_flight_by_family": _counter_dict(inflight_rows, "sequence_family"),
        "pending_queue_count": len(pending_rows),
        "pending_queue_by_family": _counter_dict(pending_rows, "sequence_family"),
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str))
    (out_dir / "support-brief.md").write_text(
        _render_support_brief(
            generated_at=generated_at,
            sequence_families=sequence_families,
            mailboxes=mailbox_rows,
            contact_rows=contact_rows,
            inflight_rows=inflight_rows,
            pending_rows=pending_rows,
        )
    )

    logger.info("Cutover package written to %s", out_dir)
    logger.info("  contact-state.csv rows: %s", len(contact_rows))
    logger.info("  in-flight.csv rows: %s", len(inflight_rows))
    logger.info("  pending-queue.csv rows: %s", len(pending_rows))
    logger.info("  mailboxes.csv rows: %s", len(mailbox_rows))
    logger.info("  campaign-mailboxes.csv rows: %s", len(campaign_mailbox_rows))

    return out_dir
