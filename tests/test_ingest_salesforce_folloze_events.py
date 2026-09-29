import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from jobs.ingest_salesforce_folloze_events import (  # noqa: E402
    _build_query,
    _normalize_task_record,
    _parse_sf_datetime,
)


def test_parse_sf_datetime_converts_to_utc():
    parsed = _parse_sf_datetime("2026-04-02T21:39:32.000+0000")
    assert parsed == datetime(2026, 4, 2, 21, 39, 32, tzinfo=timezone.utc)


def test_normalize_task_record_for_board_view_clears_content_id():
    task = {
        "Id": "00T-board",
        "Subject": "[Folloze] board_view",
        "CreatedDate": "2026-04-02T21:39:32.000+0000",
        "WhoId": "00Q-lead",
        "Who": {"Type": "Lead", "Name": "Pat Example", "Email": None},
        "Description": "Board name: Example Board",
        "Folloze_Asset_ID__c": "238181",
        "Folloze_Asset_Name__c": "Board name: Example Board",
        "Folloze_Board_ID__c": "238181",
        "Folloze_Duration_seconds__c": None,
        "Folloze_UTM_Campaign__c": "GTME_VIBE_MKT",
        "Folloze_UTM_Source__c": "smartlead",
        "Folloze_UTM_Medium__c": "email",
        "attributes": {"url": "/services/data/v66.0/sobjects/Task/00T-board"},
    }

    event = _normalize_task_record(task)

    assert event["external_event_id"] == "00T-board"
    assert event["event_type"] == "board_view"
    assert event["board_id"] == "238181"
    assert event["content_id"] is None
    assert event["crm_person_id"] == "00Q-lead"
    assert event["crm_person_type"] == "Lead"
    assert event["campaign_id"] == "GTME_VIBE_MKT"
    assert event["url"] == "/services/data/v66.0/sobjects/Task/00T-board"


def test_normalize_task_record_for_item_view_keeps_content_id_and_duration():
    task = {
        "Id": "00T-item",
        "Subject": "[Folloze] item_view",
        "CreatedDate": "2026-03-23T09:21:11.000+0000",
        "WhoId": "003-contact",
        "Who": {"Type": "Contact", "Name": "Pat Example", "Email": None},
        "Description": "Viewed item: Build a 1:Many Account Campaign",
        "Folloze_Asset_ID__c": "2211726",
        "Folloze_Asset_Name__c": "Build a 1:Many Account Campaign — Campaign Agent",
        "Folloze_Board_ID__c": "238181",
        "Folloze_Duration_seconds__c": "72",
        "Folloze_UTM_Campaign__c": "GTME_VIBE_MKT",
        "Folloze_UTM_Source__c": "smartlead",
        "Folloze_UTM_Medium__c": "email",
        "attributes": {"url": "/services/data/v66.0/sobjects/Task/00T-item"},
    }

    event = _normalize_task_record(task)

    assert event["event_type"] == "item_view"
    assert event["board_id"] == "238181"
    assert event["content_id"] == "2211726"
    assert event["duration_seconds"] == 72
    assert event["payload_json"]["asset_name"] == "Build a 1:Many Account Campaign — Campaign Agent"


def test_build_query_uses_compound_cursor_when_present():
    query = _build_query(
        cursor_created_at=datetime(2026, 4, 2, 21, 39, 32, tzinfo=timezone.utc),
        cursor_task_id="task-cursor-001",
        fallback_since=datetime(2026, 4, 1, 0, 0, tzinfo=timezone.utc),
        limit=200,
    )

    assert "CreatedDate > 2026-04-02T21:39:32Z" in query
    assert "CreatedDate = 2026-04-02T21:39:32Z AND Id > 'task-cursor-001'" in query
    assert "ORDER BY CreatedDate ASC, Id ASC" in query
    assert "LIMIT 200" in query


def test_build_query_uses_fallback_window_without_cursor():
    query = _build_query(
        cursor_created_at=None,
        cursor_task_id=None,
        fallback_since=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc),
        limit=50,
    )

    assert "CreatedDate >= 2026-03-01T12:00:00Z" in query
    assert "LIMIT 50" in query
