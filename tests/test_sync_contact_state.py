"""
Tests for contact state aggregation and lifecycle derivation.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from jobs.sync_contact_state import _aggregate_stat_row, _build_snapshot


def test_aggregate_stat_row_maps_sequence_id_to_step():
    aggregated = {}
    row = {
        "lead_email": "person@example.com",
        "email_campaign_seq_id": 6619232,
        "email_subject": "",
        "sent_time": "2026-03-12T13:37:15.701Z",
        "open_time": "2026-03-12T14:00:00.000Z",
        "click_time": None,
        "reply_time": None,
        "open_count": 1,
        "click_count": 0,
        "is_bounced": False,
        "is_unsubscribed": False,
        "sequence_number": None,
    }
    sequence_map = {
        6619232: {"seq_number": 2, "subject": ""},
    }

    _aggregate_stat_row(aggregated, row, sequence_map)

    result = aggregated["person@example.com"]
    assert result["current_step_number"] == 2
    assert result["delivery_status"] == "delivered"
    assert result["last_opened_at"].isoformat().startswith("2026-03-12T14:00:00")


def test_build_snapshot_marks_ready_to_push():
    contact = {
        "contact_id": "contact-1",
        "assignment_id": "assign-1",
        "email": "person@example.com",
        "company_name": "ExampleCo",
        "sequence_family": "MM_REVMKTG_TIMING",
        "eligibility_status": "pending",
        "payload_status": "approved",
        "has_research": True,
        "created_at": "2026-03-12T12:00:00Z",
        "last_enriched_at": "2026-03-12T12:10:00Z",
    }

    snapshot = _build_snapshot(contact, {})

    assert snapshot["current_activity"] == "ready_to_push"
    assert snapshot["research_status"] == "present"
    assert snapshot["last_activity_at"].isoformat().startswith("2026-03-12T12:10:00")


def test_build_snapshot_marks_sequence_completed():
    contact = {
        "contact_id": "contact-2",
        "assignment_id": "assign-2",
        "email": "person@example.com",
        "company_name": "ExampleCo",
        "sequence_family": "MM_REVMKTG_TIMING",
        "eligibility_status": "sent",
        "payload_status": "approved",
        "has_research": True,
        "created_at": "2026-03-12T12:00:00Z",
        "last_send_sent_at": "2026-03-14T09:00:00Z",
        "last_send_delivery_status": "delivered",
    }
    live = {
        "smartlead_campaign_id": "1005",
        "smartlead_campaign_status": "PAUSED",
        "smartlead_lead_status": "COMPLETED",
        "current_step_number": 2,
        "current_step_subject": "",
        "last_sent_at": "2026-03-15T10:00:00Z",
    }

    snapshot = _build_snapshot(contact, live)

    assert snapshot["current_activity"] == "sequence_completed"
    assert snapshot["activity_detail"] == "step 2"
    assert snapshot["current_step_number"] == 2
    assert snapshot["last_sent_at"].isoformat().startswith("2026-03-15T10:00:00")


def test_build_snapshot_marks_inprogress_as_in_sequence():
    contact = {
        "contact_id": "contact-2b",
        "assignment_id": "assign-2b",
        "email": "person@example.com",
        "company_name": "ExampleCo",
        "sequence_family": "MM_REVMKTG_TIMING",
        "eligibility_status": "sent",
        "payload_status": "approved",
        "has_research": True,
        "created_at": "2026-03-12T12:00:00Z",
    }
    live = {
        "smartlead_campaign_id": "1005",
        "smartlead_campaign_status": "PAUSED",
        "smartlead_lead_status": "INPROGRESS",
        "current_step_number": 1,
        "last_sent_at": "2026-03-15T10:00:00Z",
    }

    snapshot = _build_snapshot(contact, live)

    assert snapshot["current_activity"] == "in_sequence"
    assert snapshot["activity_detail"] == "step 1"


def test_build_snapshot_marks_replied_status():
    contact = {
        "contact_id": "contact-3",
        "assignment_id": "assign-3",
        "email": "person@example.com",
        "company_name": "ExampleCo",
        "sequence_family": "MM_REVMKTG_TIMING",
        "eligibility_status": "sent",
        "payload_status": "approved",
        "has_research": True,
        "created_at": "2026-03-12T12:00:00Z",
        "last_send_reply_type": "interested",
        "last_send_delivery_status": "delivered",
    }

    snapshot = _build_snapshot(contact, {})

    assert snapshot["current_activity"] == "replied_interested"
    assert snapshot["reply_type"] == "interested"
