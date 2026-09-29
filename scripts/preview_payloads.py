"""
Phase 4: Generate enhanced payload previews for 5 representative contacts
and post results to Mason Discord (#mason-folloze) for review.

Run: python scripts/preview_payloads.py [--contact-ids uuid1,uuid2,...]
     python scripts/preview_payloads.py --auto   (picks 5 representative contacts automatically)

Does NOT write to the DB. Preview only.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import textwrap
from pathlib import Path

# Ensure project root is on sys.path when run directly
sys.path.insert(0, str(Path(__file__).parent.parent))

import psycopg2.extras

from db.connection import db_conn
from personalization.generator import generate_payload
from personalization.objection_model import predict_objection
from scoring.account_scorer import _recency_boost
from scoring.contact_scorer import infer_trajectory

MASON_CHANNEL = os.getenv("FOLLOZE_ALERT_CHANNEL", "channel:example")

AUTO_QUERY = """
    SELECT DISTINCT ON (a.segment)
        c.contact_id::text,
        c.first_name,
        c.last_name,
        c.title,
        c.seniority,
        c.email,
        c.persona_type,
        c.created_at,
        a.account_id::text,
        a.company_name,
        a.domain,
        a.industry,
        a.employee_count,
        a.segment,
        a.priority_score,
        a.signals,
        a.apollo_raw,
        ca.sequence_family,
        cr.brief_text,
        cr.enriched_at AS research_enriched_at
    FROM contacts c
    JOIN accounts a ON c.account_id = a.account_id
    JOIN campaign_assignments ca ON ca.contact_id = c.contact_id
    LEFT JOIN contact_research cr ON cr.contact_id = c.contact_id
    JOIN personalization_payloads pp ON pp.contact_id = c.contact_id
    WHERE pp.qa_status = 'approved'
      AND c.email_status NOT IN ('invalid')
    ORDER BY a.segment, a.priority_score DESC NULLS LAST
    LIMIT 5
"""

CONTACT_ID_QUERY = """
    SELECT
        c.contact_id::text,
        c.first_name,
        c.last_name,
        c.title,
        c.seniority,
        c.email,
        c.persona_type,
        c.created_at,
        a.account_id::text,
        a.company_name,
        a.domain,
        a.industry,
        a.employee_count,
        a.segment,
        a.priority_score,
        a.signals,
        a.apollo_raw,
        ca.sequence_family,
        cr.brief_text,
        cr.enriched_at AS research_enriched_at
    FROM contacts c
    JOIN accounts a ON c.account_id = a.account_id
    JOIN campaign_assignments ca ON ca.contact_id = c.contact_id
    LEFT JOIN contact_research cr ON cr.contact_id = c.contact_id
    WHERE c.contact_id = ANY(%s::uuid[])
"""


def _fetch_contacts(contact_ids: list[str] | None) -> list[dict]:
    with db_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            if contact_ids:
                cur.execute(CONTACT_ID_QUERY, (contact_ids,))
            else:
                cur.execute(AUTO_QUERY)
            return [dict(r) for r in cur.fetchall()]


def _build_preview_block(row: dict, payload: dict, trajectory: str, objection: str, case_study_name: str) -> str:
    company  = row.get("company_name") or row.get("domain") or "?"
    name     = f"{row.get('first_name', '')} {row.get('last_name', '')}".strip()
    first    = row.get("first_name") or "there"
    title    = row.get("title") or "?"
    segment  = row.get("segment") or "?"
    family   = row.get("sequence_family") or "?"
    status   = payload.get("qa_status", "unknown")

    sep = "─" * 60
    # Header metadata (for reviewer context only — not sent)
    meta = [
        sep,
        f"TO: {name} — {title} @ {company} ({segment})",
        f"FAMILY: {family} | TRAJECTORY: {trajectory} | OBJECTION: {objection}",
        f"QA: {status}",
        sep,
    ]

    if status == "approved":
        opener    = payload.get("ai_opener", "")
        why_now   = payload.get("ai_why_now", "")
        proof     = payload.get("ai_proof_point", "")
        cta       = payload.get("ai_cta", "")
        t2        = payload.get("ai_t2_body", "")
        t3        = payload.get("ai_t3_body") or ""
        anchor    = payload.get("ai_t1_case_study_anchor") or ""

        # T1 — composed as a real email
        t1_body = f"{opener} {why_now} {proof} {cta}".strip()
        if anchor:
            t1_body += f"\n\n[{anchor}]"

        email_lines = [
            f"SUBJECT: {payload.get('ai_subject', '')}",
            "",
            t1_body,
            "",
            "-- Sender",
            "",
            "-- -- --",
            "",
            "T2 (threaded, day 3):",
            t2,
            "",
            "-- Sender",
        ]

        if t3:
            email_lines += [
                "",
                "T3 (threaded, day 7):",
                t3,
                "",
                "-- Sender",
            ]

        lines = meta + email_lines
    else:
        lines = meta + [f"FAIL: {payload.get('qa_failure_reason', 'unknown')}"]

    lines.append(sep)
    return "\n".join(lines)


def run(contact_ids: list[str] | None = None) -> None:
    print(f"[preview] Fetching {'selected' if contact_ids else '5 auto-selected'} contacts...")
    rows = _fetch_contacts(contact_ids)

    if not rows:
        print("[preview] No contacts found. Check that approved payloads exist.")
        return

    print(f"[preview] Generating enhanced payloads for {len(rows)} contacts (dry-run, no DB writes)...")

    blocks = []
    for row in rows:
        apollo_raw = row.get("apollo_raw") or {}
        contact = {
            "contact_id":  row["contact_id"],
            "first_name":  row.get("first_name"),
            "last_name":   row.get("last_name"),
            "title":       row.get("title"),
            "seniority":   row.get("seniority"),
            "email":       row.get("email"),
            "persona_type": row.get("persona_type"),
            "created_at":  row.get("created_at"),
        }
        account = {
            "account_id":   row["account_id"],
            "company_name": row.get("company_name"),
            "domain":       row.get("domain"),
            "industry":     row.get("industry"),
            "employee_count": row.get("employee_count"),
            "segment":      row.get("segment"),
            "priority_score": row.get("priority_score"),
            "signals":      row.get("signals") or {},
            "keywords":     apollo_raw.get("keywords") or [],
            "latest_funding_round_date": apollo_raw.get("latest_funding_round_date"),
            "tech_stack":   apollo_raw.get("current_technologies") or [],
        }

        trajectory = infer_trajectory(contact)
        predicted_objection = predict_objection(account, contact)
        family = row.get("sequence_family") or ""

        payload = generate_payload(
            contact, account, family,
            brief_text=row.get("brief_text"),
            trajectory=trajectory,
            predicted_objection=predicted_objection,
        )

        tokens = payload.get("deterministic_tokens") or {}
        case_study_name = tokens.get("case_study_name") or ""

        block = _build_preview_block(row, payload, trajectory, predicted_objection, case_study_name)
        blocks.append(block)
        print(block)

    header = (
        f"Folloze Outbound Engine — Enhanced Payload Preview\n"
        f"{len(rows)} contacts | Phases 3A–3E active\n"
        f"Review before running assign_push.\n\n"
    )
    full_message = header + "\n\n".join(blocks)

    print("\n[preview] Posting to Mason Discord...")
    try:
        subprocess.run(
            [
                "openclaw", "message", "send",
                "--channel", "discord",
                "--target", MASON_CHANNEL,
                "--message", full_message,
            ],
            check=True,
        )
        print("[preview] Posted to Discord.")
    except Exception as e:
        print(f"[preview] Discord post failed: {e}")
        print("[preview] Full preview text printed above — copy manually if needed.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Generate and preview enhanced payloads")
    parser.add_argument(
        "--contact-ids",
        type=str,
        default=None,
        help="Comma-separated contact UUIDs (auto-selects 5 if omitted)",
    )
    args = parser.parse_args()

    ids = [i.strip() for i in args.contact_ids.split(",")] if args.contact_ids else None
    run(contact_ids=ids)
