import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from jobs.setup_campaigns import (  # noqa: E402
    _is_assignable_email_account,
    _sync_campaign_email_accounts,
)


def test_assignable_email_account_accepts_active_warmup():
    account = {
        "id": 1,
        "from_email": "sender@example.com",
        "warmup_details": {"status": "ACTIVE"},
        "is_imap_success": False,
        "is_smtp_success": False,
    }

    assert _is_assignable_email_account(account) is True


def test_assignable_email_account_accepts_connected_mailbox_without_warmup_details():
    account = {
        "id": 2,
        "from_email": "sender@example.com",
        "warmup_details": None,
        "is_imap_success": True,
        "is_smtp_success": True,
    }

    assert _is_assignable_email_account(account) is True


def test_assignable_email_account_rejects_unhealthy_mailbox():
    account = {
        "id": 3,
        "from_email": "sender@example.com",
        "warmup_details": None,
        "is_imap_success": True,
        "is_smtp_success": False,
    }

    assert _is_assignable_email_account(account) is False


def test_sync_campaign_email_accounts_is_noop_when_already_aligned():
    class FakeSmartlead:
        def get_campaign_email_accounts(self, campaign_id):
            return [{"id": 10, "from_email": "a@example.com"}]

        def assign_email_accounts_to_campaign(self, campaign_id, email_account_ids):
            raise AssertionError("Should not assign when already aligned")

    result = _sync_campaign_email_accounts(
        FakeSmartlead(),
        "123",
        [{"id": 10, "from_email": "a@example.com"}],
        dry_run=False,
    )

    assert result["status"] == "unchanged"


def test_sync_campaign_email_accounts_reports_dry_run_update():
    class FakeSmartlead:
        def get_campaign_email_accounts(self, campaign_id):
            return [{"id": 10, "from_email": "a@example.com"}]

        def assign_email_accounts_to_campaign(self, campaign_id, email_account_ids):
            raise AssertionError("Dry run should not assign")

    result = _sync_campaign_email_accounts(
        FakeSmartlead(),
        "123",
        [{"id": 10, "from_email": "a@example.com"}, {"id": 11, "from_email": "b@example.com"}],
        dry_run=True,
    )

    assert result["status"] == "would_update"
    assert result["desired_ids"] == [10, 11]
