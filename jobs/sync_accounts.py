"""
Job: sync_accounts
Runs hourly. Pulls ICP-matching accounts from Apollo, scores them,
upserts into accounts + contacts tables. Suppressed domains are skipped.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import psycopg2.extras

from connectors.apollo_client import ApolloClient, PERSONA_TITLES
from connectors.crm_client import CRMClient
from db.connection import db_conn
from routing.assignment import infer_persona, assign_sequence_family, detect_trigger
from runtime_config import configure_logging
from scoring.account_scorer import score_account

PAGES_TO_FETCH = 4  # 4 * 25 = 100 accounts per run
logger = logging.getLogger(__name__)


def _get_suppressed_domains(conn) -> set[str]:
    """Fetch all suppressed domains from the suppressions table."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT domain FROM suppressions
            WHERE domain IS NOT NULL
              AND (expires_at IS NULL OR expires_at > NOW())
        """)
        return {row[0] for row in cur.fetchall()}


def _upsert_account(conn, domain: str, apollo_data: dict, scores: dict) -> str:
    """Upsert one account record. Returns the account_id UUID."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO accounts (
                domain, company_name, industry, employee_count,
                geography, fit_score, timing_score, priority_score, segment,
                account_status, apollo_raw, updated_at
            ) VALUES (
                %(domain)s, %(company_name)s, %(industry)s, %(employee_count)s,
                %(geography)s, %(fit_score)s, %(timing_score)s, %(priority_score)s,
                %(segment)s, 'active', %(apollo_raw)s, NOW()
            )
            ON CONFLICT (domain) DO UPDATE SET
                company_name   = EXCLUDED.company_name,
                industry       = EXCLUDED.industry,
                employee_count = EXCLUDED.employee_count,
                geography      = EXCLUDED.geography,
                fit_score      = EXCLUDED.fit_score,
                timing_score   = EXCLUDED.timing_score,
                priority_score = EXCLUDED.priority_score,
                segment        = EXCLUDED.segment,
                apollo_raw     = EXCLUDED.apollo_raw,
                updated_at     = NOW()
            RETURNING account_id
        """, {
            "domain": domain,
            "company_name": scores.get("company_name"),
            "industry": scores.get("industry"),
            "employee_count": scores.get("employee_count"),
            "geography": scores.get("geography"),
            "fit_score": scores.get("fit_score"),
            "timing_score": scores.get("timing_score"),
            "priority_score": scores.get("priority_score"),
            "segment": scores.get("segment"),
            "apollo_raw": json.dumps(apollo_data),
        })
        row = cur.fetchone()
        return str(row[0])


def _upsert_contact(conn, account_id: str, person: dict, segment: str, trigger: str | None):
    """Upsert one contact and create a pending campaign assignment."""
    persona = infer_persona(person.get("title") or "")
    family = assign_sequence_family(segment, persona, trigger)

    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO contacts (
                account_id, first_name, last_name, title, seniority,
                email, email_status, linkedin_url, persona_type, apollo_id,
                source, last_enriched_at
            ) VALUES (
                %(account_id)s, %(first_name)s, %(last_name)s, %(title)s, %(seniority)s,
                %(email)s, %(email_status)s, %(linkedin_url)s, %(persona_type)s, %(apollo_id)s,
                'apollo', NOW()
            )
            ON CONFLICT (apollo_id) WHERE apollo_id IS NOT NULL DO NOTHING
            RETURNING contact_id
        """, {
            "account_id": account_id,
            "first_name": person.get("first_name"),
            "last_name": person.get("last_name"),
            "title": person.get("title"),
            "seniority": person.get("seniority"),
            "email": person.get("email"),
            "email_status": person.get("email_status"),
            "linkedin_url": person.get("linkedin_url"),
            "persona_type": persona,
            "apollo_id": person.get("apollo_id"),
        })
        row = cur.fetchone()
        if row is None:
            return  # already exists, skip assignment creation

        contact_id = str(row[0])

        if family:
            cur.execute("""
                INSERT INTO campaign_assignments (
                    contact_id, sequence_family, trigger_type, eligibility_status
                ) VALUES (%s, %s, %s, 'pending')
                ON CONFLICT DO NOTHING
            """, (contact_id, family, trigger))


def _get_existing_account(conn, domain: str) -> dict | None:
    """Return the stored apollo_raw for a domain if enriched within the last 30 days."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT apollo_raw FROM accounts
            WHERE domain = %s AND updated_at > NOW() - INTERVAL '30 days'
        """, (domain,))
        row = cur.fetchone()
    if row and row[0]:
        import json as _json
        return _json.loads(row[0]) if isinstance(row[0], str) else row[0]
    return None


def _process_orgs(apollo, orgs, suppressed_domains, dry_run, accounts_processed, contacts_processed):
    """Shared inner loop for both segment passes."""
    with db_conn() as conn:
        for org in orgs:
            domain = (
                org.get("primary_domain")
                or (org.get("website_url") or "").replace("https://", "").replace("http://", "").split("/")[0]
            )
            if not domain:
                continue
            if domain in suppressed_domains:
                logger.info("Skipping suppressed domain %s", domain)
                continue

            # Use search result directly — enrich_org costs a credit but adds nothing
            # the search result doesn't already have. Reserve credits for bulk_match.
            existing = _get_existing_account(conn, domain)
            apollo_data = existing or org
            scores = score_account(apollo_data)
            segment = scores.get("segment", "MM")
            trigger = detect_trigger(apollo_data)

            if dry_run:
                logger.info(
                    "[DRY RUN] %s | fit=%s timing=%s priority=%s seg=%s",
                    domain,
                    scores["fit_score"],
                    scores["timing_score"],
                    scores["priority_score"],
                    segment,
                )
                accounts_processed += 1
                continue

            account_id = _upsert_account(conn, domain, apollo_data, scores)
            accounts_processed += 1

            # Skip search_people for accounts we've already pulled contacts from.
            if existing:
                continue
            people = apollo.search_people(domains=[domain])
            for person in people:
                _upsert_contact(conn, account_id, person, segment, trigger)
                contacts_processed += 1

    return accounts_processed, contacts_processed


def _get_next_page(conn, segment_num: int) -> int:
    """Read the next starting page for a segment from kv_config. Defaults to 1."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT value FROM kv_config WHERE key = %s",
            (f"sync_accounts_next_page_seg{segment_num}",),
        )
        row = cur.fetchone()
    return int(row[0]) if row else 1


def _advance_page(conn, segment_num: int, pages_fetched: int):
    """Advance (or reset) the next starting page for a segment in kv_config."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO kv_config (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
        """, (f"sync_accounts_next_page_seg{segment_num}", str(pages_fetched)))


def run(dry_run: bool = False, pages: int = PAGES_TO_FETCH):
    configure_logging("sync_accounts")
    apollo = ApolloClient()
    crm = CRMClient()

    suppressed_from_crm = crm.get_customer_domains() | crm.get_open_opp_domains()

    logger.info("Starting. dry_run=%s, pages=%s", dry_run, pages)

    accounts_processed = 0
    contacts_processed = 0

    # Run both ICP segments — see docs/ICP.md
    for segment_num in (1, 2):
        with db_conn() as conn:
            start_page = _get_next_page(conn, segment_num)

        logger.info("=== Segment %s (starting page %s) ===", segment_num, start_page)

        pages_fetched = start_page
        for page in range(start_page, start_page + pages):
            logger.info("Segment %s page %s...", segment_num, page)

            with db_conn() as conn:
                suppressed_domains = _get_suppressed_domains(conn) | suppressed_from_crm

            orgs = apollo.search_companies(segment=segment_num, page=page)
            if not orgs:
                logger.info(
                    "No results on segment %s page %s, resetting to page 1.",
                    segment_num,
                    page,
                )
                pages_fetched = 1  # wrap around — exhausted this segment's results
                break

            pages_fetched = page + 1
            accounts_processed, contacts_processed = _process_orgs(
                apollo, orgs, suppressed_domains, dry_run,
                accounts_processed, contacts_processed
            )

        if not dry_run:
            with db_conn() as conn:
                _advance_page(conn, segment_num, pages_fetched)
                conn.commit()
            logger.info(
                "Segment %s next run will start at page %s",
                segment_num,
                pages_fetched,
            )

    logger.info(
        "Done. accounts=%s contacts=%s",
        accounts_processed,
        contacts_processed,
    )
