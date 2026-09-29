"""
Job: daily_report
Pulls outbound activity from the DB plus live outbound-transport stats and sends
a summary to the configured report destination.

Usage:
    python main.py daily_report [--dry-run]
"""
from __future__ import annotations

import html
import logging
import re
from datetime import date, timedelta
from pathlib import Path

import psycopg2.extras

from connectors.smartlead_client import SmartleadClient
from db.connection import db_conn
from runtime_config import configure_logging, notify

# Active campaign IDs to pull stats from
ACTIVE_CAMPAIGNS = [
    (1001, "GTME_VIBE_MKT"),
    (1002, "MM_REVMKTG_TIMING"),
    (1003, "MM_CMO_EXEC"),
    (1004, "ENT_REVMKTG_HIRING"),
]
LOG_DIR = Path(__file__).resolve().parent.parent / "logs"
MAX_PIPELINE_ISSUES = 5
STEP_LINE_RE = re.compile(r"Step \d+:\s+([A-Za-z0-9_]+)")
FAILED_STEP_RE = re.compile(r"Step '([^']+)' failed")
logger = logging.getLogger(__name__)


def _query_db(conn) -> dict:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        yesterday = date.today() - timedelta(days=1)

        # Yesterday's T1 enrollments
        cur.execute("""
            SELECT COUNT(*) AS n FROM sends
            WHERE sent_at >= %s AND sent_at < %s
              AND (step_number = 1 OR step_number IS NULL)
        """, (yesterday, date.today()))
        enrolled_yesterday = cur.fetchone()["n"] or 0

        # Contacts in active follow-up window (enrolled in last 14 days)
        cur.execute("""
            SELECT COUNT(DISTINCT contact_id) AS n FROM sends
            WHERE sent_at >= CURRENT_DATE - INTERVAL '14 days'
              AND (step_number = 1 OR step_number IS NULL)
        """)
        followup_active = cur.fetchone()["n"] or 0

        # In-sequence by family
        cur.execute("""
            SELECT COALESCE(sequence_family, 'unknown') AS family, COUNT(*) AS n
            FROM campaign_assignments
            WHERE eligibility_status = 'sent'
            GROUP BY family ORDER BY n DESC
        """)
        in_sequence = {r["family"]: r["n"] for r in cur.fetchall()}

        # Pipeline ready to push
        cur.execute("""
            SELECT eligibility_status, COUNT(*) AS n
            FROM campaign_assignments
            WHERE eligibility_status IN ('pending', 'eligible')
            GROUP BY eligibility_status
        """)
        pipeline = {r["eligibility_status"]: r["n"] for r in cur.fetchall()}

        # All-time totals
        cur.execute("""
            SELECT
                COUNT(*) AS total_sent,
                COUNT(*) FILTER (WHERE reply_type IS NOT NULL)      AS total_replies,
                COUNT(*) FILTER (WHERE reply_type = 'interested')   AS total_interested,
                COUNT(*) FILTER (WHERE delivery_status = 'bounced') AS total_bounced
            FROM sends
        """)
        totals = dict(cur.fetchone())

        # Suppressed domains
        cur.execute("""
            SELECT COUNT(DISTINCT domain) AS n FROM suppressions
            WHERE domain IS NOT NULL AND domain != ''
        """)
        suppressed_domains = cur.fetchone()["n"] or 0

    return {
        "enrolled_yesterday": enrolled_yesterday,
        "followup_active":    followup_active,
        "in_sequence":        in_sequence,
        "total_active":       sum(in_sequence.values()),
        "pipeline":           pipeline,
        "totals":             totals,
        "suppressed_domains": suppressed_domains,
    }


def _query_smartlead() -> list[dict]:
    """Pull live aggregate stats for each active campaign from Smartlead."""
    client = SmartleadClient()
    results = []
    for cid, name in ACTIVE_CAMPAIGNS:
        try:
            stats = client.get_campaign_stats(cid)
            stats["name"] = name
            results.append(stats)
        except Exception as e:
            logger.warning("Smartlead stats failed for %s: %s", name, e)
    return results


def _normalize_log_line(line: str) -> str:
    return re.sub(
        r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\]\s*",
        "",
        line.strip(),
    )


def _get_pipeline_health(
    run_date: date | None = None,
    *,
    log_dir: Path | None = None,
) -> dict:
    run_date = run_date or (date.today() - timedelta(days=1))
    log_dir = log_dir or LOG_DIR
    log_path = log_dir / f"pipeline_{run_date:%Y-%m-%d}.log"

    if not log_path.exists():
        return {
            "run_date": run_date,
            "log_name": log_path.name,
            "status": "failure",
            "last_step": "not available",
            "warnings": [f"Log file not found: {log_path.name}"],
            "errors": [],
            "warning_count": 1,
            "error_count": 0,
            "zero_enrollment_detected": False,
        }

    lines = log_path.read_text(errors="replace").splitlines()
    last_step = "not available"
    warnings: list[str] = []
    errors: list[str] = []
    completed = False
    zero_enrollment = False

    for raw_line in lines:
        line = _normalize_log_line(raw_line)
        failed_match = FAILED_STEP_RE.search(line)
        step_match = STEP_LINE_RE.search(line)
        if failed_match:
            last_step = failed_match.group(1)
        elif step_match:
            last_step = step_match.group(1)

        if "daily_outbound: done" in line:
            completed = True
        if "Zero enrollments detected" in line:
            zero_enrollment = True
        if "WARNING:" in line or "[WARNING]" in line:
            warnings.append(line)
        if "ERROR:" in line or "[ERROR]" in line:
            errors.append(line)

    return {
        "run_date": run_date,
        "log_name": log_path.name,
        "status": "success" if completed else "failure",
        "last_step": last_step,
        "warnings": warnings[:MAX_PIPELINE_ISSUES],
        "errors": errors[:MAX_PIPELINE_ISSUES],
        "warning_count": len(warnings),
        "error_count": len(errors),
        "zero_enrollment_detected": zero_enrollment,
    }


def _format_issue_list_html(lines: list[str], empty_text: str) -> str:
    if not lines:
        return f'<p style="margin:6px 0 0;color:#888">{empty_text}</p>'
    items = "".join(
        f"<li style=\"margin:0 0 4px\">{html.escape(line)}</li>"
        for line in lines
    )
    return f"<ul style=\"margin:8px 0 0 18px;padding:0;color:#444\">{items}</ul>"


def _format_pipeline_health_html(health: dict) -> str:
    status_color = "#067647" if health["status"] == "success" else "#b42318"
    zero_enrollment = "Yes" if health["zero_enrollment_detected"] else "No"
    run_label = health["run_date"].strftime("%b %d")
    errors_html = ""
    if health["errors"]:
        errors_html = f"""
  <div style="margin-top:14px">
    <strong style="font-size:13px;color:#b42318">Errors ({health['error_count']})</strong>
    {_format_issue_list_html(health['errors'], 'None')}
  </div>"""

    return f"""
  <h3 style="margin:0 0 8px;font-size:14px;text-transform:uppercase;color:#888;letter-spacing:.05em">Pipeline Health ({run_label})</h3>
  <table style="width:100%;border-collapse:collapse;margin-bottom:14px">
    <tr><td style="padding:6px 0;width:220px">Run status</td><td><strong style="color:{status_color}">{health['status'].title()}</strong></td></tr>
    <tr><td style="padding:6px 0">Last step seen</td><td><strong>{html.escape(health['last_step'])}</strong></td></tr>
    <tr><td style="padding:6px 0">Zero-enrollment detected</td><td><strong>{zero_enrollment}</strong></td></tr>
    <tr><td style="padding:6px 0">Log file</td><td><strong>{html.escape(health['log_name'])}</strong></td></tr>
  </table>
  <div style="margin-bottom:24px;font-size:13px">
    <strong style="color:#555">Warnings ({health['warning_count']})</strong>
    {_format_issue_list_html(health['warnings'], 'None')}
    {errors_html}
  </div>
"""


def _format_pipeline_health_discord(health: dict) -> list[str]:
    run_label = health["run_date"].strftime("%b %d")
    lines = [
        f"**Pipeline Health ({run_label})**",
        f"  • Status: {health['status']}",
        f"  • Last step: {health['last_step']}",
        f"  • Zero enrollment detected: {'yes' if health['zero_enrollment_detected'] else 'no'}",
    ]
    if health["warnings"]:
        lines.extend(f"  • Warning: {warning}" for warning in health["warnings"])
    else:
        lines.append("  • Warning: none")
    if health["errors"]:
        lines.extend(f"  • Error: {error}" for error in health["errors"])
    return lines


def _format_html(db: dict, sl: list[dict], health: dict) -> str:
    today     = date.today().strftime("%B %d, %Y")
    yesterday = (date.today() - timedelta(days=1)).strftime("%b %d")
    totals    = db["totals"]
    pipeline  = db["pipeline"]
    pipeline_health_html = _format_pipeline_health_html(health)

    total_sent  = totals["total_sent"] or 0
    reply_rate  = round(totals["total_replies"] / total_sent * 100, 1) if total_sent else 0.0
    bounce_rate = round(totals["total_bounced"] / total_sent * 100, 1) if total_sent else 0.0
    pending     = pipeline.get("pending", 0)

    # Smartlead campaign rows
    sl_rows = ""
    sl_totals = {"sent": 0, "opens": 0, "replies": 0, "clicks": 0}
    for s in sl:
        sent    = s.get("sent_count", 0)
        opens   = s.get("open_count", 0)
        clicks  = s.get("click_count", 0)
        replies = s.get("reply_count", 0)
        open_r  = round(opens / sent * 100, 1) if sent else 0.0
        click_r = round(clicks / sent * 100, 1) if sent else 0.0
        reply_r = round(replies / sent * 100, 1) if sent else 0.0
        sl_rows += f"""
        <tr>
          <td style="padding:6px 12px;border-bottom:1px solid #eee">{s['name']}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:center">{sent}</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:center">{opens} ({open_r}%)</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:center">{clicks} ({click_r}%)</td>
          <td style="padding:6px 12px;border-bottom:1px solid #eee;text-align:center">{replies} ({reply_r}%)</td>
        </tr>"""
        sl_totals["sent"]    += sent
        sl_totals["opens"]   += opens
        sl_totals["clicks"]  += clicks
        sl_totals["replies"] += replies

    tot = sl_totals
    tot_open_r  = round(tot["opens"]   / tot["sent"] * 100, 1) if tot["sent"] else 0.0
    tot_click_r = round(tot["clicks"]  / tot["sent"] * 100, 1) if tot["sent"] else 0.0
    tot_reply_r = round(tot["replies"] / tot["sent"] * 100, 1) if tot["sent"] else 0.0

    seq_rows = "".join(
        f'<tr><td style="padding:4px 12px;color:#555">{fam}</td>'
        f'<td style="padding:4px 12px;text-align:center">{n}</td></tr>'
        for fam, n in db["in_sequence"].items()
    ) or '<tr><td colspan="2" style="padding:4px 12px;color:#999">none</td></tr>'

    return f"""
<div style="font-family:Arial,sans-serif;max-width:620px;color:#222">
  <p style="font-size:13px;color:#888;margin-bottom:4px">{today}</p>
  <h2 style="margin:0 0 20px">Folloze Outbound — Daily Report</h2>

  <h3 style="margin:0 0 8px;font-size:14px;text-transform:uppercase;color:#888;letter-spacing:.05em">Yesterday ({yesterday})</h3>
  <table style="width:100%;border-collapse:collapse;margin-bottom:24px">
    <tr><td style="padding:6px 0;width:220px">Enrolled</td><td><strong>{db['enrolled_yesterday']}</strong></td></tr>
    <tr><td style="padding:6px 0">In follow-up window</td><td><strong>{db['followup_active']}</strong></td></tr>
    <tr><td style="padding:6px 0">Pipeline (pending)</td><td><strong>{pending}</strong></td></tr>
  </table>

  {pipeline_health_html}

  <h3 style="margin:0 0 8px;font-size:14px;text-transform:uppercase;color:#888;letter-spacing:.05em">Campaign Performance (All-Time)</h3>
  <table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px">
    <thead>
      <tr style="background:#f5f5f5">
        <th style="padding:8px 12px;text-align:left;font-weight:600">Campaign</th>
        <th style="padding:8px 12px;text-align:center;font-weight:600">Sent</th>
        <th style="padding:8px 12px;text-align:center;font-weight:600">Opens</th>
        <th style="padding:8px 12px;text-align:center;font-weight:600">Clicks</th>
        <th style="padding:8px 12px;text-align:center;font-weight:600">Replies</th>
      </tr>
    </thead>
    <tbody>
      {sl_rows}
      <tr style="font-weight:600;background:#fafafa">
        <td style="padding:8px 12px">Total</td>
        <td style="padding:8px 12px;text-align:center">{tot['sent']}</td>
        <td style="padding:8px 12px;text-align:center">{tot['opens']} ({tot_open_r}%)</td>
        <td style="padding:8px 12px;text-align:center">{tot['clicks']} ({tot_click_r}%)</td>
        <td style="padding:8px 12px;text-align:center">{tot['replies']} ({tot_reply_r}%)</td>
      </tr>
    </tbody>
  </table>

  <h3 style="margin:0 0 8px;font-size:14px;text-transform:uppercase;color:#888;letter-spacing:.05em">Contacts In Sequence ({db['total_active']})</h3>
  <table style="width:100%;border-collapse:collapse;margin-bottom:24px;font-size:13px">
    {seq_rows}
  </table>

  <h3 style="margin:0 0 8px;font-size:14px;text-transform:uppercase;color:#888;letter-spacing:.05em">All-Time Program Stats</h3>
  <table style="width:100%;border-collapse:collapse;margin-bottom:24px">
    <tr><td style="padding:6px 0;width:220px">Total sent</td><td><strong>{total_sent}</strong></td></tr>
    <tr><td style="padding:6px 0">Total replies</td><td><strong>{totals['total_replies']} ({reply_rate}%)</strong></td></tr>
    <tr><td style="padding:6px 0">Interested replies</td><td><strong>{totals['total_interested']}</strong></td></tr>
    <tr><td style="padding:6px 0">Bounced</td><td><strong>{totals['total_bounced']} ({bounce_rate}%)</strong></td></tr>
    <tr><td style="padding:6px 0">Suppressed domains</td><td><strong>{db['suppressed_domains']}</strong></td></tr>
  </table>

  <p style="font-size:11px;color:#aaa;border-top:1px solid #eee;padding-top:12px;margin-top:8px">
    Sent by Mason · Folloze Outbound Engine
  </p>
</div>
"""


def _format_discord(db: dict, sl: list[dict], health: dict) -> str:
    today     = date.today().strftime("%B %d, %Y")
    yesterday = (date.today() - timedelta(days=1)).strftime("%b %d")
    totals    = db["totals"]
    pipeline  = db["pipeline"]

    total_sent = totals["total_sent"] or 0
    reply_rate = round(totals["total_replies"] / total_sent * 100, 1) if total_sent else 0.0

    sl_lines = []
    for s in sl:
        sent    = s.get("sent_count", 0)
        opens   = s.get("open_count", 0)
        clicks  = s.get("click_count", 0)
        replies = s.get("reply_count", 0)
        open_r  = round(opens / sent * 100, 1) if sent else 0.0
        sl_lines.append(
            f"  • {s['name']}: {sent} sent | {opens} opens ({open_r}%) | {replies} replies | {clicks} clicks"
        )

    seq_lines = "\n".join(f"  • {f}: {n}" for f, n in db["in_sequence"].items()) or "  • none"

    return "\n".join([
        f"**Folloze Outbound — {today}**",
        "",
        f"**Yesterday ({yesterday})**",
        f"  • Enrolled: {db['enrolled_yesterday']}",
        f"  • In follow-up window: {db['followup_active']}",
        f"  • Pipeline pending: {pipeline.get('pending', 0)}",
        "",
        *_format_pipeline_health_discord(health),
        "",
        "**Campaign Stats (all-time)**",
        *sl_lines,
        "",
        f"**In Sequence** ({db['total_active']} active)",
        seq_lines,
        "",
        "**All-Time**",
        f"  • Sent: {total_sent} | Replies: {totals['total_replies']} ({reply_rate}%) | Interested: {totals['total_interested']}",
        f"  • Bounced: {totals['total_bounced']} | Suppressed domains: {db['suppressed_domains']}",
    ])


def _send_report(subject: str, html_body: str, discord_body: str, dry_run: bool = False):
    if dry_run:
        logger.info("Would notify with subject '%s'", subject)
        logger.info("Discord preview:\n%s", discord_body)
        return
    notify(subject, discord_body, email_html=html_body)


def run(dry_run: bool = False):
    configure_logging("daily_report")
    logger.info("Pulling data. dry_run=%s", dry_run)
    with db_conn() as conn:
        db_data = _query_db(conn)
    sl_data = _query_smartlead()
    health = _get_pipeline_health()

    today   = date.today().strftime("%b %d")
    subject = f"Folloze Outbound — {today}"
    html    = _format_html(db_data, sl_data, health)
    discord = _format_discord(db_data, sl_data, health)

    _send_report(subject, html, discord, dry_run=dry_run)
