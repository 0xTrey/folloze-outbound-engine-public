from __future__ import annotations

import json
import re
from typing import Optional

from llm_gateway import LLMGateway

from personalization.prompts import (
    FAMILY_PROMPTS,
    HAS_PRIVATE_MESSAGING_CONTEXT,
    PRIVATE_CONTEXT_ENV,
    PROMPT_VERSION,
)
from personalization.case_study_selector import select_case_study
from research.prospect_researcher import format_brief_for_prompt

# Per-field character limits. QA rejects anything over. All required (non-empty).
FIELD_LIMITS = {
    "subject":     60,
    "opener":      200,
    "why_now":     200,
    "proof_point": 200,
    "cta":         100,
    "t2_body":     200,
}

# Optional fields — empty string is valid. QA validates length only when non-empty.
OPTIONAL_FIELD_LIMITS = {
    "t1_case_study_anchor": 80,
    "t2_case_study_anchor": 80,
    "t3_body":              150,  # present only in 3-touch families (e.g. GTME_VIBE_MKT)
}

# Sequence families that produce a T3 touch
THREE_TOUCH_FAMILIES = {"GTME_VIBE_MKT"}

# Model profile routing per family. Falls back through the list in order.
# GTME uses Gemini — better creative writing for a technically sophisticated audience.
# All other families use local → workhorse (cost-conservative default).
FAMILY_MODEL_PROFILES: dict[str, tuple[str, ...]] = {
    "GTME_VIBE_MKT": ("strategic", "workhorse"),
}
DEFAULT_MODEL_PROFILES = ("local", "workhorse")

AI_FIELDS = list(FIELD_LIMITS.keys())

# Placeholder patterns that should not appear in output
PLACEHOLDER_RE = re.compile(r"\[.*?\]|\{[a-z_]+\}")

def _build_deterministic_tokens(contact: dict, account: dict) -> dict:
    tech = account.get("tech_stack") or []
    tech_names = [t.get("name", "") for t in tech if t.get("name")][:5]
    tech_summary = ", ".join(tech_names) if tech_names else "not detected"

    return {
        "first_name":        contact.get("first_name") or "there",
        "last_name":         contact.get("last_name") or "",
        "title":             contact.get("title") or "",
        "company":           account.get("company_name") or account.get("name") or "",
        "industry":          account.get("industry") or "",
        "tech_stack_summary": tech_summary,
    }


def _format_signals_block(
    account_signals: dict | None,
    contact_signals: dict | None,
) -> str:
    """
    Format Perplexity account + contact signals into a compact block for LLM injection.
    Contact signals are injected first — they are the primary personalization hook.
    Returns empty string if no signals available.
    """
    lines = []

    # Contact-level signals (highest value — specific to this person)
    if contact_signals:
        summary = (contact_signals.get("summary") or "").strip()
        signals = contact_signals.get("signals") or []
        if summary or signals:
            lines.append("CONTACT SIGNALS (use these for the opener — specific to this person):")
            if summary:
                lines.append(f"  About: {summary}")
            for s in signals[:3]:
                stype = s.get("type", "signal").upper()
                text = s.get("text", "").strip()
                if text:
                    lines.append(f"  [{stype}] {text}")

    # Account-level signals (context for why_now)
    if account_signals:
        summary = (account_signals.get("summary") or "").strip()
        signals = account_signals.get("signals") or []
        if summary or signals:
            if lines:
                lines.append("")
            lines.append("COMPANY SIGNALS (use these to sharpen why_now):")
            if summary:
                lines.append(f"  Context: {summary}")
            for s in signals[:3]:
                stype = s.get("type", "signal").upper()
                text = s.get("text", "").strip()
                if text:
                    lines.append(f"  [{stype}] {text}")

    return "\n".join(lines)


def _format_enhancement_block(
    tokens: dict,
    trajectory: str,
    predicted_objection: str,
    case_study: tuple[str, str] | None,
) -> str:
    """
    Build an additional context block injected after brief_text.
    Contains trajectory, objection, and case study guidance for the LLM.
    """
    lines = []

    # Trajectory label. Private context supplies recipient-visible guidance.
    if trajectory and trajectory != "unknown":
        lines.append(
            f"CONTACT TRAJECTORY: {trajectory}. Apply only the approved private rules."
        )

    # Objection label. Private context supplies the response strategy.
    if predicted_objection and predicted_objection != "none":
        lines.append(
            f"PREDICTED OBJECTION: {predicted_objection}. Apply only the approved private rules."
        )

    # Case study (Phase 3A)
    if case_study:
        cs_name, cs_outcome = case_study
        lines.append(
            f"RECOMMENDED CASE STUDY: {cs_name} — {cs_outcome}. "
            f"Reference this in t1_case_study_anchor if the match is genuine."
        )

    return "\n".join(lines)


def _call_llm(system: str, user: str, sequence_family: str = "") -> Optional[dict]:
    """
    Call LLM with the appropriate profile chain for this sequence family.
    GTME_VIBE_MKT routes to strategic -> workhorse on failure.
    All other families route to local -> workhorse.
    """
    profiles = FAMILY_MODEL_PROFILES.get(sequence_family, DEFAULT_MODEL_PROFILES)
    for profile in profiles:
        try:
            gw = LLMGateway(profile=profile)
            response = gw.chat([
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ], timeout=45)
            text = response.strip()
            # Strip markdown code fences if present
            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\n?", "", text)
                text = re.sub(r"\n?```$", "", text)
            return json.loads(text)
        except Exception as e:
            print(f"[generator] LLM call failed on profile={profile}: {e}")
    return None


def qa_check(payload: dict, tokens: dict, sequence_family: str = "") -> tuple[bool, str]:
    """
    Validate all 6 AI fields against per-field char limits.
    All 6 required fields (subject, opener, why_now, proof_point, cta, t2_body) must be present.
    For THREE_TOUCH_FAMILIES, t3_body is also required.
    Returns (passed: bool, reason: str).
    """
    for field, limit in FIELD_LIMITS.items():
        value = payload.get(field) or ""

        if not value.strip():
            return False, f"field '{field}' is empty"

        if len(value) > limit:
            return False, f"field '{field}' exceeds {limit} chars ({len(value)})"

        if PLACEHOLDER_RE.search(value):
            return False, f"field '{field}' contains unreplaced placeholder"

    # t3_body is required for 3-touch families (e.g. GTME_VIBE_MKT)
    if sequence_family in THREE_TOUCH_FAMILIES:
        t3 = payload.get("t3_body") or ""
        if not t3.strip():
            return False, "t3_body is required for 3-touch family but is empty"
        if len(t3) > OPTIONAL_FIELD_LIMITS["t3_body"]:
            return False, f"t3_body exceeds {OPTIONAL_FIELD_LIMITS['t3_body']} chars ({len(t3)})"

    # Optional case study anchor fields — validate only when non-empty
    for field, limit in OPTIONAL_FIELD_LIMITS.items():
        if field == "t3_body":
            continue  # handled above
        value = payload.get(field) or ""
        if value.strip() and len(value) > limit:
            return False, f"optional field '{field}' exceeds {limit} chars ({len(value)})"

    # opener should not be a generic compliment
    opener = payload.get("opener", "").lower()
    flattery = ["great work", "impressive", "love what you're doing", "huge fan", "congrats on"]
    if any(phrase in opener for phrase in flattery):
        return False, "opener appears to be a generic compliment"

    return True, ""


def generate_payload(
    contact: dict,
    account: dict,
    sequence_family: str,
    model_hint: str = "local",
    signals: dict | None = None,
    contact_signals: dict | None = None,
    research_brief: dict | None = None,
    brief_text: str | None = None,
    predicted_objection: str = "none",
    trajectory: str = "unknown",
) -> dict:
    """
    Generate a personalization payload for one contact.
    Returns a dict ready for insertion into personalization_payloads.

    Context priority (highest to lowest):
        brief_text     — pre-formatted string from contact_research table. Used directly.
        research_brief — ProspectBrief dict from prospect_researcher (live call path).
        signals        — legacy Perplexity account-level signals from enrich_signals.
        contact_signals — legacy Perplexity contact-level signals from enrich_signals.
    """
    template = FAMILY_PROMPTS.get(sequence_family)
    if not template:
        return {
            "qa_status": "failed",
            "qa_failure_reason": f"no prompt template for family '{sequence_family}'",
            "prompt_version": PROMPT_VERSION,
        }

    if not HAS_PRIVATE_MESSAGING_CONTEXT:
        return {
            "qa_status": "failed",
            "qa_failure_reason": (
                f"private messaging context is not configured; set {PRIVATE_CONTEXT_ENV}"
            ),
            "prompt_version": PROMPT_VERSION,
            "template_version": sequence_family,
        }

    tokens = _build_deterministic_tokens(contact, account)

    # Add trajectory and objection to deterministic tokens for auditability
    tokens["trajectory"] = trajectory
    tokens["predicted_objection"] = predicted_objection

    user_prompt = template["user_template"].format(**{
        k: v for k, v in tokens.items()
        if isinstance(v, str)  # only string tokens go into format()
    })

    # brief_text from DB takes priority — already formatted, no extra processing needed
    if brief_text:
        context_block = brief_text
    elif research_brief:
        context_block = format_brief_for_prompt(research_brief)
    else:
        context_block = _format_signals_block(
            account_signals=signals,
            contact_signals=contact_signals,
        )

    if context_block:
        user_prompt = user_prompt + "\n\n" + context_block

    # Case study selection (Phase 3A)
    case_study = select_case_study(account, contact)
    if case_study:
        tokens["case_study_name"] = case_study[0]
        tokens["case_study_outcome"] = case_study[1]

    # Enhancement block: approved private handling for trajectory, objections, and proof.
    enhancement = _format_enhancement_block(tokens, trajectory, predicted_objection, case_study)
    if enhancement:
        user_prompt = user_prompt + "\n\n" + enhancement

    system_prompt = template["system"]

    raw = _call_llm(system_prompt, user_prompt, sequence_family=sequence_family)

    if raw is None:
        return {
            "deterministic_tokens": tokens,
            "qa_status": "failed",
            "qa_failure_reason": "LLM call failed on all profiles",
            "prompt_version": PROMPT_VERSION,
            "template_version": sequence_family,
        }

    passed, reason = qa_check(raw, tokens, sequence_family=sequence_family)

    result = {
        "template_version": sequence_family,
        "deterministic_tokens": tokens,
        # T1 fields
        "ai_subject":      raw.get("subject"),
        "ai_opener":       raw.get("opener"),
        "ai_why_now":      raw.get("why_now"),
        "ai_proof_point":  raw.get("proof_point"),
        "ai_cta":          raw.get("cta"),
        # T2 — threaded follow-up / close
        "ai_t2_body":      raw.get("t2_body"),
        # T3 — present only for 3-touch families; None for 2-touch families
        "ai_t3_body":      raw.get("t3_body") or None,
        # Optional case study anchor text (empty string if LLM chose not to include)
        "ai_t1_case_study_anchor": raw.get("t1_case_study_anchor") or "",
        "ai_t2_case_study_anchor": raw.get("t2_case_study_anchor") or "",
        "model_used": model_hint,
        "prompt_version": PROMPT_VERSION,
        "qa_status": "approved" if passed else "failed",
        "qa_failure_reason": reason if not passed else None,
    }

    return result
