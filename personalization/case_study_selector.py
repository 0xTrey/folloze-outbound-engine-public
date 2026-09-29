"""Select approved proof from a private runtime catalog.

The public engine contains scoring logic only. Customer names, outcomes, and
source details must be supplied through ``FOLLOZE_PROOF_CATALOG_PATH``.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional


PROOF_CATALOG_ENV = "FOLLOZE_PROOF_CATALOG_PATH"


def _load_catalog() -> list[dict]:
    configured_path = os.getenv(PROOF_CATALOG_ENV, "").strip()
    if not configured_path:
        return []

    path = Path(configured_path).expanduser()
    if not path.is_file():
        return []

    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise ValueError("private proof catalog must be a JSON list")
    return [item for item in payload if isinstance(item, dict)]


CATALOG: list[dict] = _load_catalog()


def _score(case_study: dict, account: dict, contact: dict) -> float:
    """Score one approved proof record for relevance."""
    score = 0.0

    industry = (account.get("industry") or "").lower()
    segment = (account.get("segment") or "").upper()
    tech_stack = account.get("tech_stack") or []
    tech_names = {(item.get("name") or "").lower() for item in tech_stack}

    if segment in case_study.get("segments", []):
        score += 2.0

    for keyword in case_study.get("industries", []):
        if str(keyword).lower() in industry:
            score += 3.0
            break

    for keyword in case_study.get("tech_keywords", []):
        normalized = str(keyword).lower()
        if any(normalized in name for name in tech_names):
            score += 2.0

    return score


def select_case_study(
    account: dict,
    contact: dict,
    min_score: float = 2.0,
) -> Optional[tuple[str, str]]:
    """Return the best approved proof as ``(name, outcome)`` when available."""
    best: Optional[tuple[str, str]] = None
    best_score = -1.0

    for item in CATALOG:
        name = str(item.get("name") or "").strip()
        outcome = str(item.get("outcome_headline") or "").strip()
        if not name or not outcome:
            continue

        score = _score(item, account, contact)
        if score > best_score:
            best_score = score
            best = (name, outcome)

    if best_score < min_score:
        return None
    return best
