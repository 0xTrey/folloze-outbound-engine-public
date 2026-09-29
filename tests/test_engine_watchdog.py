import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from jobs import engine_watchdog as watchdog  # noqa: E402


def test_evaluate_pipeline_log_success_detects_completed_run(tmp_path, monkeypatch):
    repo_root = tmp_path
    log_dir = repo_root / "logs"
    log_dir.mkdir(parents=True)
    target_date = "2026-04-02"
    log_path = log_dir / f"pipeline_{target_date}.log"
    log_path.write_text(
        "\n".join(
            [
                "[2026-04-02 07:03:00] daily_outbound: starting",
                "[2026-04-02 07:10:00] Step 5: assign_push (batch=100)",
                "[2026-04-02 07:11:00] daily_outbound: done",
            ]
        )
    )
    monkeypatch.setattr(watchdog, "REPO_ROOT", repo_root)

    result = watchdog._evaluate_pipeline_log_success(
        {
            "success_signal": {
                "kind": "pipeline_log",
                "max_age_minutes": 1560,
                "log_path": "logs/pipeline_{target_date}.log",
                "success_marker": "daily_outbound: done",
            }
        },
        now_local=datetime(2026, 4, 2, 12, 0, tzinfo=watchdog.LOCAL_TZ),
    )

    assert result["passed"] is True
    assert result["path"] == f"logs/pipeline_{target_date}.log"


def test_classify_job_failure_provider_failure():
    category, fix = watchdog._classify_job_failure(
        {
            "job_name": "pull_events",
            "success_signal": {"kind": "job_run"},
            "diagnostics": {"default_category": "no_run", "default_recommended_fix": "check logs"},
        },
        {"passed": False},
        [
            {
                "status": "failed",
                "error_type": "RuntimeError",
                "error_message": "Smartlead API timed out after 45 seconds",
                "summary": None,
            }
        ],
        False,
    )

    assert category == "provider_failure"
    assert "provider" in fix.lower()


def test_should_retry_requires_safe_job_and_no_active_process():
    result = {
        "status": "failed",
        "diagnosis_category": "provider_failure",
        "safe_to_retry": True,
    }
    spec = {
        "recovery": {
            "enabled": True,
            "retry_allowed_when": {"idempotent_or_resumable": True},
        }
    }

    assert watchdog._should_retry(result, spec, auto_retry_safe=True) is True

    result["diagnosis_category"] = "active_run"
    assert watchdog._should_retry(result, spec, auto_retry_safe=True) is False


def test_classify_pipeline_log_failure_prefers_provider_root_cause():
    category, fix = watchdog._classify_job_failure(
        {
            "job_name": "daily_outbound",
            "success_signal": {"kind": "pipeline_log"},
            "diagnostics": {"default_category": "no_run", "default_recommended_fix": "check logs"},
        },
        {
            "passed": False,
            "started": True,
            "completed": False,
            "last_lines": [
                "DomainNotFoundError: Parsing --target-org",
                "MyDomainResolverTimeoutError: The client has timed out.",
                "SyntaxError: invalid syntax.",
            ],
        },
        [],
        False,
    )

    assert category == "provider_failure"
    assert "provider" in fix.lower() or "dependency" in fix.lower()


def test_classify_pipeline_log_failure_beats_active_process_when_log_has_traceback():
    category, fix = watchdog._classify_job_failure(
        {
            "job_name": "daily_outbound",
            "success_signal": {"kind": "pipeline_log"},
            "diagnostics": {"default_category": "no_run", "default_recommended_fix": "check logs"},
        },
        {
            "passed": False,
            "started": True,
            "completed": False,
            "last_lines": [
                "Traceback (most recent call last):",
                "  File \"jobs/reveal_emails.py\", line 108, in run",
                "AttributeError: 'NoneType' object has no attribute 'get'",
            ],
        },
        [],
        True,
    )

    assert category == "application_failure"
    assert "crashing step" in fix.lower()


def test_classify_pipeline_log_failure_uses_active_run_when_no_explicit_error():
    category, fix = watchdog._classify_job_failure(
        {
            "job_name": "daily_outbound",
            "success_signal": {"kind": "pipeline_log"},
            "diagnostics": {"default_category": "no_run", "default_recommended_fix": "check logs"},
        },
        {
            "passed": False,
            "started": True,
            "completed": False,
            "last_lines": [
                "[2026-04-03 10:07:05] daily_outbound: starting",
                "[2026-04-03 10:07:39] Step 2: reveal_emails",
            ],
        },
        [],
        True,
    )

    assert category == "active_run"
    assert "still appears to be running" in fix.lower()


def test_pgrep_active_ignores_observer_commands(monkeypatch):
    class Completed:
        stdout = "\n".join(
            [
                "100 bash /tmp/folloze-outbound-engine/scripts/daily_outbound.sh",
                "101 git -C /tmp/folloze-outbound-engine diff -- scripts/daily_outbound.sh",
            ]
        )

    monkeypatch.setattr(watchdog.os, "getpid", lambda: 100)
    monkeypatch.setattr(
        watchdog.subprocess,
        "run",
        lambda *args, **kwargs: Completed(),
    )

    assert watchdog._pgrep_active("scripts/daily_outbound.sh") is False


def test_latest_success_within_ignores_dry_run_success():
    runs = [
        {
            "status": "success",
            "finished_at": datetime.now(watchdog.timezone.utc),
            "started_at": datetime.now(watchdog.timezone.utc),
            "context_json": {"dry_run": True},
        }
    ]

    assert watchdog._latest_success_within(runs, max_age_minutes=90) is None


def test_check_metric_thresholds_flags_below_minimum():
    class FakeCursor:
        def execute(self, sql):
            self.sql = sql

        def fetchone(self):
            return {"metric_value": 0.1}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_val, exc_tb):
            return False

    class FakeConn:
        def cursor(self, cursor_factory=None):
            return FakeCursor()

    results = watchdog._check_metric_thresholds(
        FakeConn(),
        {
            "metric_checks": [
                {
                    "name": "outbound_brief_pass_rate_3d",
                    "sql": "SELECT 0.1 AS metric_value",
                    "min_value": 0.2,
                    "soft": True,
                }
            ]
        },
    )

    assert len(results) == 1
    assert results[0]["name"] == "outbound_brief_pass_rate_3d"
    assert results[0]["status"] == "warning"
    assert "below the minimum" in results[0]["message"]


def test_aggregate_status_fails_on_hard_check_failures():
    status = watchdog._aggregate_status(
        [{"job_name": "pull_events", "status": "passed"}],
        [{"name": "salesforce_folloze_signal_events", "status": "failed", "soft": False}],
    )

    assert status == "failed"
