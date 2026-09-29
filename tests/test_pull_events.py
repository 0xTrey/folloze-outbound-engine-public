"""
Tests for reply loop state mapping in pull_events.py.
"""
import sys
import os
import types
from unittest.mock import patch
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

sys.modules.setdefault("llm_gateway", types.SimpleNamespace(LLMGateway=object))

from jobs.pull_events import _loop_status, _notify_interested


def test_loop_status_for_bounce():
    assert _loop_status("BOUNCED") == "disqualified"


def test_loop_status_for_interested_reply():
    assert _loop_status("REPLIED", "interested") == "interested"


def test_loop_status_for_objection_reply():
    assert _loop_status("REPLIED", "objection") == "not_now"


def test_loop_status_for_referral_reply():
    assert _loop_status("REPLIED", "referral") == "reengage_candidate"


def test_notify_interested_uses_shared_notify():
    lead = {
        "first_name": "Jane",
        "last_name": "Doe",
        "email": "jane@example.com",
        "company_name": "Acme",
    }

    with patch("jobs.pull_events.notify") as mock_notify:
        _notify_interested(lead, "cmp_123")

    mock_notify.assert_called_once()
    subject, body = mock_notify.call_args.args
    assert subject == "Interested reply"
    assert "Jane Doe <jane@example.com> @ Acme" in body
    assert "Campaign: cmp_123" in body
