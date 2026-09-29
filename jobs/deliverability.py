"""
Job: deliverability
Syncs unsubscribes and hard bounces from Smartlead back into the suppressions table.
Runs before assign_push each day to prevent re-enrolling contacts who opted out or bounced.

What it does:
  1. Iterates all active Smartlead campaigns
  2. For each campaign, pulls all leads
  3. Writes to suppressions:
     - lead.is_unsubscribed = True  → reason='unsubscribe'
     - campaign_lead.status in BOUNCE_STATUSES → reason='bounce'
  4. Pauses the Smartlead campaign if bounce rate > BOUNCE_THRESHOLD (safety kill)

Bounce status values observed in Smartlead:
  EMAIL_BOUNCED, HARD_BOUNCE, INVALID_EMAIL — add others as discovered.

Usage:
    python main.py deliverability --dry-run
    python main.py deliverability
"""
from __future__ import annotations

from connectors.smartlead_client import SmartleadClient
from db.connection import db_conn

BOUNCE_THRESHOLD = 0.04  # 4% — pause campaign above this
BOUNCE_STATUSES = {
    "EMAIL_BOUNCED",
    "HARD_BOUNCE",
    "BOUNCED",
    "INVALID_EMAIL",
    "EMAIL_NOT_FOUND",
}


def _upsert_suppression(conn, email: str, domain: str | None, reason: str, source: str):
    """Insert into suppressions if not already present. Idempotent."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO suppressions (email, domain, reason, source)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT DO NOTHING
        """, (email, domain, reason, source))


def _domain_from_email(email: str) -> str | None:
    parts = email.split("@")
    return parts[1] if len(parts) == 2 else None


def run(dry_run: bool = False):
    print(f"[deliverability] Starting. dry_run={dry_run}")

    sl = SmartleadClient()
    campaigns = sl.list_campaigns()
    active = [c for c in campaigns if c.get("status") in ("ACTIVE", "PAUSED")]
    print(f"[deliverability] {len(active)} active/paused campaigns to scan")

    total_unsub = 0
    total_bounce = 0

    for campaign in active:
        campaign_id = str(campaign["id"])
        campaign_name = campaign.get("name", campaign_id)

        # Pull all leads — paginate in chunks of 100
        all_leads = []
        offset = 0
        while True:
            chunk = sl.get_leads(campaign_id, offset=offset, limit=100)
            if not chunk:
                break
            all_leads.extend(chunk)
            if len(chunk) < 100:
                break
            offset += 100

        if not all_leads:
            continue

        sent_count = len(all_leads)
        bounce_count = 0
        unsub_count = 0

        with db_conn() as conn:
            for item in all_leads:
                lead = item.get("lead") or {}
                email = lead.get("email") or ""
                if not email:
                    continue

                domain = _domain_from_email(email)
                status = item.get("status") or ""

                # Unsubscribed
                if lead.get("is_unsubscribed"):
                    unsub_count += 1
                    total_unsub += 1
                    if dry_run:
                        print(f"  [DRY RUN] suppression: {email} reason=unsubscribe")
                    else:
                        _upsert_suppression(conn, email, domain, "unsubscribe", f"smartlead:{campaign_id}")

                # Bounced
                elif status.upper() in BOUNCE_STATUSES:
                    bounce_count += 1
                    total_bounce += 1
                    if dry_run:
                        print(f"  [DRY RUN] suppression: {email} reason=bounce (status={status})")
                    else:
                        _upsert_suppression(conn, email, domain, "bounce", f"smartlead:{campaign_id}")

            if not dry_run:
                conn.commit()

        # Bounce rate check — pause campaign if above threshold
        if sent_count > 0:
            bounce_rate = bounce_count / sent_count
            if bounce_rate > BOUNCE_THRESHOLD:
                print(f"  [ALERT] {campaign_name}: bounce rate {bounce_rate:.1%} > {BOUNCE_THRESHOLD:.0%} threshold")
                if dry_run:
                    print(f"  [DRY RUN] would pause campaign {campaign_id}")
                else:
                    try:
                        sl._post(f"/campaigns/{campaign_id}/status", {"status": "STOP"})
                        print(f"  [PAUSED] {campaign_name} paused due to bounce rate")
                    except Exception as e:
                        print(f"  [ERROR] Failed to pause {campaign_name}: {e}")

        if unsub_count or bounce_count:
            print(f"  {campaign_name}: {unsub_count} unsubscribes, {bounce_count} bounces (of {sent_count} leads)")

    print(f"[deliverability] Done. total_unsub={total_unsub} total_bounce={total_bounce}")
