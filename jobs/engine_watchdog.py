"""
Job: engine_watchdog

Read the engine canary spec, verify scheduled-job freshness, classify failures,
optionally retry only safe jobs, and write an aggregate incident report.

Usage:
    python main.py engine_watchdog
    python main.py engine_watchdog --dry-run
    python main.py engine_watchdog --job pull_events --auto-retry-safe
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    import psycopg2.extras as psycopg2_extras
except ImportError:
    psycopg2_extras = None

from db.connection import db_conn
from runtime_config import (
    REPO_ROOT,
    configure_logging,
    get_job_log_path,
    notify,
    record_job_incident,
)

logger = logging.getLogger(__name__)

SPEC_PATH = REPO_ROOT / "ops" / "engine-watchdog-spec.json"
LOCAL_TZ = ZoneInfo("America/Chicago")
PROVIDER_KEYWORDS = (
    "apollo",
    "smartlead",
    "salesforce",
    "perplexity",
    "brave",
    "openclaw",
    "connection already closed",
    "timed out",
    "timeout",
    "rate limit",
    "429",
    "503",
    "502",
    "security find-generic-password",
)
QUALITY_KEYWORDS = (
    "validation",
    "qa",
    "schema",
    "empty",
    "placeholder",
    "failed gate",
)
ALERTING_KEYWORDS = (
    "syntaxerror",
    "traceback",
    "notify(",
)
PIPELINE_FAILURE_MARKERS = (
    "traceback",
    "exception",
    "attributeerror",
    "typeerror",
    "valueerror",
    "runtimeerror",
    "keyerror",
    "error: step '",
    "run failed",
)
PROCESS_OBSERVER_PREFIXES = (
    "git ",
    "rg ",
    "grep ",
    "sed ",
    "awk ",
    "cat ",
    "tail ",
)


class EngineWatchdogFailure(RuntimeError):
    pass


def _now_local() -> datetime:
    return datetime.now(timezone.utc).astimezone(LOCAL_TZ)


def _load_spec() -> dict[str, Any]:
    return json.loads(SPEC_PATH.read_text())


def _tail_lines(path: Path, limit: int = 20) -> list[str]:
    if not path.exists():
        return []
    return path.read_text(errors="replace").splitlines()[-limit:]


def _render_template(value: str, *, target_date: str) -> str:
    return value.replace("{target_date}", target_date)


def _pgrep_active(process_match: str | None) -> bool:
    if not process_match:
        return False
    try:
        completed = subprocess.run(
            ["pgrep", "-fl", process_match],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False

    current_pid = str(os.getpid())
    for line in completed.stdout.splitlines():
        pid, _, command = line.partition(" ")
        if not pid or pid == current_pid or process_match not in command:
            continue
        lowered = command.lower()
        if lowered.startswith(PROCESS_OBSERVER_PREFIXES):
            continue
        if "pgrep -fl" in lowered:
            continue
        return True
    return False


def _load_recent_job_runs(conn, job_name: str, limit: int = 5) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2_extras.RealDictCursor if psycopg2_extras else None) as cur:
        cur.execute(
            """
            SELECT
                job_name,
                run_id,
                status,
                trigger_source,
                command,
                log_path,
                started_at,
                finished_at,
                duration_seconds,
                error_type,
                error_message,
                summary,
                metrics_json,
                context_json
            FROM job_runs
            WHERE job_name = %s
            ORDER BY started_at DESC
            LIMIT %s
            """,
            (job_name, limit),
        )
        return [dict(row) for row in cur.fetchall()]


def _latest_success_within(runs: list[dict[str, Any]], *, max_age_minutes: int) -> dict[str, Any] | None:
    now_utc = datetime.now(timezone.utc)
    for run in runs:
        if run.get("status") != "success":
            continue
        if _is_dry_run_run(run):
            continue
        finished_at = run.get("finished_at") or run.get("started_at")
        if not finished_at:
            continue
        if (now_utc - finished_at).total_seconds() <= max_age_minutes * 60:
            return run
    return None


def _is_dry_run_run(run: dict[str, Any]) -> bool:
    context = run.get("context_json") or {}
    if isinstance(context, str):
        try:
            context = json.loads(context)
        except json.JSONDecodeError:
            context = {}
    return bool((context or {}).get("dry_run"))


def _pipeline_log_has_explicit_failure(blob: str) -> bool:
    return any(marker in blob for marker in PIPELINE_FAILURE_MARKERS)


def _classify_pipeline_log_failure(
    spec: dict[str, Any],
    success: dict[str, Any],
    *,
    active_process: bool,
) -> tuple[str, str]:
    blob = "\n".join(success.get("last_lines") or []).lower()
    if any(keyword in blob for keyword in QUALITY_KEYWORDS):
        return (
            "quality_or_validation_failure",
            "The pipeline log shows a validation or QA-shaped failure. Fix the failing gate before retrying.",
        )
    if any(keyword in blob for keyword in PROVIDER_KEYWORDS):
        return (
            "provider_failure",
            "The pipeline log points to an upstream dependency or credential failure. Check provider health, auth, and network reachability before retrying.",
        )
    if _pipeline_log_has_explicit_failure(blob):
        return (
            "application_failure",
            "The pipeline log shows an explicit step failure. Fix the crashing step before retrying the pipeline.",
        )
    if any(keyword in blob for keyword in ALERTING_KEYWORDS):
        return (
            "alerting_failure",
            "The pipeline failure notifier also failed. Repair the alert path so the next pipeline error is reported cleanly.",
        )
    if success.get("started") and not success.get("completed"):
        if active_process:
            return (
                "active_run",
                "Expected output is missing, but the original job still appears to be running. Wait for it to finish before retrying.",
            )
        return (
            "interrupted_pipeline",
            "The daily pipeline log shows a start without a completion marker. Inspect the last pipeline step and the dated log before manually resuming.",
        )
    return (
        spec.get("diagnostics", {}).get("default_category", "no_run"),
        spec.get("diagnostics", {}).get(
            "default_recommended_fix",
            "Inspect the scheduler definition, recent logs, and dependency health.",
        ),
    )


def _evaluate_pipeline_log_success(spec: dict[str, Any], *, now_local: datetime) -> dict[str, Any]:
    signal = spec["success_signal"]
    max_age = int(signal["max_age_minutes"])
    success_marker = signal["success_marker"]
    warning_marker = signal.get("warning_marker")
    tail_limit = int(signal.get("tail_lines", 25))
    candidates = [now_local.date() - timedelta(days=offset) for offset in range(0, 3)]

    for candidate in candidates:
        target_date = candidate.isoformat()
        path = REPO_ROOT / _render_template(signal["log_path"], target_date=target_date)
        if not path.exists():
            continue
        modified_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        if (datetime.now(timezone.utc) - modified_at).total_seconds() > max_age * 60:
            continue
        lines = path.read_text(errors="replace").splitlines()
        completed = any(success_marker in line for line in lines)
        started = any("daily_outbound: starting" in line for line in lines)
        if completed:
            return {
                "passed": True,
                "target_date": target_date,
                "path": str(path.relative_to(REPO_ROOT)),
                "warning_detected": bool(warning_marker and any(warning_marker in line for line in lines)),
                "last_lines": lines[-tail_limit:],
            }
        return {
            "passed": False,
            "target_date": target_date,
            "path": str(path.relative_to(REPO_ROOT)),
            "started": started,
            "completed": completed,
            "last_lines": lines[-tail_limit:],
        }

    return {
        "passed": False,
        "target_date": now_local.date().isoformat(),
        "path": _render_template(signal["log_path"], target_date=now_local.date().isoformat()),
        "started": False,
        "completed": False,
        "last_lines": [],
    }


def _evaluate_job_run_success(conn, spec: dict[str, Any]) -> dict[str, Any]:
    signal = spec["success_signal"]
    recent_runs = _load_recent_job_runs(conn, spec["job_name"])
    latest_success = _latest_success_within(recent_runs, max_age_minutes=int(signal["max_age_minutes"]))
    if latest_success:
        return {
            "passed": True,
            "latest_success": latest_success,
            "recent_runs": recent_runs,
        }
    return {
        "passed": False,
        "latest_success": None,
        "recent_runs": recent_runs,
    }


def _classify_job_failure(
    spec: dict[str, Any],
    success: dict[str, Any],
    recent_runs: list[dict[str, Any]],
    active_process: bool,
) -> tuple[str, str]:
    if success.get("passed"):
        return "passed", "No action required."
    if spec["success_signal"]["kind"] == "pipeline_log":
        return _classify_pipeline_log_failure(spec, success, active_process=active_process)

    if active_process:
        return (
            "active_run",
            "Expected output is missing, but the original job still appears to be running. Wait for it to finish before retrying.",
        )

    last_run = recent_runs[0] if recent_runs else None
    default_fix = spec.get("diagnostics", {}).get(
        "default_recommended_fix",
        "Inspect the scheduler definition, recent logs, and dependency health.",
    )

    if not last_run:
        return spec.get("diagnostics", {}).get("default_category", "no_run"), default_fix

    if _is_dry_run_run(last_run):
        return (
            "dry_run_only",
            "Only dry-run executions were found in the freshness window. Run the live job to restore production freshness.",
        )

    blob = " ".join(
        str(value or "")
        for value in (
            last_run.get("error_type"),
            last_run.get("error_message"),
            last_run.get("summary"),
        )
    ).lower()
    if any(keyword in blob for keyword in QUALITY_KEYWORDS):
        return (
            "quality_or_validation_failure",
            "The job failed a validation or QA-shaped check. Fix the validation issue before retrying.",
        )
    if any(keyword in blob for keyword in PROVIDER_KEYWORDS):
        return (
            "provider_failure",
            "The failure looks upstream or dependency-related. Check provider health, credentials, and network reachability before retrying.",
        )
    if last_run.get("status") == "running":
        return (
            "active_run",
            "The latest run is still marked running, but no success signal was found. Check for a stuck process or stale job-run record.",
        )
    if last_run.get("status") == "success":
        return (
            "interrupted_pipeline",
            "The run ledger says the job succeeded, but the expected success output is missing. Inspect output publication and log rotation handling.",
        )
    return "unknown_failure", default_fix


def _collect_log_evidence(spec: dict[str, Any], *, target_date: str) -> list[dict[str, Any]]:
    evidence = []
    for raw_path in spec.get("execution_evidence", {}).get("app_logs", []):
        resolved = REPO_ROOT / _render_template(raw_path, target_date=target_date)
        evidence.append(
            {
                "path": str(resolved.relative_to(REPO_ROOT)) if resolved.exists() else str(resolved.relative_to(REPO_ROOT)),
                "exists": resolved.exists(),
                "tail": _tail_lines(resolved, limit=12),
            }
        )
    return evidence


def _check_freshness_checks(conn, spec: dict[str, Any], *, now_utc: datetime) -> list[dict[str, Any]]:
    results = []
    for check in spec.get("freshness_checks", []):
        with conn.cursor(cursor_factory=psycopg2_extras.RealDictCursor if psycopg2_extras else None) as cur:
            cur.execute(check["sql"])
            row = cur.fetchone() or {}
        last_seen = row.get("last_seen")
        if not last_seen:
            results.append(
                {
                    "name": check["name"],
                    "status": "warning" if bool(check.get("soft", False)) else "failed",
                    "message": f"No freshness value found for {check['name']}.",
                    "soft": bool(check.get("soft", False)),
                    "check_type": "freshness",
                }
            )
            continue
        age_minutes = (now_utc - last_seen).total_seconds() / 60
        if age_minutes > float(check["max_age_minutes"]):
            results.append(
                {
                    "name": check["name"],
                    "status": "warning" if bool(check.get("soft", False)) else "failed",
                    "message": f"{check['name']} is stale by {age_minutes:.0f} minutes.",
                    "soft": bool(check.get("soft", False)),
                    "last_seen": last_seen.isoformat(),
                    "check_type": "freshness",
                }
            )
    return results


def _check_metric_thresholds(conn, spec: dict[str, Any]) -> list[dict[str, Any]]:
    results = []
    for check in spec.get("metric_checks", []):
        with conn.cursor(cursor_factory=psycopg2_extras.RealDictCursor if psycopg2_extras else None) as cur:
            cur.execute(check["sql"])
            row = cur.fetchone() or {}
        metric_value = row.get("metric_value")
        if metric_value is None:
            results.append(
                {
                    "name": check["name"],
                    "status": "warning" if bool(check.get("soft", False)) else "failed",
                    "message": f"No metric value found for {check['name']}.",
                    "soft": bool(check.get("soft", False)),
                    "check_type": "metric",
                }
            )
            continue

        metric_numeric = float(metric_value)
        min_value = check.get("min_value")
        max_value = check.get("max_value")
        failed = False
        messages = []
        if min_value is not None and metric_numeric < float(min_value):
            failed = True
            messages.append(f"{metric_numeric:.3f} is below the minimum {float(min_value):.3f}")
        if max_value is not None and metric_numeric > float(max_value):
            failed = True
            messages.append(f"{metric_numeric:.3f} is above the maximum {float(max_value):.3f}")
        if not failed:
            continue

        results.append(
            {
                "name": check["name"],
                "status": "warning" if bool(check.get("soft", False)) else "failed",
                "message": f"{check['name']} threshold violated: {' and '.join(messages)}.",
                "soft": bool(check.get("soft", False)),
                "check_type": "metric",
                "metric_value": metric_numeric,
            }
        )
    return results


def _resolve_retry_command(command: list[str]) -> list[str]:
    resolved = list(command)
    if resolved and resolved[0] == "python3":
        resolved[0] = sys.executable
    return resolved


def _should_retry(result: dict[str, Any], spec: dict[str, Any], *, auto_retry_safe: bool) -> bool:
    recovery = spec.get("recovery", {})
    if not auto_retry_safe:
        return False
    if not recovery.get("enabled"):
        return False
    if result["status"] == "passed":
        return False
    if result["diagnosis_category"] == "active_run":
        return False
    if not result.get("safe_to_retry", False):
        return False
    return True


def _run_retry_command(command: list[str], *, dry_run: bool) -> tuple[bool, str]:
    resolved = _resolve_retry_command(command)
    pretty = " ".join(resolved)
    if dry_run:
        return False, f"[dry-run] would run retry command: {pretty}"
    try:
        subprocess.run(
            resolved,
            cwd=REPO_ROOT,
            check=True,
            capture_output=True,
            text=True,
        )
        return True, f"Retry command succeeded: {pretty}"
    except subprocess.CalledProcessError as exc:
        return False, f"Retry command failed with exit {exc.returncode}: {pretty}\n{exc.stderr.strip()}"


def _evaluate_one_job(conn, spec: dict[str, Any], *, now_local: datetime, auto_retry_safe: bool, dry_run: bool) -> dict[str, Any]:
    kind = spec["success_signal"]["kind"]
    if kind == "pipeline_log":
        success = _evaluate_pipeline_log_success(spec, now_local=now_local)
        recent_runs: list[dict[str, Any]] = []
        target_date = success["target_date"]
    else:
        success = _evaluate_job_run_success(conn, spec)
        recent_runs = success.get("recent_runs") or []
        target_date = now_local.date().isoformat()

    active_process = _pgrep_active(spec.get("execution_evidence", {}).get("process_match"))
    diagnosis_category, recommended_fix = _classify_job_failure(spec, success, recent_runs, active_process)
    result = {
        "job_name": spec["job_name"],
        "status": "passed" if success.get("passed") else "failed",
        "diagnosis_category": diagnosis_category,
        "recommended_fix": recommended_fix,
        "active_process": active_process,
        "target_date": target_date,
        "success": success,
        "recent_runs": recent_runs[:3],
        "log_evidence": _collect_log_evidence(spec, target_date=target_date),
        "actions": [],
        "safe_to_retry": bool(
            spec.get("recovery", {}).get("enabled")
            and not active_process
            and spec.get("recovery", {}).get("retry_allowed_when", {}).get("idempotent_or_resumable", False)
        ),
    }

    if _should_retry(result, spec, auto_retry_safe=auto_retry_safe):
        ok, message = _run_retry_command(spec["recovery"]["retry_command"], dry_run=dry_run)
        result["actions"].append(message)
        if ok:
            rechecked = _evaluate_job_run_success(conn, spec)
            if rechecked.get("passed"):
                result["status"] = "recovered"
                result["diagnosis_category"] = diagnosis_category
                result["success"] = rechecked
                result["recent_runs"] = (rechecked.get("recent_runs") or [])[:3]
            else:
                result["actions"].append("Retry did not restore a fresh success signal.")
    return result


def _aggregate_status(job_results: list[dict[str, Any]], hard_check_failures: list[dict[str, Any]]) -> str:
    if any(result["status"] == "failed" for result in job_results):
        return "failed"
    if hard_check_failures:
        return "failed"
    if any(result["status"] == "recovered" for result in job_results):
        return "recovered"
    return "passed"


def _write_incident_report(report: dict[str, Any], spec: dict[str, Any]) -> dict[str, str]:
    incident_dir = REPO_ROOT / spec["reporting"]["incident_dir"].replace("{target_date}", report["target_date"])
    incident_dir.mkdir(parents=True, exist_ok=True)
    slug = f"{spec['reporting']['incident_slug_prefix']}-{datetime.now():%H-%M-%S}"
    json_path = incident_dir / f"{slug}.json"
    md_path = incident_dir / f"{slug}.md"
    json_path.write_text(json.dumps(report, indent=2, default=str))
    md_path.write_text(_render_incident_markdown(report))
    return {
        "json": str(json_path.relative_to(REPO_ROOT)),
        "markdown": str(md_path.relative_to(REPO_ROOT)),
    }


def _render_incident_markdown(report: dict[str, Any]) -> str:
    job_sections = []
    for result in report["job_results"]:
        actions = "\n".join(f"- {action}" for action in result.get("actions") or []) or "- None"
        recent_runs = "\n".join(
            f"- {run.get('status')} at {run.get('finished_at') or run.get('started_at')} "
            f"(trigger={run.get('trigger_source')}, dry_run={_is_dry_run_run(run)})"
            for run in result.get("recent_runs") or []
        ) or "- None"
        logs = []
        for evidence in result.get("log_evidence") or []:
            tail = "\n".join(f"    {line}" for line in evidence.get("tail") or []) or "    (no tail)"
            logs.append(f"- {evidence['path']} (exists={evidence['exists']})\n{tail}")
        log_block = "\n".join(logs) or "- None"
        job_sections.append(
            f"""## {result['job_name']}

- Status: {result['status']}
- Diagnosis: {result['diagnosis_category']}
- Active process: {result['active_process']}
- Recommended fix: {result['recommended_fix']}

### Actions
{actions}

### Recent Runs
{recent_runs}

### Log Evidence
{log_block}
"""
        )

    hard_failures = "\n".join(
        f"- {item['name']}: {item['message']}" for item in report.get("check_failures") or []
    ) or "- None"
    freshness = "\n".join(
        f"- {item['name']}: {item['message']}" for item in report.get("soft_warnings") or []
    ) or "- None"
    return f"""# Engine Watchdog

- Status: {report['status']}
- Target date: {report['target_date']}
- Generated at: {report['generated_at']}

## Hard Check Failures
{hard_failures}

## Soft Warnings
{freshness}

{chr(10).join(job_sections)}
"""


def _notify_report(report: dict[str, Any], report_paths: dict[str, str]) -> None:
    failing = [item["job_name"] for item in report["job_results"] if item["status"] == "failed"]
    recovered = [item["job_name"] for item in report["job_results"] if item["status"] == "recovered"]
    failing_checks = [item["name"] for item in report.get("check_failures") or []]
    summary = []
    if failing:
        summary.append(f"failed: {', '.join(failing)}")
    if failing_checks:
        summary.append(f"checks: {', '.join(failing_checks)}")
    if recovered:
        summary.append(f"recovered: {', '.join(recovered)}")
    body = "\n".join(
        [
            f"Engine watchdog status: {report['status']}",
            *summary,
            f"Incident markdown: {report_paths['markdown']}",
        ]
    )
    notify(
        f"Engine watchdog {report['status']}",
        body,
    )


def run(*, dry_run: bool = False, auto_retry_safe: bool = False, target_job: str | None = None) -> dict[str, Any]:
    logger = configure_logging("engine_watchdog")
    spec = _load_spec()
    jobs = spec["jobs"]
    if target_job:
        jobs = [job for job in jobs if job["job_name"] == target_job]
        if not jobs:
            raise RuntimeError(f"Unknown watchdog job '{target_job}'")

    now_local = _now_local()
    now_utc = datetime.now(timezone.utc)

    with db_conn() as conn:
        job_results = [
            _evaluate_one_job(
                conn,
                job_spec,
                now_local=now_local,
                auto_retry_safe=auto_retry_safe,
                dry_run=dry_run,
            )
            for job_spec in jobs
        ]
        check_results = [
            *_check_freshness_checks(conn, spec, now_utc=now_utc),
            *_check_metric_thresholds(conn, spec),
        ]

    check_failures = [item for item in check_results if not item.get("soft", False)]
    soft_warnings = [item for item in check_results if item.get("soft", False)]

    report = {
        "status": _aggregate_status(job_results, check_failures),
        "generated_at": now_utc.isoformat(),
        "target_date": now_local.date().isoformat(),
        "job_results": job_results,
        "check_failures": check_failures,
        "soft_warnings": soft_warnings,
    }
    report_paths = _write_incident_report(report, spec)
    record_job_incident(
        canary_name=spec["canary_name"],
        target_name=target_job or "engine",
        status=report["status"],
        diagnosis_category="aggregate",
        summary=f"Engine watchdog {report['status']}",
        details=report,
        report_json_path=report_paths["json"],
        report_markdown_path=report_paths["markdown"],
    )

    logger.info(
        "Engine watchdog complete status=%s report=%s",
        report["status"],
        report_paths["markdown"],
    )

    if report["status"] in {"failed", "recovered"} and not dry_run:
        _notify_report(report, report_paths)

    if report["status"] == "failed":
        failing = ", ".join(item["job_name"] for item in job_results if item["status"] == "failed")
        failing_checks = ", ".join(item["name"] for item in check_failures)
        failure_parts = []
        if failing:
            failure_parts.append(f"jobs: {failing}")
        if failing_checks:
            failure_parts.append(f"checks: {failing_checks}")
        raise EngineWatchdogFailure(f"Engine watchdog found failures: {'; '.join(failure_parts)}")

    return {
        "summary": f"engine_watchdog {report['status']}",
        "metrics": {
            "job_count": len(job_results),
            "hard_check_failure_count": len(check_failures),
            "soft_warning_count": len(soft_warnings),
        },
    }
