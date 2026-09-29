"""
Job: re_engage

Implements the 45-day re-engagement cycle.

Finds contacts who:
  - Completed a send cycle (last_sent_at is set)
  - Have been silent for >= CYCLE_DAYS (default 45)
  - Have no positive reply (interested / referral) in sends
  - Are not suppressed
  - Have no pending or eligible assignment in flight

For each eligible contact:
  1. Rotates to a fresh sequence family (different angle than the last cycle)
  2. Creates a new campaign_assignment with cycle_number incremented
  3. Invalidates their old payload so build_payloads regenerates with fresh context

Run order: re_engage → build_payloads → assign_push

Usage:
    python main.py re_engage --dry-run
    python main.py re_engage [--batch N] [--cycle-days N]
"""
from __future__ import annotations

from datetime import datetime, timezone

import psycopg2.extras

from db.connection import db_conn
from routing.assignment import assign_sequence_family

CYCLE_DAYS = 45
BATCH_SIZE = 100

# When a contact re-engages, rotate to a different angle within the same segment.
# The goal is a fresh hook — not a repeat of the last touch.
FAMILY_ROTATION: dict[str, str] = {
    "ENT_REVMKTG_INTENT":  "ENT_REVMKTG_HIRING",
    "ENT_REVMKTG_HIRING":  "ENT_ABM_INTENT",
    "ENT_ABM_INTENT":      "ENT_REVMKTG_INTENT",
    "MM_REVMKTG_TIMING":   "MM_CMO_EXEC",
    "MM_REVOPS_INTENT":    "MM_REVMKTG_TIMING",
    "MM_CMO_EXEC":         "MM_REVMKTG_TIMING",
}


def _rotate_family(last_family: str) -> str:
    """Return the next sequence family in the rotation for this contact."""
    return FAMILY_ROTATION.get(last_family, last_family)


def _fetch_eligible_contacts(conn, cycle_days: int, limit: int) -> list[dict]:
    """
    Return contacts eligible for re-engagement.

    Eligibility rules:
      - last_sent_at is set and older than cycle_days
      - not in suppressions (email or domain)
      - no positive reply (interested / referral) in sends
      - no pending or eligible assignment currently in flight
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                c.contact_id,
                c.first_name,
                c.last_name,
                c.email,
                c.persona_type,
                a.segment,
                c.last_sent_at,
                ca.sequence_family   AS last_family,
                MAX(ca.cycle_number) AS last_cycle
            FROM contacts c
            JOIN accounts a ON a.account_id = c.account_id
            JOIN campaign_assignments ca ON ca.contact_id = c.contact_id
            WHERE c.last_sent_at IS NOT NULL
              AND c.last_sent_at < NOW() - INTERVAL '{days} days'
              AND a.account_status = 'active'
              AND c.email NOT IN (
                  SELECT email FROM suppressions
                  WHERE email IS NOT NULL
                    AND (expires_at IS NULL OR expires_at > NOW())
              )
              AND a.domain NOT IN (
                  SELECT domain FROM suppressions
                  WHERE domain IS NOT NULL
                    AND (expires_at IS NULL OR expires_at > NOW())
              )
              AND c.contact_id NOT IN (
                  SELECT DISTINCT contact_id FROM sends
                  WHERE reply_type IN ('interested', 'referral')
              )
              AND c.contact_id NOT IN (
                  SELECT contact_id FROM campaign_assignments
                  WHERE eligibility_status IN ('pending', 'eligible')
              )
            GROUP BY
                c.contact_id, c.first_name, c.last_name, c.email,
                c.persona_type, a.segment, c.last_sent_at, ca.sequence_family
            ORDER BY c.last_sent_at ASC
            LIMIT %s
        """.format(days=cycle_days), (limit,))
        return [dict(row) for row in cur.fetchall()]


def _invalidate_old_payload(conn, contact_id: str):
    """Mark existing approved payload as superseded so build_payloads regenerates it."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE personalization_payloads
            SET qa_status = 'superseded'
            WHERE contact_id = %s
              AND qa_status = 'approved'
        """, (contact_id,))


def _create_assignment(conn, contact_id: str, family: str, cycle_number: int):
    """Insert a new pending campaign assignment for the re-engagement cycle."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO campaign_assignments (
                contact_id, sequence_family, eligibility_status, cycle_number
            ) VALUES (%s, %s, 'pending', %s)
        """, (contact_id, family, cycle_number))


def run(dry_run: bool = False, batch_size: int = BATCH_SIZE, cycle_days: int = CYCLE_DAYS):
    print(f"[re_engage] Starting. dry_run={dry_run} cycle_days={cycle_days} batch={batch_size}")

    with db_conn() as conn:
        contacts = _fetch_eligible_contacts(conn, cycle_days, batch_size)

    print(f"[re_engage] {len(contacts)} contacts eligible for re-engagement")

    if not contacts:
        print("[re_engage] Nothing to do.")
        return

    queued = 0
    skipped = 0

    with db_conn() as conn:
        for c in contacts:
            contact_id = str(c["contact_id"])
            last_family = c["last_family"] or ""
            last_cycle = int(c["last_cycle"] or 1)
            next_cycle = last_cycle + 1
            next_family = _rotate_family(last_family)

            name = f"{c['first_name'] or ''} {c['last_name'] or ''}".strip()
            days_since = (datetime.now(timezone.utc) - c["last_sent_at"]).days

            if dry_run:
                print(
                    f"  [DRY RUN] {name} <{c['email']}> — "
                    f"silent {days_since}d | {last_family} → {next_family} | cycle {next_cycle}"
                )
                queued += 1
                continue

            _invalidate_old_payload(conn, contact_id)
            _create_assignment(conn, contact_id, next_family, next_cycle)

            print(
                f"  [queued] {name} <{c['email']}> — "
                f"{last_family} → {next_family} | cycle {next_cycle} | {days_since}d silent"
            )
            queued += 1

        if not dry_run:
            conn.commit()

    print(f"[re_engage] Done. queued={queued} skipped={skipped}")
    if not dry_run and queued:
        print(f"[re_engage] Next step: python main.py build_payloads")
