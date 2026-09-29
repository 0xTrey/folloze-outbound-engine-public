"""
Job: reveal_emails
Bulk-reveals email addresses for contacts that were discovered via Apollo
api_search (which returns obfuscated profiles). Calls people/bulk_match in
batches of 10 to fill in email, last_name, and linkedin_url.

Run after sync_accounts, before enrich_research.

Usage:
    python main.py reveal_emails [--batch 50] [--dry-run]
"""
from __future__ import annotations

import logging

from connectors.apollo_client import ApolloClient
from db.connection import db_conn
from runtime_config import configure_logging

BATCH_SIZE = 10  # Apollo bulk_match accepts up to 10 per call
logger = logging.getLogger(__name__)


def _get_unrevealed_contacts(conn, limit: int) -> list[dict]:
    """Return contacts with an apollo_id but no email yet."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT contact_id, apollo_id
            FROM contacts
            WHERE apollo_id IS NOT NULL
              AND email IS NULL
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        return [{"contact_id": str(r[0]), "apollo_id": r[1]} for r in cur.fetchall()]


def _apply_reveal(conn, contact_id: str, match: dict):
    """Write revealed email + profile fields back to the contact, and re-open any
    assignment that was blocked solely due to missing email."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE contacts SET
                last_name    = COALESCE(last_name, %(last_name)s),
                email        = %(email)s,
                email_status = %(email_status)s,
                linkedin_url = COALESCE(linkedin_url, %(linkedin_url)s),
                seniority    = COALESCE(seniority, %(seniority)s)
            WHERE contact_id = %(contact_id)s
        """, {
            "contact_id": contact_id,
            "last_name": match.get("last_name"),
            "email": match.get("email"),
            "email_status": match.get("email_status"),
            "linkedin_url": match.get("linkedin_url"),
            "seniority": match.get("seniority"),
        })
        # Re-open assignments that were blocked only because email was missing.
        # Any other ineligibility reason (crm_suppressed, push_error, etc.) stays closed.
        cur.execute("""
            UPDATE campaign_assignments
            SET eligibility_status = 'pending', ineligibility_reason = NULL
            WHERE contact_id = %s
              AND eligibility_status = 'ineligible'
              AND (ineligibility_reason IS NULL
                   OR ineligibility_reason IN ('no_email_apollo_exhausted', 'no_email'))
        """, (contact_id,))


def _mark_ineligible(conn, contact_id: str):
    """Mark pending assignments ineligible for contacts with no email."""
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE campaign_assignments
            SET eligibility_status = 'ineligible'
            WHERE contact_id = %s AND eligibility_status = 'pending'
        """, (contact_id,))


def run(dry_run: bool = False, batch: int = 100):
    configure_logging("reveal_emails")
    logger.info("Starting. dry_run=%s, batch=%s", dry_run, batch)
    apollo = ApolloClient()

    with db_conn() as conn:
        contacts = _get_unrevealed_contacts(conn, batch)

    if not contacts:
        logger.info("No contacts to reveal.")
        return

    logger.info("%s contacts to reveal.", len(contacts))

    revealed = 0
    skipped_no_email = 0
    failed = 0

    # Process in chunks of BATCH_SIZE
    for i in range(0, len(contacts), BATCH_SIZE):
        chunk = contacts[i:i + BATCH_SIZE]
        ids = [c["apollo_id"] for c in chunk]
        id_to_contact = {c["apollo_id"]: c["contact_id"] for c in chunk}

        if dry_run:
            logger.info("[DRY RUN] Would reveal %s contacts: %s", len(chunk), ids)
            continue

        matches = apollo.reveal_emails(ids)

        with db_conn() as conn:
            for apollo_id, match in matches.items():
                contact_id = id_to_contact.get(apollo_id)
                if not contact_id:
                    continue
                if not match.get("email"):
                    # No email available — mark ineligible so it never blocks the queue
                    _mark_ineligible(conn, contact_id)
                    skipped_no_email += 1
                    continue
                try:
                    _apply_reveal(conn, contact_id, match)
                    revealed += 1
                except Exception as e:
                    logger.error("contact_id=%s reveal apply failed: %s", contact_id, e)
                    failed += 1

        # Contacts Apollo returned no match for at all — also ineligible
        unmatched_ids = set(ids) - set(matches.keys())
        unmatched_contact_ids = [id_to_contact[aid] for aid in unmatched_ids if aid in id_to_contact]
        with db_conn() as conn:
            for contact_id in unmatched_contact_ids:
                _mark_ineligible(conn, contact_id)
        skipped_no_email += len(unmatched_ids)

    logger.info(
        "Done. revealed=%s no_email=%s failed=%s",
        revealed,
        skipped_no_email,
        failed,
    )
