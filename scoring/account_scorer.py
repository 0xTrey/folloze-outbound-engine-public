from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

# Tech stack keywords that indicate ICP fit
ICP_TECH = {"marketo", "hubspot", "salesforce", "eloqua", "6sense", "demandbase"}

# Keywords suggesting negative timing (budget freeze, layoffs, etc.)
FREEZE_KEYWORDS = {"layoffs", "restructuring", "hiring freeze", "cost cutting", "downsizing"}

# Keywords suggesting positive timing signals
HIRING_KEYWORDS = {"demand generation", "demand gen", "revenue marketing", "abm", "account-based"}
FUNDING_TYPES_POSITIVE = {"series_a", "series_b", "series_c", "series_d", "growth"}
NEW_LEADER_KEYWORDS = {"new cmo", "new vp marketing", "new chief marketing", "joins as vp"}


def _count_tech_signals(tech_stack: list[dict]) -> int:
    """Count how many ICP tech tools are present in the tech stack."""
    names = {t.get("name", "").lower() for t in tech_stack}
    return sum(1 for tool in ICP_TECH if any(tool in name for name in names))


def compute_fit_score(account_data: dict) -> float:
    """
    Fit score 0–10 based on industry, size, and tech stack.
      Industry match (B2B SaaS/MarTech): +3
      Employee count 200–2000: +3 / 100–5000: +2
      ICP tech tools: +2 each, max +4
    """
    score = 0.0
    industry = (account_data.get("industry") or "").lower()
    employees = account_data.get("estimated_employees") or account_data.get("employee_count") or 0
    tech_stack = account_data.get("tech_stack") or []
    keywords = [k.lower() for k in (account_data.get("keywords") or [])]

    # Industry match
    if any(term in industry for term in ["saas", "software", "marketing technology", "martech", "enterprise"]):
        score += 3

    # Employee count
    if 200 <= employees <= 2000:
        score += 3
    elif 100 <= employees <= 5000:
        score += 2

    # Tech stack signals (capped at 4)
    tech_hits = _count_tech_signals(tech_stack)
    score += min(tech_hits * 2, 4)

    return round(min(score, 10.0), 2)


def compute_timing_score(account_data: dict) -> float:
    """
    Timing score 0–10 based on hiring, funding, and leadership signals.
      Hiring for demand gen roles: +3
      Recent funding (< 12 months): +3
      New CMO/VPM signal: +2
      Freeze keywords present: -2
    """
    score = 0.0
    keywords = " ".join([k.lower() for k in (account_data.get("keywords") or [])])
    description = (account_data.get("short_description") or "").lower()
    funding_date = account_data.get("latest_funding_round_date") or ""
    funding_type = (account_data.get("latest_funding_round_type") or "").lower().replace(" ", "_")

    combined_text = keywords + " " + description

    # Hiring signal
    if any(kw in combined_text for kw in HIRING_KEYWORDS):
        score += 3

    # Recent funding
    if funding_type in FUNDING_TYPES_POSITIVE and funding_date:
        from datetime import datetime, timezone
        try:
            funded = datetime.fromisoformat(funding_date.replace("Z", "+00:00"))
            months_ago = (datetime.now(timezone.utc) - funded).days / 30
            if months_ago <= 12:
                score += 3
        except ValueError:
            pass

    # New leadership signal
    if any(kw in combined_text for kw in NEW_LEADER_KEYWORDS):
        score += 2

    # Negative signals
    if any(kw in combined_text for kw in FREEZE_KEYWORDS):
        score -= 2

    return round(max(min(score, 10.0), 0.0), 2)


def compute_priority_score(fit: float, timing: float) -> float:
    """Priority = (fit * 0.6) + (timing * 0.4), rounded to 2 decimal places."""
    return round((fit * 0.6) + (timing * 0.4), 2)


def assign_segment(employee_count: Optional[int]) -> str:
    """ENT > 1000, MM 100–1000, SMB < 100."""
    if employee_count is None:
        return "MM"  # default if unknown
    if employee_count > 1000:
        return "ENT"
    if employee_count >= 100:
        return "MM"
    return "SMB"


def _recency_boost(last_enriched_at) -> float:
    """
    Additive boost to timing_score based on how recently research was enriched.
      Within 7 days:  +2.0
      Within 30 days: +1.0
      Older or None:   0.0
    """
    if not last_enriched_at:
        return 0.0
    try:
        if isinstance(last_enriched_at, datetime):
            enriched = last_enriched_at
        else:
            enriched = datetime.fromisoformat(str(last_enriched_at).replace("Z", "+00:00"))
        if enriched.tzinfo is None:
            enriched = enriched.replace(tzinfo=timezone.utc)
        days_ago = (datetime.now(timezone.utc) - enriched).days
        if days_ago <= 7:
            return 2.0
        elif days_ago <= 30:
            return 1.0
    except (ValueError, TypeError):
        pass
    return 0.0


def score_account(apollo_data: dict, last_enriched_at=None) -> dict:
    """
    Given raw Apollo org data, return a dict with all scoring fields.
    Suitable for direct upsert into the accounts table.
    """
    employees = apollo_data.get("estimated_employees") or apollo_data.get("employee_count")
    fit = compute_fit_score(apollo_data)
    timing = min(compute_timing_score(apollo_data) + _recency_boost(last_enriched_at), 10.0)
    timing = round(timing, 2)
    priority = compute_priority_score(fit, timing)
    segment = assign_segment(employees)

    geography = None
    city = apollo_data.get("city")
    country = apollo_data.get("country")
    if city and country:
        geography = f"{city}, {country}"
    elif country:
        geography = country

    return {
        "company_name": apollo_data.get("name"),
        "industry": apollo_data.get("industry"),
        "employee_count": employees,
        "geography": geography,
        "fit_score": fit,
        "timing_score": timing,
        "priority_score": priority,
        "segment": segment,
    }
