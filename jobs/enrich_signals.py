"""
Job: enrich_signals
Enriches accounts and contacts with recent intelligence via Perplexity API.

Run order: enrich_signals → build_payloads
Skips records enriched within the last 7 days (configurable via SIGNALS_STALE_DAYS).

Account signals: company-level (funding, hiring, product, leadership, news)
Contact signals: individual-level (LinkedIn posts, conference talks, press quotes, job changes)

Both signal types are injected into the LLM prompt during build_payloads for
more specific and timely personalization.

Usage:
    python main.py enrich_signals [--dry-run] [--batch N] [--stale-days N] [--domain example.com]
    python main.py enrich_signals --contacts-only   # skip account enrichment
    python main.py enrich_signals --accounts-only   # skip contact enrichment
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone

import psycopg2.extras

from connectors.perplexity_client import PerplexityClient
from db.connection import db_conn

BATCH_SIZE = 20
STALE_DAYS = int(os.environ.get("SIGNALS_STALE_DAYS", "7"))


def _fetch_stale_accounts(conn, limit: int, stale_days: int, domain_filter: str | None) -> list[dict]:
    """Return accounts without signals or with signals older than stale_days."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        base_query = """
            SELECT account_id, company_name, domain, industry, employee_count,
                   signals, signals_enriched_at
            FROM accounts
            WHERE account_status = 'active'
              AND (
                  signals_enriched_at IS NULL
                  OR signals_enriched_at < NOW() - INTERVAL '{stale_days} days'
              )
        """.format(stale_days=stale_days)

        if domain_filter:
            cur.execute(base_query + " AND domain = %s ORDER BY priority_score DESC NULLS LAST LIMIT %s",
                        (domain_filter, limit))
        else:
            cur.execute(base_query + " ORDER BY priority_score DESC NULLS LAST LIMIT %s",
                        (limit,))

        return [dict(row) for row in cur.fetchall()]


def _write_signals(conn, account_id: str, signals_data: dict):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE accounts
            SET signals = %s::jsonb,
                signals_enriched_at = %s,
                updated_at = %s
            WHERE account_id = %s
        """, (
            json.dumps(signals_data),
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
            account_id,
        ))


def _fetch_stale_contacts(conn, limit: int, stale_days: int) -> list[dict]:
    """Return contacts without signals or with signals older than stale_days,
    who have a pending campaign assignment (worth enriching)."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT c.contact_id, c.first_name, c.last_name, c.title,
                   a.company_name, a.domain, c.contact_signals_enriched_at
            FROM contacts c
            JOIN accounts a ON c.account_id = a.account_id
            JOIN campaign_assignments ca ON ca.contact_id = c.contact_id
            WHERE ca.eligibility_status IN ('pending', 'eligible')
              AND (
                  c.contact_signals_enriched_at IS NULL
                  OR c.contact_signals_enriched_at < NOW() - INTERVAL '{stale_days} days'
              )
            ORDER BY a.priority_score DESC NULLS LAST
            LIMIT %s
        """.format(stale_days=stale_days), (limit,))
        return [dict(row) for row in cur.fetchall()]


def _write_contact_signals(conn, contact_id: str, signals_data: dict):
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE contacts
            SET contact_signals = %s::jsonb,
                contact_signals_enriched_at = %s
            WHERE contact_id = %s
        """, (
            json.dumps(signals_data),
            datetime.now(timezone.utc),
            contact_id,
        ))


def run(
    dry_run: bool = False,
    batch_size: int = BATCH_SIZE,
    stale_days: int = STALE_DAYS,
    domain_filter: str | None = None,
    contacts_only: bool = False,
    accounts_only: bool = False,
):
    print(f"[enrich_signals] Starting. dry_run={dry_run}, batch={batch_size}, stale_days={stale_days}")

    client = PerplexityClient()

    # --- Account enrichment ---
    if not contacts_only:
        acct_enriched = 0
        acct_failed = 0

        with db_conn() as conn:
            accounts = _fetch_stale_accounts(conn, batch_size, stale_days, domain_filter)
            print(f"[enrich_signals] {len(accounts)} accounts to enrich")

            for acct in accounts:
                company = acct["company_name"] or acct["domain"]
                domain = acct["domain"]
                account_id = str(acct["account_id"])

                print(f"  [account] {company} ({domain})")
                signals_data = client.enrich_company(company_name=company, domain=domain)
                summary = signals_data.get("summary", "")
                signals = signals_data.get("signals", [])

                if dry_run:
                    print(f"    [DRY RUN] {summary[:100]}")
                    for s in signals:
                        print(f"    [{s.get('type','?')}] {s.get('text','')[:100]}")
                    acct_enriched += 1
                    continue

                if not summary and not signals:
                    print(f"    [WARN] No signals for {company}")
                    acct_failed += 1
                    continue

                _write_signals(conn, account_id, signals_data)
                print(f"    {len(signals)} account signals stored")
                acct_enriched += 1

        print(f"[enrich_signals] Accounts: enriched={acct_enriched} failed={acct_failed}")

    # --- Contact enrichment ---
    if not accounts_only:
        contact_enriched = 0
        contact_failed = 0

        with db_conn() as conn:
            contacts = _fetch_stale_contacts(conn, batch_size, stale_days)
            print(f"[enrich_signals] {len(contacts)} contacts to enrich")

            for c in contacts:
                first = c["first_name"] or ""
                last = c["last_name"] or ""
                title = c["title"] or ""
                company = c["company_name"] or c["domain"]
                contact_id = str(c["contact_id"])

                print(f"  [contact] {first} {last} — {title} @ {company}")
                signals_data = client.research_contact(
                    first_name=first,
                    last_name=last,
                    title=title,
                    company=company,
                )
                summary = signals_data.get("summary", "")
                signals = signals_data.get("signals", [])

                if dry_run:
                    print(f"    [DRY RUN] {summary[:100]}")
                    for s in signals:
                        print(f"    [{s.get('type','?')}] {s.get('text','')[:100]}")
                    contact_enriched += 1
                    continue

                # Write even if signals is empty — marks enrichment attempt so we don't retry immediately
                _write_contact_signals(conn, contact_id, signals_data)
                print(f"    {len(signals)} contact signals stored")
                contact_enriched += 1 if summary or signals else 0
                contact_failed += 0 if summary or signals else 1

        print(f"[enrich_signals] Contacts: enriched={contact_enriched} failed={contact_failed}")
