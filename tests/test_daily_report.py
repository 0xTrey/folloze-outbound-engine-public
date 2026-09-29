"""
Tests for pipeline health reporting in daily_report.py.
"""
import os
import sys
from datetime import date
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from jobs.daily_report import _format_html, _get_pipeline_health, _send_report


def test_get_pipeline_health_success_with_zero_enrollment(tmp_path):
    log_path = tmp_path / "pipeline_2026-03-24.log"
    log_path.write_text(
        "\n".join([
            "[2026-03-24 07:03:00] daily_outbound: starting",
            "[2026-03-24 07:03:01] Step 1: sync_accounts (4 pages = ~100 accounts)",
            "[2026-03-24 07:04:30] Step 5: assign_push (batch=100)",
            "[2026-03-24 07:05:12] [INFO] [assign_push] Done. pushed=0 failed=0",
            "[2026-03-24 07:05:13] WARNING: Zero enrollments detected today. Pipeline ran but no contacts were pushed to Smartlead.",
            "[2026-03-24 07:05:15] daily_outbound: done",
        ])
    )

    health = _get_pipeline_health(date(2026, 3, 24), log_dir=tmp_path)

    assert health["status"] == "success"
    assert health["last_step"] == "assign_push"
    assert health["zero_enrollment_detected"] is True
    assert health["warning_count"] == 1
    assert "Zero enrollments detected today" in health["warnings"][0]


def test_get_pipeline_health_missing_log_is_failure(tmp_path):
    health = _get_pipeline_health(date(2026, 3, 24), log_dir=tmp_path)

    assert health["status"] == "failure"
    assert health["last_step"] == "not available"
    assert health["warning_count"] == 1
    assert "Log file not found" in health["warnings"][0]


def test_format_html_includes_pipeline_health_section():
    db = {
        "enrolled_yesterday": 3,
        "followup_active": 7,
        "in_sequence": {"MM_REVMKTG_TIMING": 5},
        "total_active": 5,
        "pipeline": {"pending": 2},
        "totals": {
            "total_sent": 10,
            "total_replies": 2,
            "total_interested": 1,
            "total_bounced": 0,
        },
        "suppressed_domains": 4,
    }
    health = {
        "run_date": date(2026, 3, 24),
        "log_name": "pipeline_2026-03-24.log",
        "status": "success",
        "last_step": "assign_push",
        "warnings": ["WARNING: Zero enrollments detected today."],
        "errors": [],
        "warning_count": 1,
        "error_count": 0,
        "zero_enrollment_detected": True,
    }

    report_html = _format_html(db, [], health)

    assert "Pipeline Health" in report_html
    assert "assign_push" in report_html
    assert "Zero-enrollment detected" in report_html
    assert "WARNING: Zero enrollments detected today." in report_html


def test_send_report_uses_shared_notify():
    with patch("jobs.daily_report.notify") as mock_notify:
        _send_report("Subject", "<p>email</p>", "discord body", dry_run=False)

    mock_notify.assert_called_once_with(
        "Subject",
        "discord body",
        email_html="<p>email</p>",
    )
