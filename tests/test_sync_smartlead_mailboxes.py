import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from jobs.sync_smartlead_mailboxes import (  # noqa: E402
    _build_mailbox_row,
    _derive_health_status,
    _derive_warmup_status,
)


def test_build_mailbox_row_maps_connected_account():
    account = {
        "id": 17754665,
        "from_email": "sender@example.com",
        "type": "GMAIL",
        "message_per_day": 15,
        "daily_sent_count": 0,
        "is_imap_success": True,
        "is_smtp_success": True,
        "warmup_details": None,
    }

    row = _build_mailbox_row(account)

    assert row["email_address"] == "sender@example.com"
    assert row["sending_domain"] == "example.com"
    assert row["provider"] == "smartlead_gmail"
    assert row["warmup_status"] == "warming"
    assert row["health_status"] == "healthy"
    assert row["daily_cap"] == 15
    assert row["current_daily_sent"] == 0


def test_derive_warmup_status_prefers_explicit_status():
    account = {
        "warmup_details": {"status": "PAUSED"},
        "is_imap_success": True,
        "is_smtp_success": True,
    }

    assert _derive_warmup_status(account) == "paused"


def test_derive_health_status_marks_unhealthy_mailbox():
    account = {
        "is_imap_success": False,
        "is_smtp_success": True,
    }

    assert _derive_health_status(account) == "degraded"
