"""
Job: enrich_research

Collects all available intelligence for each contact and stores it in
contact_research (one row per contact, upserted on re-run). Records are
considered stale after RESEARCH_STALE_DAYS (default 7) and re-enriched.

Sources (in priority order):
  Perplexity  — company signals + contact signals (primary)
  Brave       — company fallback when Perplexity returns < 2 signals
  Brave       — contact fallback when Perplexity returns < 1 signal
  X / Twitter — exec personas only (cmo, revmktg_vp, abm_leader)

After collecting signals, runs a single local LLM call to produce:
  brief_text    — pre-formatted string injected directly into build_payloads prompt
  brief_summary — 2-3 sentence human-readable summary for review

build_payloads reads brief_text from this table. Contacts with no research
row are skipped by build_payloads and reported as needing enrich_research first.

Recommended run order:
  1. sync_accounts
  2. enrich_research   ← this job
  3. build_payloads

Usage:
  python main.py enrich_research [--dry-run] [--batch N] [--stale-days N]
  python main.py enrich_research --domain example.com
  python main.py enrich_research --contact-id <uuid>
"""
from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import psycopg2.extras

from db.connection import db_conn
from llm_gateway import LLMGateway
from research.prospect_researcher import format_brief_for_prompt, research as run_research
from runtime_config import configure_logging

BATCH_SIZE = 20
STALE_DAYS = int(os.environ.get("RESEARCH_STALE_DAYS", "7"))
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _fetch_contacts_needing_research(
    conn,
    limit: int,
    stale_days: int,
    domain_filter: str | None,
    contact_id_filter: str | None,
) -> list[dict]:
    """
    Return contacts with a pending/eligible assignment that have no research
    row or have a stale one. Ordered by account priority descending.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        query = """
            SELECT
                c.contact_id,
                c.first_name,
                c.last_name,
                c.title,
                c.persona_type,
                c.linkedin_url,
                a.account_id,
                a.company_name,
                a.domain,
                a.industry,
                a.employee_count,
                a.segment,
                a.apollo_raw,
                cr.enriched_at AS research_enriched_at
            FROM contacts c
            JOIN accounts a ON c.account_id = a.account_id
            JOIN campaign_assignments ca ON ca.contact_id = c.contact_id
            LEFT JOIN contact_research cr ON cr.contact_id = c.contact_id
            WHERE ca.eligibility_status IN ('pending', 'eligible')
              AND c.email IS NOT NULL
              AND c.email_status NOT IN ('invalid')
              AND (
                  cr.research_id IS NULL
                  OR cr.enriched_at < NOW() - INTERVAL '{stale_days} days'
              )
        """.format(stale_days=stale_days)

        params: list = []

        if domain_filter:
            query += " AND a.domain = %s"
            params.append(domain_filter)

        if contact_id_filter:
            query += " AND c.contact_id = %s"
            params.append(contact_id_filter)

        query += " ORDER BY a.priority_score DESC NULLS LAST LIMIT %s"
        params.append(limit)

        cur.execute(query, params)
        return [dict(row) for row in cur.fetchall()]


def _upsert_research(conn, contact_id: str, data: dict):
    """Insert or update the research row for a contact."""
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO contact_research (
                contact_id,
                brief_text,
                brief_summary,
                hook_angle,
                company_snapshot,
                people_intel,
                trigger_events,
                tech_context,
                competitive_notes,
                recent_posts,
                sources_used,
                model_used,
                enriched_at
            ) VALUES (
                %(contact_id)s,
                %(brief_text)s,
                %(brief_summary)s,
                %(hook_angle)s,
                %(company_snapshot)s,
                %(people_intel)s,
                %(trigger_events)s::jsonb,
                %(tech_context)s,
                %(competitive_notes)s,
                %(recent_posts)s::jsonb,
                %(sources_used)s,
                %(model_used)s,
                %(enriched_at)s
            )
            ON CONFLICT (contact_id) DO UPDATE SET
                brief_text       = EXCLUDED.brief_text,
                brief_summary    = EXCLUDED.brief_summary,
                hook_angle       = EXCLUDED.hook_angle,
                company_snapshot = EXCLUDED.company_snapshot,
                people_intel     = EXCLUDED.people_intel,
                trigger_events   = EXCLUDED.trigger_events,
                tech_context     = EXCLUDED.tech_context,
                competitive_notes = EXCLUDED.competitive_notes,
                recent_posts     = EXCLUDED.recent_posts,
                sources_used     = EXCLUDED.sources_used,
                model_used       = EXCLUDED.model_used,
                enriched_at      = EXCLUDED.enriched_at
        """, {
            "contact_id":        contact_id,
            "brief_text":        data.get("brief_text") or "",
            "brief_summary":     data.get("brief_summary"),
            "hook_angle":        data.get("hook_angle") or "",
            "company_snapshot":  data.get("company_snapshot") or "",
            "people_intel":      data.get("people_intel") or "",
            "trigger_events":    json.dumps(data.get("trigger_events") or []),
            "tech_context":      data.get("tech_context") or "",
            "competitive_notes": data.get("competitive_notes") or "",
            "recent_posts":      json.dumps(data.get("recent_posts") or []),
            "sources_used":      data.get("sources_used") or [],
            "model_used":        data.get("model_used"),
            "enriched_at":       datetime.now(timezone.utc),
        })


# ---------------------------------------------------------------------------
# LLM summarization
# ---------------------------------------------------------------------------

_SUMMARY_SYSTEM = (
    "You are a sales research analyst. Given a research brief about a prospect, "
    "write 2-3 sentences summarizing: who this person is, what their company is doing "
    "right now, and the single strongest signal for outreach. Be specific and factual. "
    "No flattery. Return only the summary — no labels, no headers, no quotes."
)


def _generate_summary(brief_text: str) -> tuple[str | None, str | None]:
    """
    Run a fast local LLM call to produce a human-readable brief_summary
    from the pre-formatted brief_text. Returns (summary, model_used).
    Falls back to nvidia if local fails. Returns (None, None) on total failure.
    """
    if not brief_text.strip():
        return None, None

    for profile in ("local", "workhorse"):
        try:
            gw = LLMGateway(profile=profile)
            response = gw.chat([
                {"role": "system", "content": _SUMMARY_SYSTEM},
                {"role": "user", "content": brief_text},
            ])
            summary = response.strip()
            # Strip any accidental markdown
            summary = re.sub(r"^[`\"']+|[`\"']+$", "", summary).strip()
            if summary:
                return summary, profile
        except Exception as e:
            logger.warning("LLM summary failed on profile=%s: %s", profile, e)

    return None, None


# ---------------------------------------------------------------------------
# Per-contact research worker (runs in thread pool)
# ---------------------------------------------------------------------------

def _research_one(row: dict) -> dict:
    """
    Run the full research pipeline for one contact.
    Called from a thread pool — no DB access here (DB writes happen on the main thread).
    Returns a result dict with status and data, or an error dict.
    """
    contact_id = str(row["contact_id"])
    first      = row["first_name"] or ""
    last       = row["last_name"] or ""
    title      = row["title"] or ""
    company    = row["company_name"] or row["domain"]
    domain     = row["domain"] or ""
    persona    = row["persona_type"] or ""

    apollo_raw = row.get("apollo_raw") or {}
    contact = {
        "contact_id":   contact_id,
        "first_name":   first,
        "last_name":    last,
        "title":        title,
        "persona_type": persona,
        "linkedin_url": row.get("linkedin_url"),
    }
    account = {
        "account_id":    str(row["account_id"]),
        "company_name":  company,
        "domain":        domain,
        "industry":      row.get("industry") or "",
        "employee_count": row.get("employee_count"),
        "segment":       row.get("segment") or "",
        "tech_stack":    apollo_raw.get("current_technologies") or [],
        "keywords":      apollo_raw.get("keywords") or [],
    }

    try:
        brief = run_research(contact, account)
    except Exception as e:
        return {"contact_id": contact_id, "label": f"{first} {last} @ {company}", "error": str(e)}

    sources = brief.get("sources_used") or []
    if not sources:
        return {"contact_id": contact_id, "label": f"{first} {last} @ {company}", "empty": True}

    brief_text = format_brief_for_prompt(brief)
    brief_summary, model_used = _generate_summary(brief_text)

    return {
        "contact_id":    contact_id,
        "label":         f"{first} {last} @ {company}",
        "brief":         brief,
        "brief_text":    brief_text,
        "brief_summary": brief_summary,
        "model_used":    model_used,
        "sources":       sources,
    }


# ---------------------------------------------------------------------------
# Main run
# ---------------------------------------------------------------------------

ENRICH_WORKERS = 5  # parallel research threads


def run(
    dry_run: bool = False,
    batch_size: int = BATCH_SIZE,
    stale_days: int = STALE_DAYS,
    domain_filter: str | None = None,
    contact_id_filter: str | None = None,
):
    configure_logging("enrich_research")
    logger.info(
        "Starting. dry_run=%s, batch=%s, stale_days=%s",
        dry_run,
        batch_size,
        stale_days,
    )

    with db_conn() as conn:
        contacts = _fetch_contacts_needing_research(
            conn, batch_size, stale_days, domain_filter, contact_id_filter
        )

    logger.info(
        "%s contacts to research (workers=%s)",
        len(contacts),
        ENRICH_WORKERS,
    )

    # Research all contacts in parallel (I/O-bound: Perplexity + Brave + X + LLM)
    results = []
    with ThreadPoolExecutor(max_workers=ENRICH_WORKERS) as executor:
        futures = {executor.submit(_research_one, row): row for row in contacts}
        for future in as_completed(futures):
            results.append(future.result())

    # Write results to DB sequentially on the main thread
    enriched = 0
    skipped_empty = 0
    failed = 0

    with db_conn() as conn:
        for result in results:
            label = result.get("label", result.get("contact_id", "?"))

            if "error" in result:
                logger.error("%s: %s", label, result["error"])
                failed += 1
                continue

            if result.get("empty"):
                logger.warning("%s: no sources returned — skipping", label)
                skipped_empty += 1
                continue

            sources    = result["sources"]
            brief_text = result["brief_text"]
            brief_summary = result.get("brief_summary")

            if brief_summary:
                logger.info("%s: %s...", label, brief_summary[:120])
            else:
                logger.warning(
                    "%s: LLM summary failed — brief_text stored, brief_summary null",
                    label,
                )

            if dry_run:
                logger.info("[DRY RUN] sources=%s", sources)
                logger.info(
                    "hook_angle: %s",
                    result["brief"].get("hook_angle", "")[:100],
                )
                if brief_text:
                    logger.info("brief_text (%s chars)", len(brief_text))
                enriched += 1
                continue

            _upsert_research(conn, result["contact_id"], {
                **result["brief"],
                "brief_text":    brief_text,
                "brief_summary": brief_summary,
                "model_used":    result.get("model_used"),
            })
            enriched += 1
            logger.info(
                "%s: stored (%s sources, %s chars)",
                label,
                len(sources),
                len(brief_text),
            )

    logger.info(
        "Done. enriched=%s skipped_empty=%s failed=%s",
        enriched,
        skipped_empty,
        failed,
    )
