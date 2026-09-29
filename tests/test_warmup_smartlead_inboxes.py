import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from jobs.warmup_smartlead_inboxes import (  # noqa: E402
    _is_connected_mailbox,
    _matches_mailbox_filters,
)


def test_is_connected_mailbox_requires_imap_and_smtp():
    assert _is_connected_mailbox({"is_imap_success": True, "is_smtp_success": True}) is True
    assert _is_connected_mailbox({"is_imap_success": True, "is_smtp_success": False}) is False


def test_matches_mailbox_filters_accepts_email_and_id():
    account = {
        "id": 17754838,
        "from_email": "sender@example.com",
    }

    assert _matches_mailbox_filters(account, ["sender@example.com"]) is True
    assert _matches_mailbox_filters(account, ["17754838"]) is True
    assert _matches_mailbox_filters(account, ["other@example.com"]) is False
