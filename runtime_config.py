from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import socket
import subprocess
import sys
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable


ENGINE_SLUG = "folloze-outbound"
EXPECTED_REPO_NAME = "folloze-outbound-engine"
EXPECTED_SENDER_DOMAIN = "folloze.com"
REPO_ROOT = Path(__file__).resolve().parent
LOGS_DIR = REPO_ROOT / "logs"
JOB_LOG_DIR = LOGS_DIR / "jobs"
INCIDENT_LOG_DIR = LOGS_DIR / "incidents"
EVENTS_LOG_PATH = LOGS_DIR / "job-events.jsonl"
INCIDENT_EVENTS_LOG_PATH = LOGS_DIR / "incident-events.jsonl"
ENGINE_PAUSE_STATE_PATH = REPO_ROOT / "ops" / "engine-paused.json"
PAUSE_BLOCKED_JOBS = frozenset(
    {
        "assign_push",
        "daily_report",
        "deliverability",
        "engine_watchdog",
        "pull_events",
        "re_engage",
        "setup_campaigns",
    }
)

_SENSITIVE_KEY_RE = re.compile(
    r"(api[_-]?key|token|secret|password|passwd|authorization|bearer|client[_-]?secret)",
    re.IGNORECASE,
)
_SENSITIVE_VALUE_RE = re.compile(
    r"(api_key=)[^&\s]+|"
    r"(Authorization:\s*Bearer\s+)[^\s]+|"
    r"(Bearer\s+)[A-Za-z0-9._~+/=-]{12,}|"
    r"(sk-[A-Za-z0-9_-]{12,})|"
    r"(xox[baprs]-[A-Za-z0-9-]{12,})|"
    r"(github_pat_[A-Za-z0-9_]{12,})|"
    r"(gh[pousr]_[A-Za-z0-9_]{12,})",
    re.IGNORECASE,
)


class _TeeStream:
    """Mirror print output to the original stream and a log file."""

    def __init__(self, original, file_handle):
        self.original = original
        self.file_handle = file_handle
        self.encoding = getattr(original, "encoding", "utf-8")

    def write(self, data):
        if not data:
            return 0
        self.original.write(data)
        self.file_handle.write(data)
        return len(data)

    def flush(self):
        self.original.flush()
        self.file_handle.flush()

    def isatty(self):
        return bool(getattr(self.original, "isatty", lambda: False)())


def _ensure_log_dirs() -> None:
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    JOB_LOG_DIR.mkdir(parents=True, exist_ok=True)
    INCIDENT_LOG_DIR.mkdir(parents=True, exist_ok=True)


def _append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload, default=str, sort_keys=True))
        fh.write("\n")


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: "<redacted>" if _SENSITIVE_KEY_RE.search(str(key)) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_sensitive(item) for item in value)
    if isinstance(value, str):
        return _SENSITIVE_VALUE_RE.sub(
            lambda match: f"{match.group(1) or match.group(2) or match.group(3) or ''}<redacted>",
            value,
        )
    return value


def _safe_json(value: dict[str, Any] | None) -> str:
    return json.dumps(redact_sensitive(value or {}), default=str, sort_keys=True)


def get_job_log_path(job_name: str, run_date: date | None = None) -> Path:
    _ensure_log_dirs()
    run_date = run_date or date.today()
    safe_name = job_name.replace("/", "_").replace(" ", "_")
    return JOB_LOG_DIR / f"{safe_name}_{run_date:%Y-%m-%d}.log"


def _create_run_id(job_name: str) -> str:
    return f"{job_name}-{datetime.now(timezone.utc):%Y%m%d%H%M%S}-{uuid.uuid4().hex[:8]}"


def _read_keychain(service: str) -> str | None:
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service, "-w"],
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        return None
    value = result.stdout.strip()
    return value or None


def get_required_setting(
    env_name: str,
    *,
    keychain_service: str | None = None,
    allow_empty: bool = False,
) -> str:
    value = os.getenv(env_name)
    if not value and keychain_service:
        value = _read_keychain(keychain_service)
    if value:
        return value
    if allow_empty:
        return ""
    raise RuntimeError(f"{env_name} is required for {ENGINE_SLUG}")


def assert_repo_context() -> None:
    repo_root = str(REPO_ROOT)
    if EXPECTED_REPO_NAME not in repo_root:
        raise RuntimeError(
            f"{ENGINE_SLUG} expected repo {EXPECTED_REPO_NAME}, got {repo_root}"
        )


def get_engine_pause_state() -> dict[str, Any] | None:
    if not ENGINE_PAUSE_STATE_PATH.exists():
        return None
    try:
        raw = json.loads(ENGINE_PAUSE_STATE_PATH.read_text())
    except json.JSONDecodeError:
        raw = {}
    if isinstance(raw, dict) and raw.get("paused") is False:
        return None
    if not isinstance(raw, dict):
        raw = {}
    return {
        "paused": True,
        "reason": raw.get("reason") or "engine is paused",
        "updated_at": raw.get("updated_at"),
        "updated_by": raw.get("updated_by"),
        "path": str(ENGINE_PAUSE_STATE_PATH),
    }


def should_skip_for_pause(job_name: str) -> tuple[bool, str]:
    if job_name not in PAUSE_BLOCKED_JOBS:
        return False, ""
    if os.getenv("FOLLOZE_ALLOW_WHEN_PAUSED") == "1":
        return False, ""
    state = get_engine_pause_state()
    if not state:
        return False, ""
    reason = state.get("reason") or "engine is paused"
    details = []
    if state.get("updated_at"):
        details.append(f"updated_at={state['updated_at']}")
    if state.get("updated_by"):
        details.append(f"updated_by={state['updated_by']}")
    suffix = f" ({', '.join(details)})" if details else ""
    return (
        True,
        f"[engine-paused] {job_name} skipped because the outbound engine is paused: {reason}{suffix}. "
        "Set FOLLOZE_ALLOW_WHEN_PAUSED=1 to override.",
    )


def get_link_debug_logger() -> logging.Logger:
    """
    Returns a dedicated logger that writes to logs/link_debug.log.

    Tracks the full link-creation pipeline per contact:
      INPUT → URL → HTML → PAYLOAD → API response

    Call once at module level:
        from runtime_config import get_link_debug_logger
        _link_log = get_link_debug_logger()

    All lines are prefixed [assign_id|email] so you can grep for one contact:
        grep "jane@acme.com" logs/link_debug.log
    """
    logger = logging.getLogger("link_debug")
    if logger.handlers:
        return logger  # already configured — don't add duplicate handlers
    logger.setLevel(logging.DEBUG)
    logger.propagate = False  # don't double-write to the root pipeline log
    log_path = REPO_ROOT / "logs" / "link_debug.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.FileHandler(log_path)
    fh.setFormatter(logging.Formatter(
        "[%(asctime)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    logger.addHandler(fh)
    return logger


def configure_logging(
    job_name: str,
    *,
    run_id: str | None = None,
    log_path: Path | None = None,
) -> logging.Logger:
    """
    Configure root logger with timestamps and job name. Call once at the top
    of each job's run() function.

    Output format: [2026-03-16 07:00:01] [INFO] [assign_push] message
    """
    _ensure_log_dirs()
    run_id = run_id or os.getenv("FOLLOZE_RUN_ID")
    log_path = log_path or get_job_log_path(job_name)
    format_string = f"[%(asctime)s] [%(levelname)s] [{job_name}] %(message)s"
    if run_id:
        format_string = f"[%(asctime)s] [%(levelname)s] [{job_name}] [{run_id}] %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=format_string,
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(log_path),
            logging.StreamHandler(sys.__stdout__),
        ],
        force=True,  # override any previously configured handlers
    )
    return logging.getLogger(job_name)


@contextlib.contextmanager
def tee_std_streams(log_path: Path):
    """Capture print output in the per-job log while preserving console output."""
    _ensure_log_dirs()
    with log_path.open("a", encoding="utf-8") as fh:
        stdout = _TeeStream(sys.__stdout__, fh)
        stderr = _TeeStream(sys.__stderr__, fh)
        old_stdout, old_stderr = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = stdout, stderr
        try:
            yield
        finally:
            stdout.flush()
            stderr.flush()
            sys.stdout, sys.stderr = old_stdout, old_stderr


def start_job_run(
    job_name: str,
    *,
    run_id: str,
    trigger_source: str = "cli",
    command: str | None = None,
    log_path: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    payload = {
        "event_type": "job_started",
        "job_name": job_name,
        "run_id": run_id,
        "trigger_source": trigger_source,
        "command": command,
        "log_path": log_path,
        "pid": os.getpid(),
        "host": socket.gethostname(),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "context": context or {},
    }
    _append_jsonl(EVENTS_LOG_PATH, payload)

    try:
        from db.connection import db_conn
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO job_runs (
                        job_name,
                        run_id,
                        status,
                        trigger_source,
                        command,
                        pid,
                        host,
                        log_path,
                        context_json
                    )
                    VALUES (%s, %s, 'running', %s, %s, %s, %s, %s, %s::jsonb)
                    """,
                    (
                        job_name,
                        run_id,
                        trigger_source,
                        command,
                        os.getpid(),
                        socket.gethostname(),
                        log_path,
                        _safe_json(context),
                    ),
                )
    except Exception as exc:
        logging.getLogger(__name__).warning("start_job_run: could not persist run metadata: %s", exc)


def finish_job_run(
    run_id: str,
    *,
    status: str,
    summary: str | None = None,
    metrics: dict[str, Any] | None = None,
    error_type: str | None = None,
    error_message: str | None = None,
    context: dict[str, Any] | None = None,
) -> None:
    error_message = redact_sensitive(error_message)
    payload = {
        "event_type": "job_finished",
        "run_id": run_id,
        "status": status,
        "summary": summary,
        "metrics": metrics or {},
        "error_type": error_type,
        "error_message": redact_sensitive(error_message),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "context": context or {},
    }
    _append_jsonl(EVENTS_LOG_PATH, payload)

    try:
        from db.connection import db_conn
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE job_runs
                    SET
                        status = %s,
                        finished_at = NOW(),
                        duration_seconds = EXTRACT(EPOCH FROM (NOW() - started_at)),
                        error_type = %s,
                        error_message = %s,
                        summary = %s,
                        metrics_json = %s::jsonb,
                        context_json = context_json || %s::jsonb
                    WHERE run_id = %s
                    """,
                    (
                        status,
                        error_type,
                        error_message,
                        summary,
                        _safe_json(metrics),
                        _safe_json(context),
                        run_id,
                    ),
                )
    except Exception as exc:
        logging.getLogger(__name__).warning("finish_job_run: could not persist run metadata: %s", exc)


def record_job_incident(
    *,
    canary_name: str,
    target_name: str,
    status: str,
    diagnosis_category: str,
    summary: str,
    details: dict[str, Any],
    report_json_path: str | None = None,
    report_markdown_path: str | None = None,
) -> None:
    payload = {
        "event_type": "job_incident",
        "canary_name": canary_name,
        "target_name": target_name,
        "status": status,
        "diagnosis_category": diagnosis_category,
        "summary": summary,
        "report_json_path": report_json_path,
        "report_markdown_path": report_markdown_path,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "details": details,
    }
    _append_jsonl(INCIDENT_EVENTS_LOG_PATH, payload)

    try:
        from db.connection import db_conn
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO job_incidents (
                        canary_name,
                        target_name,
                        status,
                        diagnosis_category,
                        summary,
                        details_json,
                        report_json_path,
                        report_markdown_path
                    )
                    VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s)
                    """,
                    (
                        canary_name,
                        target_name,
                        status,
                        diagnosis_category,
                        summary,
                        _safe_json(details),
                        report_json_path,
                        report_markdown_path,
                    ),
                )
    except Exception as exc:
        logging.getLogger(__name__).warning("record_job_incident: could not persist incident metadata: %s", exc)


def run_tracked_job(
    job_name: str,
    target: Callable[..., Any],
    *args,
    trigger_source: str | None = None,
    **kwargs,
) -> Any:
    """Run a job under a shared logging + run-ledger wrapper."""
    run_id = _create_run_id(job_name)
    log_path = get_job_log_path(job_name)
    trigger_source = trigger_source or os.getenv("FOLLOZE_TRIGGER_SOURCE", "cli")
    context = {
        "dry_run": bool(kwargs.get("dry_run", False)),
        "kwargs": kwargs,
    }
    os.environ["FOLLOZE_RUN_ID"] = run_id
    os.environ["FOLLOZE_JOB_NAME"] = job_name
    os.environ["FOLLOZE_JOB_LOG_PATH"] = str(log_path)

    configure_logging(job_name, run_id=run_id, log_path=log_path)
    logger = logging.getLogger(job_name)
    command = " ".join(sys.argv)
    start_job_run(
        job_name,
        run_id=run_id,
        trigger_source=trigger_source,
        command=command,
        log_path=str(log_path.relative_to(REPO_ROOT)),
        context=context,
    )

    with tee_std_streams(log_path):
        logger.info("Run starting")
        try:
            result = target(*args, **kwargs)
        except Exception as exc:
            logger.exception("Run failed")
            finish_job_run(
                run_id,
                status="failed",
                error_type=exc.__class__.__name__,
                error_message=str(exc),
                context=context,
            )
            raise

        summary = None
        metrics = None
        if isinstance(result, dict):
            summary = result.get("summary")
            metrics = result.get("metrics")
        elif isinstance(result, str):
            summary = result
        finish_job_run(
            run_id,
            status="success",
            summary=summary,
            metrics=metrics,
            context=context,
        )
        logger.info("Run succeeded")
        return result


MASON_CHANNEL = os.getenv("FOLLOZE_ALERT_CHANNEL", "channel:example")
MASON_EMAIL = os.getenv("FOLLOZE_ALERT_EMAIL", "ops@example.com")
REPORT_TO = os.getenv("FOLLOZE_REPORT_TO", "ops@example.com")


def notify(
    subject: str,
    body: str,
    *,
    discord_body: str | None = None,
) -> None:
    """Send a pipeline alert via Discord. Fail-silent.

    Shell callers should prefer passing subject/body via environment into a small
    Python wrapper so quoting in the message body cannot break the alert path.
    """
    message = discord_body or f"[folloze-outbound] {subject}\n{body}"

    try:
        subprocess.run(
            ["openclaw", "message", "send", "--channel", "discord",
             "--target", MASON_CHANNEL, "--message", message],
            check=True,
            capture_output=True,
            timeout=15,
        )
    except Exception as exc:
        logging.getLogger(__name__).warning("notify: Discord send failed: %s", exc)


def assert_runtime_env() -> None:
    assert_repo_context()
    get_required_setting("FOLLOZE_DATABASE_URL", keychain_service="folloze-outbound-db")
    get_required_setting("FOLLOZE_SMARTLEAD_API_KEY", keychain_service="smartlead-api")
    get_required_setting("FOLLOZE_APOLLO_API_KEY", keychain_service="apollo-folloze-api")
