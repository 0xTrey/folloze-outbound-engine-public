import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from jobs.ingest_folloze_people_csv import (  # noqa: E402
    _normalize_snapshot_row,
    _parse_folloze_ts,
    _parse_report_updated_at,
    _validate_report_scope,
)


def test_parse_report_updated_at():
    value = _parse_report_updated_at(
        "People Engagement Report Updated At: 2026-04-02 12:00:00, Date Range: 2026-01-02 - 2026-04-02"
    )
    assert value == datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone.utc)


def test_parse_folloze_ts():
    value = _parse_folloze_ts("2026-03-23 15:08:44 UTC")
    assert value == datetime(2026, 3, 23, 15, 8, 44, tzinfo=timezone.utc)


def test_normalize_snapshot_row_links_contact_and_metrics():
    report_updated_at = datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone.utc)
    row = {
        "Lead Id": "158124965",
        "Email": "person@example.com",
        "First Name": "Pat",
        "Last Name": "Example",
        "Role": "GTM Engineer",
        "Lead Status": "Visitor",
        "Lead Event Status": "Not Registered",
        "Company": "ExampleCo",
        "Domain": "example.com",
        "Board Id": "238181",
        "Board Name": "Example outbound board",
        "Last Utm Source": "smartlead",
        "Last Utm Medium": "email",
        "Last Utm Campaign": "GTME_VIBE_MKT",
        "Last Invitation On": "",
        "Joined Board On": "2026-03-18 15:04:59 UTC",
        "Last Action On": "2026-03-18 15:08:44 UTC",
        "Number Of Board Views": "3",
        "Number Of Items Viewed": "0",
        "Number Of Forms Filled": "0",
        "Number Of Links Clicked": "0",
        "Number Of Messages Sent": "0",
        "Number Of Request To Contact": "0",
        "Number Of Cta Clicks": "0",
        "Number Of Downloads": "0",
        "Total View Time": "245",
        "Number Of Days Visited": "1",
        "Number Of Emailed To Clicked": "0",
        "Most Viewed Item": "Build a Persona-Based Nurture Campaign — Campaign Agent",
        "Email Delivered (Y/N)?": "1",
        "Email Opened (Y/N)?": "0",
        "External Invitation (Y/N)?": "0",
        "Item Viewer (Y/N)?": "0",
        "Cta Clicker (Y/N)?": "0",
    }

    event = _normalize_snapshot_row(
        row,
        report_updated_at=report_updated_at,
        local_contact={"contact_id": "contact-1", "account_id": "account-1"},
        local_account_id=None,
        report_metadata={
            "source_file_name": "people.csv",
            "source_file_path": "/tmp/people.csv",
            "source_file_sha256": "abc123",
            "validated_board_id": "238181",
            "validated_campaign_id": "GTME_VIBE_MKT",
        },
    )

    assert event["event_type"] == "engagement_snapshot"
    assert event["contact_id"] == "contact-1"
    assert event["account_id"] == "account-1"
    assert event["board_id"] == "238181"
    assert event["campaign_id"] == "GTME_VIBE_MKT"
    assert event["duration_seconds"] == 245
    assert event["event_timestamp_utc"] == datetime(2026, 3, 18, 15, 8, 44, tzinfo=timezone.utc)
    assert event["payload_json"]["most_viewed_item"] == "Build a Persona-Based Nurture Campaign — Campaign Agent"
    assert event["payload_json"]["source_file_name"] == "people.csv"
    assert event["payload_json"]["source_file_sha256"] == "abc123"
    assert event["payload_json"]["validated_campaign_id"] == "GTME_VIBE_MKT"


def test_validate_report_scope_rejects_mixed_board_ids(tmp_path):
    report_path = tmp_path / "People Engagement.csv"
    report_path.write_text("irrelevant")
    rows = [
        {"Board Id": "238181", "Last Utm Campaign": "GTME_VIBE_MKT"},
        {"Board Id": "999999", "Last Utm Campaign": "GTME_VIBE_MKT"},
    ]

    try:
        _validate_report_scope(
            rows,
            report_updated_at=datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone.utc),
            report_path=report_path,
        )
    except ValueError as exc:
        assert "mixed-board" in str(exc)
    else:
        raise AssertionError("Expected mixed-board report validation to fail")


def test_validate_report_scope_records_lineage_and_staleness(tmp_path):
    report_path = tmp_path / "People Engagement.csv"
    report_path.write_text("hello world")
    report_updated_at = datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone.utc)
    metadata = _validate_report_scope(
        [{"Board Id": "238181", "Last Utm Campaign": "GTME_VIBE_MKT"}],
        report_updated_at=report_updated_at,
        report_path=report_path,
        expected_board_id="238181",
        expected_campaign="GTME_VIBE_MKT",
        max_report_age_hours=72,
        now_utc=datetime(2026, 4, 3, 12, 0, 0, tzinfo=timezone.utc),
    )

    assert metadata["validated_board_id"] == "238181"
    assert metadata["validated_campaign_id"] == "GTME_VIBE_MKT"
    assert metadata["source_file_name"] == "People Engagement.csv"
    assert metadata["source_file_path"] == str(Path(report_path).resolve())
    assert metadata["source_file_sha256"]

    try:
        _validate_report_scope(
            [{"Board Id": "238181", "Last Utm Campaign": "GTME_VIBE_MKT"}],
            report_updated_at=report_updated_at,
            report_path=report_path,
            max_report_age_hours=12,
            now_utc=datetime(2026, 4, 3, 12, 0, 1, tzinfo=timezone.utc),
        )
    except ValueError as exc:
        assert "max_report_age_hours" in str(exc)
    else:
        raise AssertionError("Expected stale report validation to fail")
