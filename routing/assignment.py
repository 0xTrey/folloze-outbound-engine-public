from __future__ import annotations

# ---------------------------------------------------------------------------
# Legacy routing label selection
# ---------------------------------------------------------------------------
# SIMPLE and VISIONARY are retained as database-compatible identifiers. Their
# recipient-visible meaning belongs in the private messaging context.
# ---------------------------------------------------------------------------

MOMENTUM_SIGNALS = {"funding", "leadership", "award"}  # signal types that indicate growth momentum


def select_narrative_pillar(
    persona_type: str,
    touch_number: int = 1,
    account_signals: list[dict] | None = None,
) -> str:
    """
    Select the narrative pillar for this contact.

    Args:
        persona_type: from TITLE_TO_PERSONA mapping
        touch_number: 1 = first touch, 2+ = follow-up
        account_signals: list of signal dicts with "type" key (from Perplexity/Brave enrichment)

    Returns:
        "SIMPLE" or "VISIONARY"
    """
    if persona_type != "cmo":
        return "SIMPLE"

    # CMO on first touch: SIMPLE (Visionary hint goes in why_now field only, handled by prompt)
    if touch_number <= 1:
        return "SIMPLE"

    # CMO on second touch or later: VISIONARY
    if touch_number >= 2:
        return "VISIONARY"

    # CMO + momentum signal (funding, new leadership, award) on any touch: VISIONARY
    if account_signals:
        signal_types = {s.get("type", "").lower() for s in account_signals}
        if signal_types & MOMENTUM_SIGNALS:
            return "VISIONARY"

    return "SIMPLE"


# Maps persona + segment → sequence family name.
# Order matters: first match wins.
ROUTING_RULES = [
    # ENT
    {"segment": "ENT", "persona": "revmktg_vp", "trigger": "intent",  "family": "ENT_REVMKTG_INTENT"},
    {"segment": "ENT", "persona": "revmktg_vp", "trigger": "hiring",  "family": "ENT_REVMKTG_HIRING"},
    {"segment": "ENT", "persona": "abm_leader", "trigger": "intent",  "family": "ENT_ABM_INTENT"},
    # MM
    {"segment": "MM",  "persona": "revmktg_vp", "trigger": None,      "family": "MM_REVMKTG_TIMING"},
    {"segment": "MM",  "persona": "revops",     "trigger": "intent",  "family": "MM_REVOPS_INTENT"},
    {"segment": "MM",  "persona": "cmo",        "trigger": None,      "family": "MM_CMO_EXEC"},
    # GTME — GTM engineer persona, all segments route here
    {"segment": "GTME", "persona": "gtm_engineer", "trigger": None,   "family": "GTME_VIBE_MKT"},
]

# Fallback family used when no specific rule matches.
# Catches borderline fits, unrecognized personas, and edge-case segments.
# ENT contacts get the ENT hiring sequence; everything else gets MM timing.
FALLBACK_FAMILY = {
    "ENT": "ENT_REVMKTG_HIRING",
    "default": "MM_REVMKTG_TIMING",
}

# Persona inference from title keywords (order matters: first match wins)
TITLE_TO_PERSONA = [
    (["gtm engineer", "gtm engineering", "go-to-market engineer"], "gtm_engineer"),
    (["abm", "account-based marketing", "account based marketing"], "abm_leader"),
    (["revenue marketing", "demand generation", "demand gen"], "revmktg_vp"),
    (["revenue operations", "revops", "marketing operations", "mops"], "revops"),
    (["chief marketing officer", "cmo"], "cmo"),
    (["marketing"], "practitioner"),
]


def infer_persona(title: str) -> str:
    """Map a contact's title to a persona_type."""
    title_lower = title.lower()
    for keywords, persona in TITLE_TO_PERSONA:
        if any(kw in title_lower for kw in keywords):
            return persona
    return "practitioner"


def assign_sequence_family(
    segment: str,
    persona_type: str,
    trigger_type: str | None = None,
    use_fallback: bool = True,
) -> str | None:
    """
    Return the best-matching sequence family name for a contact.

    If no specific rule matches and use_fallback=True (default), returns the
    fallback family for the segment instead of None. This ensures borderline
    fits, unrecognized personas, and edge-case segments still get queued.

    Set use_fallback=False only when you need to audit unmatched contacts.
    """
    for rule in ROUTING_RULES:
        if rule["segment"] != segment:
            continue
        if rule["persona"] != persona_type:
            continue
        if rule["trigger"] is not None and rule["trigger"] != trigger_type:
            continue
        return rule["family"]

    if use_fallback:
        return FALLBACK_FAMILY.get(segment, FALLBACK_FAMILY["default"])
    return None


def detect_trigger(account_data: dict) -> str | None:
    """
    Derive a trigger_type from account signals.
    Returns 'intent', 'hiring', or None.
    """
    keywords = " ".join([k.lower() for k in (account_data.get("keywords") or [])])
    description = (account_data.get("short_description") or "").lower()
    combined = keywords + " " + description

    hiring_terms = ["demand generation", "demand gen", "revenue marketing", "abm manager", "abm director"]
    if any(t in combined for t in hiring_terms):
        return "hiring"

    # Intent is inferred externally (6sense/Demandbase) — not available from Apollo
    # Default to None; intent signal can be injected later via suppression or enrichment job
    return None
