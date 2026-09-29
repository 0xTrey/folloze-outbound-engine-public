"""
Job: patch_sequences
Replaces the sequence templates for all 6 live Smartlead campaigns with the
current 2-step template.

POST /campaigns/{id}/sequences replaces the entire sequence — partial posts delete the others.
Safe to re-run (idempotent).

Usage:
    python main.py patch_sequences --dry-run
    python main.py patch_sequences
"""
from __future__ import annotations

from connectors.smartlead_client import SmartleadClient
from db.connection import db_conn
from jobs.setup_campaigns import SIGNATURE, STEP_1_BODY, STEP_2_BODY


UPDATED_STEPS = [
    {
        "seq_number": 1,
        "subject": "{{ai_subject}}",
        "email_body": STEP_1_BODY,
        "seq_delay_details": {"delay_in_days": 0},
    },
    {
        "seq_number": 2,
        "subject": "",  # threaded reply — sends as "Re: [T1 subject]"
        "email_body": STEP_2_BODY,
        "seq_delay_details": {"delay_in_days": 3},
    },
]


def _load_active_campaign_ids(conn) -> dict[str, str]:
    """Returns {family_name: smartlead_campaign_id} for all active families."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT family_name, smartlead_campaign_id
            FROM sequence_families
            WHERE is_active = TRUE
              AND smartlead_campaign_id IS NOT NULL
        """)
        return {row[0]: row[1] for row in cur.fetchall()}


def run(dry_run: bool = False):
    print(f"[patch_sequences] Starting. dry_run={dry_run}")

    with db_conn() as conn:
        campaign_map = _load_active_campaign_ids(conn)

    if not campaign_map:
        print("[patch_sequences] No campaigns with smartlead_campaign_id found.")
        return

    print(f"[patch_sequences] {len(campaign_map)} campaigns to patch: {list(campaign_map.keys())}")

    if dry_run:
        for family_name, campaign_id in campaign_map.items():
            print(f"\n  [{family_name}] campaign_id={campaign_id}")
            for step in UPDATED_STEPS:
                preview = step["email_body"][:100].replace("\n", " ↵ ")
                subj = f"'{step['subject']}'" if step["subject"] else "(threaded reply)"
                print(f"    Step {step['seq_number']} | day+{step['seq_delay_details']['delay_in_days']} | subject={subj}")
                print(f"      body: {preview}...")
        print("\n[patch_sequences] Dry run complete — no changes made.")
        return

    sl = SmartleadClient()
    patched = 0

    for family_name, campaign_id in campaign_map.items():
        print(f"\n  [{family_name}] campaign_id={campaign_id}")
        try:
            result = sl.replace_sequences(campaign_id, UPDATED_STEPS)
            print(f"    OK: {result}")
            patched += 1
        except Exception as e:
            print(f"    [ERROR] {e}")

    print(f"\n[patch_sequences] Done. patched={patched}/{len(campaign_map)}")
