"""
Job: warmup_smartlead_inboxes

Configure Smartlead inbox warmup settings for connected mailboxes and optionally
sync the resulting mailbox state into Neon and campaign mailbox assignments.

This job does not add leads or start cold outbound. It only prepares sender
inboxes and records their current state.

Usage:
    python main.py warmup_smartlead_inboxes --dry-run
    python main.py warmup_smartlead_inboxes
    python main.py warmup_smartlead_inboxes --sync-mailboxes --sync-campaigns
"""
from __future__ import annotations

import logging
from typing import Iterable

from connectors.smartlead_client import SmartleadClient
from jobs.setup_campaigns import _mailbox_label
from jobs.sync_smartlead_mailboxes import run as sync_smartlead_mailboxes_run
from runtime_config import configure_logging

logger = logging.getLogger(__name__)


def _is_connected_mailbox(account: dict) -> bool:
    return bool(account.get("is_imap_success") and account.get("is_smtp_success"))


def _matches_mailbox_filters(account: dict, mailbox_filters: Iterable[str] | None) -> bool:
    if not mailbox_filters:
        return True
    wanted = {value.strip().lower() for value in mailbox_filters if value and value.strip()}
    if not wanted:
        return True
    account_id = str(account.get("id") or "").strip().lower()
    label = _mailbox_label(account).strip().lower()
    return account_id in wanted or label in wanted


def run(
    *,
    dry_run: bool = False,
    mailbox_filters: list[str] | None = None,
    warmup_per_day: int = 20,
    daily_rampup: int = 5,
    reply_rate: int = 30,
    max_email_per_day: int = 30,
    wait_minutes: int = 20,
    sync_mailboxes: bool = True,
    sync_campaigns: bool = False,
    active_families_only: bool = False,
) -> None:
    logger = configure_logging("warmup_smartlead_inboxes")
    smartlead = SmartleadClient()
    accounts = smartlead.list_email_accounts()

    selected_accounts = [
        account
        for account in accounts
        if _matches_mailbox_filters(account, mailbox_filters)
    ]
    connected_accounts = [
        account
        for account in selected_accounts
        if _is_connected_mailbox(account)
    ]
    skipped_accounts = [
        account
        for account in selected_accounts
        if not _is_connected_mailbox(account)
    ]

    logger.info(
        "Starting. dry_run=%s selected_accounts=%s connected_accounts=%s skipped_accounts=%s sync_mailboxes=%s sync_campaigns=%s",
        dry_run,
        len(selected_accounts),
        len(connected_accounts),
        len(skipped_accounts),
        sync_mailboxes,
        sync_campaigns,
    )

    for account in skipped_accounts:
        logger.warning(
            "Skipping mailbox=%s id=%s because IMAP/SMTP connectivity is not healthy (imap=%s smtp=%s)",
            _mailbox_label(account),
            account.get("id"),
            account.get("is_imap_success"),
            account.get("is_smtp_success"),
        )

    configured = 0
    for account in connected_accounts:
        account_id = str(account["id"])
        details = smartlead.get_email_account(account_id)
        warmup_stats = smartlead.get_email_account_warmup_stats(account_id)
        label = _mailbox_label(details)
        current_warmup = details.get("warmup_details") or {}

        logger.info(
            "mailbox=%s id=%s created_at=%s current_message_per_day=%s warmup_status=%s warmup_sent=%s warmup_received=%s",
            label,
            account_id,
            details.get("created_at"),
            details.get("message_per_day"),
            current_warmup.get("status") or "missing",
            warmup_stats.get("sent_count") or 0,
            warmup_stats.get("warmup_email_received_count") or 0,
        )

        if dry_run:
            logger.info(
                "[DRY RUN] would configure mailbox=%s id=%s warmup_enabled=true total_warmup_per_day=%s daily_rampup=%s max_email_per_day=%s reply_rate_percentage=%s time_to_wait_in_mins=%s",
                label,
                account_id,
                warmup_per_day,
                daily_rampup,
                max_email_per_day,
                reply_rate,
                wait_minutes,
            )
            configured += 1
            continue

        result = smartlead.configure_email_account_warmup(
            account_id,
            warmup_enabled=True,
            total_warmup_per_day=warmup_per_day,
            daily_rampup=daily_rampup,
            max_email_per_day=max_email_per_day,
            reply_rate_percentage=reply_rate,
            time_to_wait_in_mins=wait_minutes,
        )
        configured += 1
        logger.info(
            "Configured mailbox=%s id=%s response_status=%s response_keys=%s",
            label,
            account_id,
            (result.get("data") or {}).get("warmup_details", {}).get("status")
            if isinstance(result, dict)
            else None,
            sorted(result.keys()) if isinstance(result, dict) else type(result).__name__,
        )

    logger.info(
        "Warmup configuration complete. configured=%s skipped=%s dry_run=%s",
        configured,
        len(skipped_accounts),
        dry_run,
    )

    if sync_mailboxes:
        sync_smartlead_mailboxes_run(
            dry_run=dry_run,
            sync_campaigns=sync_campaigns,
            active_families_only=active_families_only,
        )
