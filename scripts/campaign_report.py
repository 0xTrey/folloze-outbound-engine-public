"""
scripts/campaign_report.py

Generates a campaign status report and emails it to the configured recipient.
Covers: Smartlead engagement stats, DB pipeline funnel, inbox health.

Usage:
    python scripts/campaign_report.py            # send the email
    python scripts/campaign_report.py --dry-run  # print to stdout only
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2.extras

from connectors.smartlead_client import SmartleadClient
from db.connection import db_conn

RECIPIENT = os.getenv("FOLLOZE_REPORT_TO", "ops@example.com")

CAMPAIGN_IDS = {
    "ENT_ABM_INTENT":     "1001",
    "ENT_REVMKTG_HIRING": "1002",
    "ENT_REVMKTG_INTENT": "1003",
    "MM_CMO_EXEC":        "1004",
    "MM_REVMKTG_TIMING":  "1005",
    "MM_REVOPS_INTENT":   "1006",
}


# ---------------------------------------------------------------------------
# Data collection
# ---------------------------------------------------------------------------

def _pipeline_stats(conn) -> dict:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                COUNT(*) FILTER (WHERE ca.eligibility_status = 'pending'
                    AND cr.research_id IS NULL)                          AS needs_research,
                COUNT(*) FILTER (WHERE ca.eligibility_status = 'pending'
                    AND cr.research_id IS NOT NULL
                    AND pp.payload_id IS NULL)                           AS needs_payload,
                COUNT(*) FILTER (WHERE ca.eligibility_status = 'pending'
                    AND pp.qa_status = 'approved')                       AS ready_to_send,
                COUNT(*) FILTER (WHERE ca.eligibility_status = 'sent')   AS enrolled,
                COUNT(*) FILTER (WHERE ca.eligibility_status = 'ineligible') AS ineligible
            FROM contacts c
            JOIN campaign_assignments ca ON ca.contact_id = c.contact_id
            LEFT JOIN contact_research cr ON cr.contact_id = c.contact_id
            LEFT JOIN personalization_payloads pp ON pp.contact_id = c.contact_id
        """)
        funnel = dict(cur.fetchone())

        cur.execute("SELECT COUNT(*) AS n FROM accounts WHERE account_status = 'active'")
        funnel["total_accounts"] = cur.fetchone()["n"]

        cur.execute("""
            SELECT COUNT(*) AS n FROM sends
            WHERE sent_at >= NOW() - INTERVAL '7 days'
        """)
        funnel["sent_this_week"] = cur.fetchone()["n"]

    return funnel


def _smartlead_stats(sl: SmartleadClient) -> list[dict]:
    rows = []
    for family, cid in CAMPAIGN_IDS.items():
        try:
            stats = sl.get_campaign_stats(cid)
            rows.append({
                "family": family,
                "sent":    stats.get("sent_count", 0),
                "opened":  stats.get("open_count", 0),
                "clicked": stats.get("click_count", 0),
                "replied": stats.get("reply_count", 0),
                "bounced": stats.get("block_count", 0),
            })
        except Exception as e:
            rows.append({"family": family, "error": str(e)})
    return rows


def _inbox_health(sl: SmartleadClient) -> dict:
    accounts = sl.list_email_accounts()
    active, not_started, blocked = 0, 0, 0
    for a in accounts:
        wd = a.get("warmup_details") or {}
        status = wd.get("status") or "NOT_STARTED"
        if wd.get("is_warmup_blocked"):
            blocked += 1
        elif status == "ACTIVE":
            active += 1
        else:
            not_started += 1
    return {"total": len(accounts), "active": active, "not_started": not_started, "blocked": blocked}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _pct(num: int, denom: int) -> str:
    if not denom:
        return "—"
    return f"{round(num / denom * 100, 1)}%"


def _render_html(funnel: dict, sl_stats: list[dict], inbox: dict) -> str:
    today = date.today().strftime("%B %d, %Y")

    # Smartlead table rows
    sl_rows = ""
    total_sent = total_opened = total_replied = total_bounced = 0
    for r in sl_stats:
        if "error" in r:
            sl_rows += f"<tr><td>{r['family']}</td><td colspan='5' style='color:#999'>API error: {r['error'][:60]}</td></tr>"
            continue
        sent    = r.get("sent", 0)
        opened  = r.get("opened", 0)
        replied = r.get("replied", 0)
        bounced = r.get("bounced", 0)
        total_sent    += sent
        total_opened  += opened
        total_replied += replied
        total_bounced += bounced
        sl_rows += (
            f"<tr>"
            f"<td>{r['family']}</td>"
            f"<td style='text-align:center'>{sent}</td>"
            f"<td style='text-align:center'>{opened} ({_pct(opened, sent)})</td>"
            f"<td style='text-align:center'>{replied} ({_pct(replied, sent)})</td>"
            f"<td style='text-align:center'>{bounced} ({_pct(bounced, sent)})</td>"
            f"</tr>"
        )
    sl_rows += (
        f"<tr style='font-weight:bold;border-top:2px solid #ccc'>"
        f"<td>TOTAL</td>"
        f"<td style='text-align:center'>{total_sent}</td>"
        f"<td style='text-align:center'>{total_opened} ({_pct(total_opened, total_sent)})</td>"
        f"<td style='text-align:center'>{total_replied} ({_pct(total_replied, total_sent)})</td>"
        f"<td style='text-align:center'>{total_bounced} ({_pct(total_bounced, total_sent)})</td>"
        f"</tr>"
    )

    inbox_status = "All good" if inbox["blocked"] == 0 and inbox["not_started"] <= 1 else "Needs attention"
    inbox_color  = "#2d7d46" if inbox_status == "All good" else "#b45309"

    return f"""
<html><body style="font-family:sans-serif;color:#111;max-width:680px;margin:0 auto">
<h2 style="margin-bottom:4px">Folloze Outbound — Campaign Report</h2>
<p style="color:#666;margin-top:0">{today}</p>

<h3>Pipeline Funnel</h3>
<table style="border-collapse:collapse;width:100%">
  <tr><td style="padding:4px 12px 4px 0">Accounts in DB</td><td><strong>{funnel['total_accounts']}</strong></td></tr>
  <tr><td style="padding:4px 12px 4px 0">Enrolled in Smartlead</td><td><strong>{funnel['enrolled']}</strong></td></tr>
  <tr><td style="padding:4px 12px 4px 0">Sent this week</td><td><strong>{funnel['sent_this_week']}</strong></td></tr>
  <tr><td style="padding:4px 12px 4px 0">Ready to send (approved, not yet pushed)</td><td><strong>{funnel['ready_to_send']}</strong></td></tr>
  <tr><td style="padding:4px 12px 4px 0">Needs research</td><td><strong>{funnel['needs_research']}</strong></td></tr>
  <tr><td style="padding:4px 12px 4px 0">Needs payload</td><td><strong>{funnel['needs_payload']}</strong></td></tr>
</table>

<h3>Smartlead Engagement (all-time)</h3>
<table style="border-collapse:collapse;width:100%;font-size:14px">
  <thead>
    <tr style="background:#f4f4f4">
      <th style="text-align:left;padding:6px 10px">Sequence</th>
      <th style="padding:6px 10px">Sent</th>
      <th style="padding:6px 10px">Opened</th>
      <th style="padding:6px 10px">Replied</th>
      <th style="padding:6px 10px">Bounced</th>
    </tr>
  </thead>
  <tbody>
    {sl_rows}
  </tbody>
</table>

<h3>Inbox Health</h3>
<p>
  {inbox['active']}/{inbox['total']} inboxes active &nbsp;|&nbsp;
  {inbox['not_started']} not started &nbsp;|&nbsp;
  {inbox['blocked']} blocked &nbsp;|&nbsp;
  <strong style="color:{inbox_color}">{inbox_status}</strong>
</p>

<hr style="margin-top:32px;border:none;border-top:1px solid #eee">
<p style="color:#999;font-size:12px">Folloze Outbound Engine &middot; automated report</p>
</body></html>
"""


def _render_text(funnel: dict, sl_stats: list[dict], inbox: dict) -> str:
    today = date.today().strftime("%B %d, %Y")
    lines = [
        f"FOLLOZE OUTBOUND — CAMPAIGN REPORT ({today})",
        "=" * 50,
        "",
        "PIPELINE FUNNEL",
        f"  Accounts in DB:          {funnel['total_accounts']}",
        f"  Enrolled in Smartlead:   {funnel['enrolled']}",
        f"  Sent this week:          {funnel['sent_this_week']}",
        f"  Ready to send:           {funnel['ready_to_send']}",
        f"  Needs research:          {funnel['needs_research']}",
        f"  Needs payload:           {funnel['needs_payload']}",
        "",
        "SMARTLEAD ENGAGEMENT",
    ]
    for r in sl_stats:
        if "error" in r:
            lines.append(f"  {r['family']}: error")
            continue
        sent = r.get("sent", 0)
        lines.append(
            f"  {r['family']}: sent={sent} "
            f"open={_pct(r.get('opened',0), sent)} "
            f"reply={_pct(r.get('replied',0), sent)} "
            f"bounce={_pct(r.get('bounced',0), sent)}"
        )
    lines += [
        "",
        "INBOX HEALTH",
        f"  {inbox['active']}/{inbox['total']} active | {inbox['not_started']} not started | {inbox['blocked']} blocked",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run(dry_run: bool = False):
    print("[campaign_report] Collecting data...")

    with db_conn() as conn:
        funnel = _pipeline_stats(conn)

    sl = SmartleadClient()
    sl_stats  = _smartlead_stats(sl)
    inbox     = _inbox_health(sl)

    html = _render_html(funnel, sl_stats, inbox)
    text = _render_text(funnel, sl_stats, inbox)

    if dry_run:
        print(text)
        return

    from google_workspace import gmail
    today = date.today().strftime("%B %d")
    subject = f"Folloze Outbound — Campaign Report ({today})"
    result = gmail.send_email(RECIPIENT, subject, html, text)
    print(f"[campaign_report] Sent to {RECIPIENT} — message id: {result.get('id')}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)
