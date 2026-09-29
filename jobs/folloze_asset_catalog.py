"""
Shared helpers for loading and resolving Folloze board content assets.
"""
from __future__ import annotations

import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

DEFAULT_OVERRIDES_PATH = Path(__file__).resolve().parent.parent / "data" / "folloze_asset_overrides.json"

_DASH_TRANSLATION = str.maketrans({
    "—": "-",
    "–": "-",
    "−": "-",
    "‑": "-",
    "‒": "-",
})


def normalize_asset_title(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").strip())
    if not text:
        return ""
    text = text.translate(_DASH_TRANSLATION)
    text = text.replace("&", " and ")
    text = re.sub(r"[^0-9a-zA-Z]+", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def parse_tag_list(value: str | None) -> list[str]:
    return [part.strip() for part in str(value or "").split(",") if part.strip()]


def dedupe_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def load_asset_overrides(path: str | None = None) -> dict[str, dict[str, Any]]:
    override_path = Path(path).expanduser() if path else DEFAULT_OVERRIDES_PATH
    if not override_path.exists():
        return {}
    with override_path.open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return {
        str(asset_id): dict(payload or {})
        for asset_id, payload in dict(data).items()
    }


def asset_title_candidates(asset: dict[str, Any]) -> list[str]:
    candidates = [asset.get("title"), *(asset.get("title_aliases") or [])]
    return dedupe_preserve_order([str(value) for value in candidates if value])


def _filtered_assets(assets: dict[str, dict[str, Any]], board_id: str | None) -> list[dict[str, Any]]:
    if not board_id:
        return list(assets.values())
    filtered = [
        asset
        for asset in assets.values()
        if not asset.get("board_id") or str(asset.get("board_id")) == str(board_id)
    ]
    return filtered or list(assets.values())


def resolve_asset_by_title(
    title: str | None,
    assets: dict[str, dict[str, Any]],
    *,
    board_id: str | None = None,
    min_ratio: float = 0.9,
) -> dict[str, Any] | None:
    if not title or not assets:
        return None

    raw_target = str(title).strip()
    normalized_target = normalize_asset_title(raw_target)
    if not normalized_target:
        return None

    candidates = _filtered_assets(assets, board_id)

    # Prefer exact string matches first so duplicate normalized titles still
    # resolve to the punctuation-preserving asset we saw in the report.
    exact_matches: list[dict[str, Any]] = []
    for asset in candidates:
        for candidate in asset_title_candidates(asset):
            if candidate.strip().lower() == raw_target.lower():
                exact_matches.append(asset)
                break
    if len(exact_matches) == 1:
        return {"asset": exact_matches[0], "match_type": "exact", "confidence": 1.0}

    normalized_matches: list[dict[str, Any]] = []
    for asset in candidates:
        for candidate in asset_title_candidates(asset):
            if normalize_asset_title(candidate) == normalized_target:
                normalized_matches.append(asset)
                break
    if len(normalized_matches) == 1:
        return {"asset": normalized_matches[0], "match_type": "normalized", "confidence": 0.98}

    best_asset = None
    best_ratio = 0.0
    for asset in candidates:
        for candidate in asset_title_candidates(asset):
            ratio = SequenceMatcher(
                None,
                normalized_target,
                normalize_asset_title(candidate),
            ).ratio()
            if ratio > best_ratio:
                best_ratio = ratio
                best_asset = asset

    if best_asset and best_ratio >= min_ratio:
        return {"asset": best_asset, "match_type": "fuzzy", "confidence": round(best_ratio, 4)}
    return None
