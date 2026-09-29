"""
Emergency pause operations. Call directly from CLI or via main.py.

Usage:
  python main.py kill_domain --domain acme.com
  python main.py kill_campaign --campaign ENT_REVMKTG_INTENT
  python main.py kill_contact --email jane@acme.com
"""
from __future__ import annotations

from db.connection import db_conn


def pause_domain(domain: str, reason: str = "manual"):
    """Suppress all contacts from a domain and pause in-flight assignments."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            # Add to suppressions
            cur.execute("""
                INSERT INTO suppressions (domain, reason, source)
                VALUES (%s, %s, 'kill_switch')
                ON CONFLICT DO NOTHING
            """, (domain, reason))

            # Mark account as suppressed
            cur.execute("""
                UPDATE accounts SET account_status = 'suppressed'
                WHERE domain = %s
            """, (domain,))

            # Cancel pending assignments for all contacts at this domain
            cur.execute("""
                UPDATE campaign_assignments ca
                SET eligibility_status = 'ineligible',
                    ineligibility_reason = 'domain suppressed: ' || %s
                FROM contacts c
                JOIN accounts a ON c.account_id = a.account_id
                WHERE ca.contact_id = c.contact_id
                  AND a.domain = %s
                  AND ca.eligibility_status IN ('pending', 'eligible')
            """, (reason, domain))

    print(f"[kill_switches] Domain suppressed: {domain} (reason: {reason})")


def pause_campaign(sequence_family: str):
    """Set a sequence family to inactive and cancel all pending assignments."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE sequence_families
                SET is_active = FALSE
                WHERE family_name = %s
            """, (sequence_family,))

            cur.execute("""
                UPDATE campaign_assignments
                SET eligibility_status = 'ineligible',
                    ineligibility_reason = 'campaign paused'
                WHERE sequence_family = %s
                  AND eligibility_status IN ('pending', 'eligible')
            """, (sequence_family,))

    print(f"[kill_switches] Campaign paused: {sequence_family}")


def suppress_contact(email: str, reason: str = "manual"):
    """Add a contact's email to suppressions and cancel their assignments."""
    with db_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO suppressions (email, reason, source)
                VALUES (%s, %s, 'kill_switch')
                ON CONFLICT DO NOTHING
            """, (email, reason))

            cur.execute("""
                UPDATE campaign_assignments ca
                SET eligibility_status = 'ineligible',
                    ineligibility_reason = 'contact suppressed'
                FROM contacts c
                WHERE ca.contact_id = c.contact_id
                  AND c.email = %s
                  AND ca.eligibility_status IN ('pending', 'eligible')
            """, (email,))

    print(f"[kill_switches] Contact suppressed: {email}")
