"""
Job: build_payloads
Runs hourly. Finds contacts with a pending/eligible assignment and no approved
payload, then generates bounded AI personalization snippets and writes results.

Requires enrich_research to have run first. Contacts with no entry in
contact_research are skipped and reported — run enrich_research to populate them.
"""
from __future__ import annotations

from datetime import datetime, timezone
import logging

import psycopg2.extras

import json

from db.connection import db_conn
from personalization.generator import generate_payload
from personalization.objection_model import predict_objection
from scoring.account_scorer import _recency_boost
from scoring.contact_scorer import infer_trajectory
from runtime_config import configure_logging

BATCH_SIZE = 50
logger = logging.getLogger(__name__)

# These families generate standalone copy and do not require a contact_research row.
RESEARCH_NOT_REQUIRED = {"GTME_VIBE_MKT"}


def _fetch_pending_contacts(conn, limit: int) -> list[dict]:
    """
    Return contacts with an eligible assignment and no approved payload.
    Joins contact_research so brief_text is available directly.
    Contacts with no research row are returned with brief_text=None and
    excluded from LLM generation — they need enrich_research first.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("""
            SELECT
                c.contact_id,
                c.first_name,
                c.last_name,
                c.title,
                c.seniority,
                c.email,
                c.persona_type,
                a.account_id,
                a.company_name,
                a.domain,
                a.industry,
                a.employee_count,
                a.segment,
                a.priority_score,
                a.signals,
                a.apollo_raw,
                ca.sequence_family,
                ca.trigger_type,
                cr.brief_text,
                cr.brief_summary,
                cr.hook_angle,
                cr.enriched_at AS research_enriched_at
            FROM contacts c
            JOIN accounts a ON c.account_id = a.account_id
            JOIN campaign_assignments ca ON ca.contact_id = c.contact_id
            LEFT JOIN contact_research cr ON cr.contact_id = c.contact_id
            WHERE ca.eligibility_status IN ('pending', 'eligible')
              AND NOT EXISTS (
                SELECT 1 FROM personalization_payloads pp
                WHERE pp.contact_id = c.contact_id
                  AND pp.qa_status = 'approved'
              )
              AND c.email_status NOT IN ('invalid')
            ORDER BY a.priority_score DESC NULLS LAST
            LIMIT %s
        """, (limit,))
        return [dict(row) for row in cur.fetchall()]


def _write_payload(conn, contact_id: str, payload: dict):
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO personalization_payloads (
                contact_id, template_version, deterministic_tokens,
                ai_subject, ai_opener, ai_why_now, ai_proof_point, ai_cta,
                ai_t2_body, ai_t3_body,
                ai_t1_case_study_anchor, ai_t2_case_study_anchor,
                model_used, prompt_version, qa_status, qa_failure_reason,
                approved_at
            ) VALUES (
                %(contact_id)s, %(template_version)s, %(deterministic_tokens)s::jsonb,
                %(ai_subject)s, %(ai_opener)s, %(ai_why_now)s, %(ai_proof_point)s, %(ai_cta)s,
                %(ai_t2_body)s, %(ai_t3_body)s,
                %(ai_t1_case_study_anchor)s, %(ai_t2_case_study_anchor)s,
                %(model_used)s, %(prompt_version)s, %(qa_status)s, %(qa_failure_reason)s,
                %(approved_at)s
            )
        """, {
            "contact_id":              contact_id,
            "template_version":        payload.get("template_version"),
            "deterministic_tokens":    json.dumps(payload.get("deterministic_tokens") or {}),
            "ai_subject":              payload.get("ai_subject"),
            "ai_opener":               payload.get("ai_opener"),
            "ai_why_now":              payload.get("ai_why_now"),
            "ai_proof_point":          payload.get("ai_proof_point"),
            "ai_cta":                  payload.get("ai_cta"),
            "ai_t2_body":              payload.get("ai_t2_body"),
            "ai_t3_body":              payload.get("ai_t3_body"),
            "ai_t1_case_study_anchor": payload.get("ai_t1_case_study_anchor"),
            "ai_t2_case_study_anchor": payload.get("ai_t2_case_study_anchor"),
            "model_used":              payload.get("model_used"),
            "prompt_version":          payload.get("prompt_version"),
            "qa_status":               payload.get("qa_status"),
            "qa_failure_reason":       payload.get("qa_failure_reason"),
            "approved_at":             datetime.now(timezone.utc) if payload.get("qa_status") == "approved" else None,
        })


def run(dry_run: bool = False, batch_size: int = BATCH_SIZE):
    configure_logging("build_payloads")
    logger.info("Starting. dry_run=%s, batch_size=%s", dry_run, batch_size)

    approved = 0
    failed = 0
    no_research = 0

    with db_conn() as conn:
        contacts = _fetch_pending_contacts(conn, batch_size)

    # Sort by effective priority: stored score + recency boost for freshly-enriched contacts
    contacts.sort(
        key=lambda r: float(r.get("priority_score") or 0) + _recency_boost(r.get("research_enriched_at")),
        reverse=True,
    )

    logger.info("%s contacts fetched", len(contacts))

    for row in contacts:
        brief_text = row.get("brief_text")
        family = row["sequence_family"]

        if not brief_text and family not in RESEARCH_NOT_REQUIRED:
            no_research += 1
            logger.info(
                "[SKIP] %s %s @ %s — no research. Run enrich_research first.",
                row.get("first_name"),
                row.get("last_name"),
                row.get("company_name"),
            )
            continue

        contact = {
            "contact_id": str(row["contact_id"]),
            "first_name":  row["first_name"],
            "last_name":   row["last_name"],
            "title":       row["title"],
            "seniority":   row["seniority"],
            "email":       row["email"],
            "persona_type": row["persona_type"],
        }

        apollo_raw = row.get("apollo_raw") or {}
        account = {
            "account_id":    str(row["account_id"]),
            "company_name":  row["company_name"],
            "domain":        row["domain"],
            "industry":      row["industry"],
            "employee_count": row["employee_count"],
            "segment":       row["segment"],
            "priority_score": row.get("priority_score"),
            "signals":       row.get("signals") or {},
            "keywords":      apollo_raw.get("keywords") or [],
            "latest_funding_round_date": apollo_raw.get("latest_funding_round_date"),
            "tech_stack":    apollo_raw.get("current_technologies") or [],
        }

        if not family:
            logger.warning("[SKIP] %s has no sequence family", contact["contact_id"])
            continue

        trajectory = infer_trajectory(contact)
        predicted_objection = predict_objection(account, contact)

        payload = generate_payload(
            contact, account, family,
            brief_text=brief_text,
            trajectory=trajectory,
            predicted_objection=predicted_objection,
        )

        if dry_run:
            status = payload.get("qa_status")
            logger.info(
                "[DRY RUN] %s %s @ %s | %s | %s",
                contact.get("first_name"),
                contact.get("last_name"),
                account.get("company_name"),
                family,
                status,
            )
            if status == "approved":
                logger.info("subject: %s", payload.get("ai_subject"))
                logger.info("opener: %s", payload.get("ai_opener"))
                logger.info("t2_body: %s", payload.get("ai_t2_body"))
                if payload.get('ai_t3_body'):
                    logger.info("t3_body: %s", payload.get("ai_t3_body"))
            else:
                logger.info("reason: %s", payload.get("qa_failure_reason"))
            approved += (1 if status == "approved" else 0)
            failed   += (0 if status == "approved" else 1)
            continue

        with db_conn() as conn:
            _write_payload(conn, contact["contact_id"], payload)

        if payload.get("qa_status") == "approved":
            approved += 1
        else:
            failed += 1
            logger.warning(
                "[QA FAIL] %s: %s",
                contact["contact_id"],
                payload.get("qa_failure_reason"),
            )

    if no_research:
        logger.info(
            "%s contacts skipped — no research row. Run: python main.py enrich_research",
            no_research,
        )
    logger.info(
        "Done. approved=%s failed=%s skipped=%s",
        approved,
        failed,
        no_research,
    )
