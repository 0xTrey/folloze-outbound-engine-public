"""
inspect_sent_links.py — Stage 5 link debugger

Fetches the actual email HTML that Smartlead sent for one or more leads and
extracts every href so you can compare against what assign_push logged.

Usage:
    python scripts/inspect_sent_links.py jane@example.com
    python scripts/inspect_sent_links.py jane@example.com bob@example.com
    python scripts/inspect_sent_links.py --campaign 1234 jane@example.com

How it works:
    1. Looks up the lead in Smartlead by email (across all campaigns, or a specific one)
    2. Fetches the full message history (subject, body, sent_at for each step)
    3. Extracts all href values from the email HTML
    4. Prints a side-by-side view of step → link for each sent message

Requires: FOLLOZE_SMARTLEAD_API_KEY in env or macOS Keychain (smartlead-api)
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

# Allow running from repo root or scripts/
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.smartlead_client import SmartleadClient


def _extract_hrefs(html: str) -> list[str]:
    """Pull all href values out of an HTML string."""
    return re.findall(r'href=["\']([^"\']+)["\']', html or "")


def _inspect_lead(client: SmartleadClient, email: str, campaign_id: str | None) -> None:
    print(f"\n{'='*70}")
    print(f"  Lead: {email}")
    print(f"{'='*70}")

    # Find the lead — try the supplied campaign first, fall back to all campaigns
    campaigns_to_check: list[str] = []
    if campaign_id:
        campaigns_to_check = [campaign_id]
    else:
        all_campaigns = client.list_campaigns()
        campaigns_to_check = [str(c["id"]) for c in all_campaigns]

    found = False
    for cid in campaigns_to_check:
        leads = client.get_leads(cid, limit=200)
        matched = [
            item for item in leads
            if isinstance(item, dict)
            and (item.get("email") == email or (item.get("lead") or {}).get("email") == email)
        ]
        if not matched:
            continue

        found = True
        for item in matched:
            lead_obj = item.get("lead") or item
            sl_lead_id = str(item.get("id") or item.get("campaign_lead_map_id") or "?")
            print(f"\n  Campaign: {cid}  |  sl_lead_id: {sl_lead_id}")
            print(f"  Smartlead status: {item.get('status', 'unknown')}")

            history = client.get_lead_message_history(cid, sl_lead_id)
            if not history:
                print("  No message history yet (email not sent yet, or API returned empty)")
                continue

            for msg in history:
                seq_num  = msg.get("seq_number") or msg.get("sequence_number") or "?"
                sent_at  = msg.get("time") or msg.get("sent_at") or msg.get("created_at") or "?"
                subject  = msg.get("subject") or "(no subject)"
                body     = msg.get("email_body") or msg.get("body") or ""
                hrefs    = _extract_hrefs(body)

                print(f"\n  ── Step {seq_num}  sent_at={sent_at}")
                print(f"     Subject : {subject}")
                if hrefs:
                    print(f"     Links in sent email:")
                    for href in hrefs:
                        tag = " ← Folloze board" if "engage.folloze.com" in href else ""
                        print(f"       {href}{tag}")
                else:
                    print(f"     Links : (none found in body)")

    if not found:
        print(f"  Not found in Smartlead{'  (campaign ' + campaign_id + ')' if campaign_id else ''}")
        print(f"  Tip: check that the lead was enrolled — it may still be pending.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Inspect actual links Smartlead sent for one or more leads (Stage 5)"
    )
    parser.add_argument("emails", nargs="+", help="Email address(es) to inspect")
    parser.add_argument(
        "--campaign",
        metavar="CAMPAIGN_ID",
        default=None,
        help="Limit search to a specific Smartlead campaign ID (faster)",
    )
    args = parser.parse_args()

    client = SmartleadClient()

    for email in args.emails:
        try:
            _inspect_lead(client, email.strip(), args.campaign)
        except Exception as exc:
            print(f"\n  ERROR for {email}: {exc}")

    print()


if __name__ == "__main__":
    main()
