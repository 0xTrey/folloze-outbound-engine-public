"""
Job: sync_crm_contacts

Sync every outbound-engine contact with an email into Salesforce.

Behavior:
  - Existing Contact -> update Contact fields when changed.
  - Existing Lead -> update Lead fields when changed.
  - Missing person + matching Salesforce Account -> create Contact.
  - Missing person + no matching Account -> create Lead.

Usage:
    python main.py sync_crm_contacts --dry-run
    python main.py sync_crm_contacts
    python main.py sync_crm_contacts --contact-id <uuid>
"""
from __future__ import annotations

from collections import Counter

import psycopg2.extras

from connectors.crm_client import CRMClient
from db.connection import db_conn
from runtime_config import configure_logging


def _fetch_contacts(conn, *, batch_size: int, contact_id_filter: str | None = None) -> list[dict]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                c.contact_id::text,
                c.first_name,
                c.last_name,
                c.title,
                c.seniority,
                c.department,
                c.email,
                c.email_status,
                c.linkedin_url,
                c.persona_type,
                c.source,
                c.last_enriched_at,
                c.created_at,
                a.account_id::text,
                a.company_name,
                a.domain,
                a.segment,
                a.crm_account_id,
                COALESCE(
                    string_agg(DISTINCT ca.sequence_family, ', ' ORDER BY ca.sequence_family),
                    ''
                ) AS sequence_families
            FROM contacts c
            LEFT JOIN accounts a
                ON a.account_id = c.account_id
            LEFT JOIN campaign_assignments ca
                ON ca.contact_id = c.contact_id
            WHERE c.email IS NOT NULL
              AND c.email <> ''
              AND (%s IS NULL OR c.contact_id = %s::uuid)
            GROUP BY
                c.contact_id,
                c.first_name,
                c.last_name,
                c.title,
                c.seniority,
                c.department,
                c.email,
                c.email_status,
                c.linkedin_url,
                c.persona_type,
                c.source,
                c.last_enriched_at,
                c.created_at,
                a.account_id,
                a.company_name,
                a.domain,
                a.segment,
                a.crm_account_id
            ORDER BY c.created_at ASC
            LIMIT %s
            """,
            (contact_id_filter, contact_id_filter, batch_size),
        )
        return [dict(row) for row in cur.fetchall()]


def _persist_account_mapping(conn, *, account_id: str | None, crm_account_id: str | None) -> None:
    if not account_id or not crm_account_id:
        return
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE accounts
            SET crm_account_id = %s,
                updated_at = NOW()
            WHERE account_id = %s::uuid
              AND COALESCE(crm_account_id, '') <> %s
            """,
            (crm_account_id, account_id, crm_account_id),
        )


def _preview_action(client: CRMClient, contact: dict) -> str:
    person = client.find_person_by_email(contact.get("email") or "")
    account_id = client.find_account_id(
        domain=contact.get("domain") or "",
        company_name=contact.get("company_name") or "",
        crm_account_id=contact.get("crm_account_id") or "",
    )

    if person and person.get("_sf_type") == "Contact":
        desired = client._build_contact_sync_values(  # noqa: SLF001 - intentional dry-run preview
            contact,
            account_id or person.get("AccountId"),
        )
        updates = client._diff_fields(person, desired)  # noqa: SLF001 - intentional dry-run preview
        return "contact_update" if updates else "contact_unchanged"

    if person and person.get("_sf_type") == "Lead":
        desired = client._build_lead_sync_values(contact)  # noqa: SLF001 - intentional dry-run preview
        updates = client._diff_fields(person, desired)  # noqa: SLF001 - intentional dry-run preview
        return "lead_update" if updates else "lead_unchanged"

    return "contact_create" if account_id else "lead_create"


def run(
    *,
    dry_run: bool = False,
    batch_size: int = 1000,
    contact_id_filter: str | None = None,
) -> None:
    logger = configure_logging("sync_crm_contacts")
    client = CRMClient()

    with db_conn() as conn:
        contacts = _fetch_contacts(
            conn,
            batch_size=batch_size,
            contact_id_filter=contact_id_filter,
        )
        logger.info("Loaded %s contact(s) for CRM sync", len(contacts))

        if dry_run:
            counts = Counter()
            for contact in contacts:
                counts[_preview_action(client, contact)] += 1

            logger.info("Dry run summary: %s", dict(counts))
            for contact in contacts[:10]:
                logger.info(
                    "Preview %s <%s> @ %s -> %s",
                    (contact.get("first_name") or "").strip() or (contact.get("last_name") or "").strip() or "Unknown",
                    contact.get("email"),
                    contact.get("company_name") or contact.get("domain") or "Unknown",
                    _preview_action(client, contact),
                )
            return

        counts = Counter()
        for index, contact in enumerate(contacts, start=1):
            result = client.sync_outbound_contact(contact)
            counts[result["action"]] += 1
            _persist_account_mapping(
                conn,
                account_id=contact.get("account_id"),
                crm_account_id=result.get("account_id"),
            )
            logger.info(
                "[%s/%s] %s <%s> -> %s (%s)",
                index,
                len(contacts),
                " ".join(
                    piece for piece in [contact.get("first_name"), contact.get("last_name")] if piece
                ) or contact.get("email") or "Unknown",
                contact.get("email"),
                result["action"],
                result.get("record_id"),
            )

        logger.info("CRM sync summary: %s", dict(counts))
