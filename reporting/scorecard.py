"""
Job: scorecard
Generates a daily KPI summary from sends, campaign_assignments, and personalization_payloads.
"""
from __future__ import annotations

import psycopg2.extras

from db.connection import db_conn


def run(dry_run: bool = False):
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:

            # Payload generation stats
            cur.execute("""
                SELECT
                    qa_status,
                    COUNT(*) AS count,
                    MAX(created_at) AS latest
                FROM personalization_payloads
                GROUP BY qa_status
                ORDER BY qa_status
            """)
            payload_stats = cur.fetchall()

            # Assignment funnel
            cur.execute("""
                SELECT
                    eligibility_status,
                    COUNT(*) AS count
                FROM campaign_assignments
                GROUP BY eligibility_status
                ORDER BY eligibility_status
            """)
            assignment_stats = cur.fetchall()

            # Account pipeline
            cur.execute("""
                SELECT
                    segment,
                    COUNT(*) AS accounts,
                    ROUND(AVG(priority_score), 2) AS avg_priority
                FROM accounts
                WHERE account_status = 'active'
                GROUP BY segment
                ORDER BY segment
            """)
            account_stats = cur.fetchall()

            # Send stats (Phase 2 — will be empty until Smartlead is active)
            cur.execute("""
                SELECT
                    delivery_status,
                    COUNT(*) AS count
                FROM sends
                GROUP BY delivery_status
                ORDER BY delivery_status
            """)
            send_stats = cur.fetchall()

    print("=" * 50)
    print("FOLLOZE OUTBOUND ENGINE — DAILY SCORECARD")
    print("=" * 50)

    print("\n--- Personalization Payloads ---")
    for row in payload_stats:
        print(f"  {row['qa_status']}: {row['count']}")

    print("\n--- Assignment Funnel ---")
    for row in assignment_stats:
        print(f"  {row['eligibility_status']}: {row['count']}")

    print("\n--- Account Pipeline ---")
    for row in account_stats:
        print(f"  {row['segment']}: {row['accounts']} accounts | avg priority: {row['avg_priority']}")

    print("\n--- Sends (Phase 2) ---")
    if send_stats:
        for row in send_stats:
            print(f"  {row['delivery_status']}: {row['count']}")
    else:
        print("  No sends yet.")

    print("=" * 50)
