"""
research/prospect_researcher.py

5-category pre-outreach research brief for personalization enrichment.

Inspired by the gtm-engineer-starter-kit prospect-researcher skill
(github.com/JJenglert1/gtm-engineer-starter-kit), adapted to use our
existing connector stack and return a brief structured for LLM injection.

Five categories:
  1. Company Fundamentals  — what they do, recent news, one-liner context
  2. People Intelligence   — who this person is, their public work + focus area
  3. Trigger Events        — funding, exec changes, job postings, product launches
  4. Tech Stack            — buyer stack relevant to Folloze's ICP
  5. Competitive Context   — what they might be evaluating or already using

Source priority:
  Perplexity → primary for all company + contact research (online search, structured)
  Brave      → fallback when Perplexity returns thin results (< 2 signals)
  X          → exec-level contacts only OR when a handle is known

Output: ProspectBrief dict, ready for generator._format_research_brief() injection.
"""
from __future__ import annotations

import re

from connectors.perplexity_client import PerplexityClient
from connectors.brave_client import BraveClient
from connectors.x_client import XClient

# Thresholds
MIN_COMPANY_SIGNALS = 2       # trigger Brave fallback if below
MIN_CONTACT_SIGNALS = 1       # trigger Brave contact fallback if below

# Only run X lookup for exec-level personas (API cost management)
EXEC_PERSONAS = frozenset({"cmo", "revmktg_vp", "abm_leader"})

# ICP-relevant tools for tech stack filtering
ICP_TOOLS = frozenset({
    "marketo", "hubspot", "salesforce", "eloqua", "pardot",
    "6sense", "demandbase", "pathfactory", "uberflip", "drift",
    "outreach", "salesloft", "gong",
})

# Competitive tools Folloze displaces or complements
COMPETITIVE_TOOLS = frozenset({
    "pathfactory", "uberflip", "turtl", "zoominfo intent",
    "folloze", "highspot", "seismic", "showpad",
})


def research(contact: dict, account: dict) -> dict:
    """
    Run 5-category research for one contact + account.

    Args:
        contact: dict with first_name, last_name, title, persona_type,
                 and optionally x_handle or twitter_handle
        account: dict with company_name, domain, industry, segment,
                 and optionally tech_stack (list of dicts with 'name' key)

    Returns a ProspectBrief dict:
    {
        "company_snapshot":  str,    # 1-2 sentences: what they do + key context
        "trigger_events":    [str],  # ordered: most actionable first
        "people_intel":      str,    # who this person is, their public focus
        "tech_context":      str,    # relevant stack (ICP tools detected)
        "competitive_notes": str,    # tools they're using that Folloze complements/displaces
        "recent_posts":      [str],  # X posts from the contact (if available)
        "hook_angle":        str,    # suggested opener direction for the LLM
        "sources_used":      [str],  # perplexity | brave | x (for debugging + cost tracking)
    }

    Returns empty brief on complete failure rather than raising.
    """
    brief: dict = {
        "company_snapshot": "",
        "trigger_events": [],
        "people_intel": "",
        "tech_context": "",
        "competitive_notes": "",
        "recent_posts": [],
        "hook_angle": "",
        "sources_used": [],
    }

    company = account.get("company_name") or ""
    domain = account.get("domain") or ""
    first = contact.get("first_name") or ""
    last = contact.get("last_name") or ""
    title = contact.get("title") or ""
    persona = contact.get("persona_type") or ""

    # -------------------------------------------------------------------------
    # 1. Company fundamentals + trigger events via Perplexity
    # -------------------------------------------------------------------------
    perp_company_signals = 0
    perp_contact_signals = 0

    try:
        perp = PerplexityClient()

        company_data = perp.enrich_company(company, domain)
        brief["company_snapshot"] = company_data.get("summary") or ""

        for sig in (company_data.get("signals") or []):
            text = (sig.get("text") or "").strip()
            sig_type = (sig.get("type") or "").lower()
            if text:
                brief["trigger_events"].append(_tag_signal(text, sig_type))
                perp_company_signals += 1

        # People intelligence
        contact_data = perp.research_contact(first, last, title, company)
        brief["people_intel"] = contact_data.get("summary") or ""

        for sig in (contact_data.get("signals") or []):
            text = (sig.get("text") or "").strip()
            sig_type = (sig.get("type") or "").lower()
            if text:
                # Contact signals are tagged so the generator prioritizes them for opener
                brief["trigger_events"].insert(0, _tag_signal(text, sig_type, contact_level=True))
                perp_contact_signals += 1

        brief["sources_used"].append("perplexity")

    except Exception as e:
        print(f"[researcher] Perplexity failed for {first} {last} @ {company}: {e}")

    # -------------------------------------------------------------------------
    # 2. Tech stack — from Apollo raw data (already in account dict)
    # -------------------------------------------------------------------------
    tech_stack = account.get("tech_stack") or []
    if tech_stack:
        icp_detected = []
        competitive_detected = []

        for t in tech_stack:
            name = (t.get("name") or "") if isinstance(t, dict) else str(t)
            name_lower = name.lower()
            if any(tool in name_lower for tool in ICP_TOOLS):
                icp_detected.append(name)
            if any(tool in name_lower for tool in COMPETITIVE_TOOLS):
                competitive_detected.append(name)

        if icp_detected:
            brief["tech_context"] = f"Stack: {', '.join(icp_detected[:6])}"
        if competitive_detected:
            brief["competitive_notes"] = f"Using: {', '.join(competitive_detected[:4])}"

    # -------------------------------------------------------------------------
    # 3. Brave fallback — when Perplexity returns thin company results
    # -------------------------------------------------------------------------
    if perp_company_signals < MIN_COMPANY_SIGNALS:
        try:
            brave = BraveClient()
            results = brave.research_company(company, domain)

            if results:
                brief["sources_used"].append("brave")

                # Use first result description as snapshot fallback
                if not brief["company_snapshot"] and results[0].get("description"):
                    brief["company_snapshot"] = results[0]["description"][:400]

                # Remaining results as web-sourced trigger signals
                for r in results[1:3]:
                    desc = (r.get("description") or r.get("title") or "").strip()
                    if desc:
                        brief["trigger_events"].append(f"[WEB] {desc[:200]}")

        except Exception as e:
            print(f"[researcher] Brave company fallback failed for {company}: {e}")

    # Contact research Brave fallback
    if perp_contact_signals < MIN_CONTACT_SIGNALS:
        try:
            brave = BraveClient()
            results = brave.research_contact(first, last, company)
            if results:
                if "brave" not in brief["sources_used"]:
                    brief["sources_used"].append("brave")
                for r in results[:2]:
                    desc = (r.get("description") or r.get("title") or "").strip()
                    if desc:
                        brief["trigger_events"].insert(0, f"[WEB-CONTACT] {desc[:200]}")

        except Exception as e:
            print(f"[researcher] Brave contact fallback failed for {first} {last}: {e}")

    # -------------------------------------------------------------------------
    # 4. X posts — exec personas only (cost management)
    # -------------------------------------------------------------------------
    x_handle = contact.get("x_handle") or contact.get("twitter_handle")
    run_x = persona in EXEC_PERSONAS or bool(x_handle)

    if run_x:
        try:
            xc = XClient()
            if x_handle:
                posts = xc.get_user_recent_posts(x_handle, max_results=3)
            else:
                posts = xc.search_contact_posts(first, last, company, max_results=3)

            if posts:
                brief["sources_used"].append("x")
                brief["recent_posts"] = [p["text"] for p in posts if p.get("text")]

        except Exception as e:
            print(f"[researcher] X lookup failed for {first} {last}: {e}")

    # -------------------------------------------------------------------------
    # 5. Derive hook angle from the strongest available signal
    # -------------------------------------------------------------------------
    brief["hook_angle"] = _derive_hook_angle(brief, contact, account)

    return brief


def _tag_signal(text: str, sig_type: str, contact_level: bool = False) -> str:
    """Tag a signal string with its type for parser-friendly injection."""
    if contact_level:
        return f"[PERSON:{sig_type.upper()}] {text}"
    return f"[{sig_type.upper()}] {text}"


def _derive_hook_angle(brief: dict, contact: dict, account: dict) -> str:
    """
    Suggest an opener direction based on the highest-value signal available.
    This is a hint to the LLM — not copy to use verbatim.

    Priority order:
    1. Contact-level signal (talk, article, quote, hire) — most personal
    2. X post — they said something recently
    3. Hiring for demand gen — directly validates the pain
    4. Funding / growth signal — timing play
    5. Exec change — new leader evaluates tools
    6. Persona default — generic but accurate
    """
    triggers = brief.get("trigger_events") or []
    posts = brief.get("recent_posts") or []

    # 1. Contact-level signal
    for t in triggers:
        if t.startswith("[PERSON:"):
            clean = re.sub(r"^\[PERSON:[A-Z]+\]\s*", "", t)
            return f"They have a public signal to reference: {clean[:200]}"

    # 2. X post
    if posts:
        return f"They posted recently: {posts[0][:150]}"

    # 3. Hiring signal
    for t in triggers:
        tl = t.lower()
        if any(w in tl for w in ["hiring", "job posting", "open role", "demand gen", "revenue marketing"]):
            return "Hiring into the exact pain Folloze solves — execution capacity gap."

    # 4. Funding
    for t in triggers:
        tl = t.lower()
        if any(w in tl for w in ["funding", "raised", "series", "investment", "round"]):
            return "Growth stage — execution capacity becomes the bottleneck right after a raise."

    # 5. Exec change
    for t in triggers:
        tl = t.lower()
        if any(w in tl for w in ["hired", "appointed", "joined", "new vp", "new head", "new cmo", "promoted"]):
            return "New leader in the seat. First 90 days = tool evaluation window."

    # 6. Persona default
    persona = contact.get("persona_type") or ""
    defaults = {
        "cmo":          "Pipeline predictability and always-on programs — CMO core pain.",
        "revmktg_vp":   "Execution bottleneck: more accounts, same team, same tools.",
        "abm_leader":   "ABX at scale requires orchestration, not more tools.",
        "revops":       "Attribution gap — campaign activity doesn't connect to pipeline.",
        "practitioner": "Volume of manual campaign work. Launch speed is the constraint.",
    }
    return defaults.get(persona, "Execution bottleneck in demand gen.")


# ---------------------------------------------------------------------------
# Formatting helper for generator.py injection
# ---------------------------------------------------------------------------

def format_brief_for_prompt(brief: dict) -> str:
    """
    Format a ProspectBrief into a compact string for injection into the LLM prompt.
    Returns empty string if brief has no useful content.
    """
    lines = []

    # Hook angle first — tells the LLM what angle to take
    hook = (brief.get("hook_angle") or "").strip()
    if hook:
        lines.append(f"SUGGESTED OPENER ANGLE: {hook}")
        lines.append("")

    # Contact-level signals (highest priority for opener)
    contact_triggers = [t for t in (brief.get("trigger_events") or []) if t.startswith("[PERSON:")]
    if contact_triggers:
        lines.append("CONTACT SIGNALS (highest value — use for opener if relevant):")
        for t in contact_triggers[:3]:
            clean = re.sub(r"^\[PERSON:[A-Z]+\]\s*", "", t)
            lines.append(f"  {clean}")
        lines.append("")

    # People intel
    people = (brief.get("people_intel") or "").strip()
    if people:
        lines.append(f"ABOUT THIS PERSON: {people}")
        lines.append("")

    # Company snapshot
    snapshot = (brief.get("company_snapshot") or "").strip()
    if snapshot:
        lines.append(f"COMPANY CONTEXT: {snapshot}")
        lines.append("")

    # Company-level triggers
    company_triggers = [
        t for t in (brief.get("trigger_events") or [])
        if not t.startswith("[PERSON:") and not t.startswith("[WEB-CONTACT]")
    ]
    if company_triggers:
        lines.append("COMPANY SIGNALS (use to sharpen why_now):")
        for t in company_triggers[:4]:
            clean = re.sub(r"^\[[A-Z]+\]\s*", "", t)
            lines.append(f"  {clean}")
        lines.append("")

    # Tech context
    tech = (brief.get("tech_context") or "").strip()
    if tech:
        lines.append(f"TECH STACK: {tech}")

    competitive = (brief.get("competitive_notes") or "").strip()
    if competitive:
        lines.append(f"COMPETITIVE: {competitive}")

    if tech or competitive:
        lines.append("")

    # X posts
    posts = brief.get("recent_posts") or []
    if posts:
        lines.append("RECENT X POSTS (optional personalization hook):")
        for p in posts[:2]:
            lines.append(f"  \"{p[:150]}\"")
        lines.append("")

    return "\n".join(lines).strip()
