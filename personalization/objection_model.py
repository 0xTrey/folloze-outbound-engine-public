"""
Objection prediction: classify the most likely objection a contact will raise
before we even send, so T2/T3 copy can acknowledge it implicitly.
"""
from __future__ import annotations

from datetime import datetime, timezone


FREEZE_KEYWORDS = {"layoffs", "restructuring", "hiring freeze", "cost cutting", "downsizing"}

SENIOR_TITLES = ("vp", "vice president", "svp", "evp", "director", "head of", "chief")
PRACTITIONER_TITLES = ("manager", "specialist", "coordinator", "analyst", "associate")


def predict_objection(account: dict, contact: dict) -> str:
    """
    Predict the most likely objection for this contact before outreach.

    Returns one of:
      'budget'         — SMB or freeze signals detected
      'timing'         — stale funding, no hiring signal, established company
      'priorities'     — senior title, possibly too busy / agenda-locked
      'wrong_persona'  — practitioner, not a decision-maker
      'none'           — strong timing + ENT segment, no obvious blocker
    """
    segment = (account.get("segment") or "").upper()
    title = (contact.get("title") or "").lower()
    seniority = (contact.get("seniority") or "").lower()

    # Pull signals text for freeze keyword detection
    signals = account.get("signals") or {}
    signals_text = " ".join(
        str(v) for v in signals.values() if isinstance(v, str)
    ).lower()
    keywords_text = " ".join(
        k.lower() for k in (account.get("keywords") or [])
    )
    combined = signals_text + " " + keywords_text

    # Budget objection: SMB or freeze language detected
    if segment == "SMB" or any(kw in combined for kw in FREEZE_KEYWORDS):
        return "budget"

    # Timing objection: funding is stale (> 18 months) and no recent hiring signal
    funding_date = account.get("latest_funding_round_date") or ""
    has_hiring_signal = any(
        kw in combined
        for kw in {"demand generation", "demand gen", "revenue marketing", "abm"}
    )
    if funding_date and not has_hiring_signal:
        try:
            funded = datetime.fromisoformat(funding_date.replace("Z", "+00:00"))
            if funded.tzinfo is None:
                funded = funded.replace(tzinfo=timezone.utc)
            months_ago = (datetime.now(timezone.utc) - funded).days / 30
            if months_ago > 18:
                return "timing"
        except (ValueError, TypeError):
            pass

    # Wrong persona: practitioner title, not a decision-maker
    if any(kw in title for kw in PRACTITIONER_TITLES) and not any(kw in title for kw in SENIOR_TITLES):
        return "wrong_persona"

    # Priorities: senior VP+ with no clear timing hook
    if seniority in ("vp", "c_suite") or any(kw in title for kw in ("svp", "evp", "chief")):
        if not has_hiring_signal:
            return "priorities"

    # No obvious objection — strong candidate
    return "none"
