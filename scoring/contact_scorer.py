"""
Contact-level scoring: trajectory inference from Apollo employment history.
"""
from __future__ import annotations

from datetime import datetime, timezone


SENIOR_TITLE_KEYWORDS = ("vp", "vice president", "director", "head of", "chief")


def infer_trajectory(contact: dict) -> str:
    """
    Infer contact career trajectory from employment history.

    Returns one of:
      'newly_hired'       — started current role within 6 months
      'recently_promoted' — senior title (VP/Director/Head of) with tenure < 12 months
      'established'       — tenure > 24 months
      'unknown'           — no usable tenure data
    """
    title = (contact.get("title") or "").lower()
    is_senior = any(kw in title for kw in SENIOR_TITLE_KEYWORDS)

    # Apollo employment_history is nested inside apollo_raw on the contact record.
    # If not available, fall back to contact created_at as a rough proxy.
    apollo_raw = contact.get("apollo_raw") or {}
    employment_history = apollo_raw.get("employment_history") or []

    start_date_str = None
    if employment_history:
        start_date_str = (employment_history[0] or {}).get("start_date")

    if not start_date_str:
        # Fallback: use contact created_at (when they were ingested)
        # This correlates loosely with when we first found them — not the same as tenure.
        start_date_str = contact.get("created_at")

    if not start_date_str:
        return "unknown"

    try:
        date_str = str(start_date_str)
        # Apollo short date format "2024-03" — pad to full ISO
        if len(date_str) == 7 and "-" in date_str:
            date_str += "-01"
        started = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        if started.tzinfo is None:
            started = started.replace(tzinfo=timezone.utc)
        tenure_months = (datetime.now(timezone.utc) - started).days / 30
    except (ValueError, TypeError):
        return "unknown"

    if tenure_months <= 6:
        return "newly_hired"
    if is_senior and tenure_months <= 12:
        return "recently_promoted"
    if tenure_months > 24:
        return "established"
    return "unknown"
