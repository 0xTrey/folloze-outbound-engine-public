import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from jobs.assign_push import (  # noqa: E402
    _assess_mailbox_readiness,
    _ensure_campaigns_launch_ready,
)


def test_assess_mailbox_readiness_blocks_fresh_mailbox():
    now = datetime(2026, 4, 12, tzinfo=timezone.utc)
    account = {
        "id": 1,
        "from_email": "fresh@example.com",
        "created_at": (now - timedelta(days=1)).isoformat().replace("+00:00", "Z"),
        "is_imap_success": True,
        "is_smtp_success": True,
    }
    stats = {"sent_count": 0, "warmup_email_received_count": 0}

    ready, reason = _assess_mailbox_readiness(account, stats, now=now)

    assert ready is False
    assert "earliest safe date" in reason


def test_assess_mailbox_readiness_accepts_old_warmed_mailbox():
    now = datetime(2026, 4, 12, tzinfo=timezone.utc)
    account = {
        "id": 2,
        "from_email": "warmed@example.com",
        "created_at": (now - timedelta(days=20)).isoformat().replace("+00:00", "Z"),
        "is_imap_success": True,
        "is_smtp_success": True,
    }
    stats = {"sent_count": 15, "warmup_email_received_count": 8}

    ready, reason = _assess_mailbox_readiness(account, stats, now=now)

    assert ready is True
    assert reason is None


def test_ensure_campaigns_launch_ready_rejects_paused_campaign():
    class FakeSmartlead:
        def get_campaign(self, campaign_id):
            return {"id": campaign_id, "status": "PAUSED"}

        def get_campaign_email_accounts(self, campaign_id):
            return [{"id": 1}]

        def get_email_account(self, account_id):
            return {
                "id": int(account_id),
                "from_email": "sender@example.com",
                "created_at": "2026-03-01T00:00:00Z",
                "is_imap_success": True,
                "is_smtp_success": True,
            }

        def get_email_account_warmup_stats(self, account_id):
            return {"sent_count": 30, "warmup_email_received_count": 10}

    try:
        _ensure_campaigns_launch_ready(FakeSmartlead(), {"GTME_VIBE_MKT": "1001"})
    except RuntimeError as exc:
        message = str(exc)
    else:
        raise AssertionError("Expected launch readiness failure for paused campaign")

    assert "GTME_VIBE_MKT" in message
    assert "PAUSED" in message
