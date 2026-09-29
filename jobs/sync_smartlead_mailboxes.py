"""
Job: sync_smartlead_mailboxes

Sync live Smartlead inbox state into the local mailboxes table and optionally
reconcile those inboxes onto existing Smartlead campaigns.

This is safe to run while the outbound engine is paused. It does not add leads
or send mail.

Usage:
    python main.py sync_smartlead_mailboxes --dry-run
    python main.py sync_smartlead_mailboxes
    python main.py sync_smartlead_mailboxes --sync-campaigns
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from connectors.smartlead_client import SmartleadClient
from db.connection import db_conn
from jobs.setup_campaigns import (
    _mailbox_label,
    _mailbox_status,
    _select_assignable_email_accounts,
    _sync_campaign_email_accounts,
)
from runtime_config import configure_logging

logger = logging.getLogger(__name__)


def _derive_provider(account: dict) -> str:
    account_type = str(account.get("type") or "").strip().lower()
    if account_type:
        return f"smartlead_{account_type}"
    return "smartlead"


def _derive_health_status(account: dict) -> str:
    if account.get("is_imap_success") and account.get("is_smtp_success"):
        return "healthy"
    return "degraded"


def _derive_warmup_status(account: dict) -> str:
    status = _mailbox_status(account).lower()
    if status in {"active", "warming", "paused"}:
        return status
    if status == "connected":
        return "warming"
    if status in {"failed", "error", "unhealthy"}:
        return "dead"
    return "warming"


def _build_mailbox_row(account: dict) -> dict:
    email_address = _mailbox_label(account)
    sending_domain = email_address.split("@", 1)[1].lower() if "@" in email_address else None
    return {
        "email_address": email_address,
        "sending_domain": sending_domain,
        "provider": _derive_provider(account),
        "warmup_status": _derive_warmup_status(account),
        "health_status": _derive_health_status(account),
        "daily_cap": int(account.get("message_per_day") or 30),
        "current_daily_sent": int(account.get("daily_sent_count") or 0),
        "last_checked_at": datetime.now(timezone.utc),
    }


def _upsert_mailbox(conn, mailbox: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO mailboxes (
                email_address,
                sending_domain,
                provider,
                warmup_status,
                health_status,
                daily_cap,
                current_daily_sent,
                last_checked_at
            )
            VALUES (
                %(email_address)s,
                %(sending_domain)s,
                %(provider)s,
                %(warmup_status)s,
                %(health_status)s,
                %(daily_cap)s,
                %(current_daily_sent)s,
                %(last_checked_at)s
            )
            ON CONFLICT (email_address)
            DO UPDATE SET
                sending_domain = EXCLUDED.sending_domain,
                provider = EXCLUDED.provider,
                warmup_status = EXCLUDED.warmup_status,
                health_status = EXCLUDED.health_status,
                daily_cap = EXCLUDED.daily_cap,
                current_daily_sent = EXCLUDED.current_daily_sent,
                last_checked_at = EXCLUDED.last_checked_at
            """,
            mailbox,
        )


def _fetch_campaigns(conn, *, active_only: bool) -> list[dict]:
    where = "WHERE smartlead_campaign_id IS NOT NULL"
    if active_only:
        where += " AND is_active = TRUE"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT family_name, smartlead_campaign_id, is_active
            FROM sequence_families
            {where}
            ORDER BY family_name
            """
        )
        cols = [desc[0] for desc in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def run(*, dry_run: bool = False, sync_campaigns: bool = False, active_families_only: bool = False) -> None:
    logger = configure_logging("sync_smartlead_mailboxes")
    smartlead = SmartleadClient()
    accounts = smartlead.list_email_accounts()
    assignable_accounts = _select_assignable_email_accounts(accounts)

    logger.info(
        "Starting. dry_run=%s sync_campaigns=%s active_families_only=%s connected_accounts=%s assignable_accounts=%s",
        dry_run,
        sync_campaigns,
        active_families_only,
        len(accounts),
        len(assignable_accounts),
    )

    for account in accounts:
        logger.info(
            "mailbox=%s id=%s status=%s imap=%s smtp=%s",
            _mailbox_label(account),
            account.get("id"),
            _mailbox_status(account),
            account.get("is_imap_success"),
            account.get("is_smtp_success"),
        )

    synced_mailboxes = 0
    if not dry_run:
        with db_conn() as conn:
            for account in accounts:
                _upsert_mailbox(conn, _build_mailbox_row(account))
                synced_mailboxes += 1
    else:
        synced_mailboxes = len(accounts)

    synced_campaigns = 0
    if sync_campaigns:
        with db_conn() as conn:
            campaigns = _fetch_campaigns(conn, active_only=active_families_only)
        for campaign in campaigns:
            result = _sync_campaign_email_accounts(
                smartlead,
                str(campaign["smartlead_campaign_id"]),
                assignable_accounts,
                dry_run=dry_run,
            )
            logger.info(
                "campaign=%s family=%s active=%s mailbox_sync=%s current=%s desired=%s",
                campaign["smartlead_campaign_id"],
                campaign["family_name"],
                campaign["is_active"],
                result["status"],
                result["current_labels"],
                result["desired_labels"],
            )
            if result["status"] in {"updated", "would_update"}:
                synced_campaigns += 1

    logger.info(
        "Done. synced_mailboxes=%s synced_campaigns=%s dry_run=%s",
        synced_mailboxes,
        synced_campaigns,
        dry_run,
    )
