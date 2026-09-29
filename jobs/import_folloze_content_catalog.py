"""
Job: import_folloze_content_catalog

Load the Folloze Content Engagement report into asset_catalog so signal
derivation can resolve board item IDs, Content Center IDs, titles, themes,
and manual summaries from a single source of truth.

Usage:
    python main.py import_folloze_content_catalog
    python main.py import_folloze_content_catalog --path /path/to/content-report.csv
    python main.py import_folloze_content_catalog --dry-run --limit 10
"""
from __future__ import annotations

import csv
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from db.connection import db_conn
from jobs.folloze_asset_catalog import dedupe_preserve_order, load_asset_overrides, parse_tag_list
from runtime_config import configure_logging

SOURCE_NAME = "folloze_content_report"
REPORT_GLOB = "*Content Engagement*.csv"

logger = logging.getLogger(__name__)


def _discover_latest_report() -> Path:
    downloads = Path.home() / "Downloads"
    candidates = sorted(downloads.glob(REPORT_GLOB), key=lambda path: path.stat().st_mtime, reverse=True)
    if not candidates:
        raise FileNotFoundError(f"No CSV matching {REPORT_GLOB!r} found in {downloads}")
    return candidates[0]


def _parse_report_updated_at(header_line: str) -> datetime:
    match = re.search(r"Updated At:\s*([0-9:\-\s]+)", header_line or "")
    if not match:
        raise ValueError(f"Could not parse report timestamp from header: {header_line!r}")
    return datetime.strptime(match.group(1).strip(), "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)


def _load_rows(path: Path) -> tuple[datetime, list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            first_row = next(reader)
        except StopIteration as exc:
            raise ValueError(f"CSV is empty: {path}") from exc
        report_updated_at = _parse_report_updated_at(first_row[0] if first_row else "")
        next(reader, None)
        header = next(reader, None)
        if not header:
            raise ValueError(f"CSV is missing a header row: {path}")

        dict_reader = csv.DictReader(handle, fieldnames=header)
        rows = [dict(row) for row in dict_reader if any((cell or "").strip() for cell in row.values())]
        return report_updated_at, rows


def _fallback_themes(row: dict[str, str]) -> list[str]:
    themes: list[str] = []
    category = (row.get("Category 1") or "").strip().lower().replace(" ", "_")
    if category:
        themes.append(category)
    for tag in parse_tag_list(row.get("Item Tags"))[:4]:
        normalized = re.sub(r"[^a-z0-9]+", "_", tag.lower()).strip("_")
        if normalized:
            themes.append(normalized)
    return dedupe_preserve_order(themes)


def _build_asset_record(row: dict[str, str], overrides: dict[str, dict]) -> dict:
    asset_id = (row.get("Board Item Id") or "").strip()
    if not asset_id:
        raise ValueError(f"Missing Board Item Id in row: {row}")

    override = overrides.get(asset_id, {})
    item_source_value = (row.get("Item Source Value") or "").strip() or None
    source_url = item_source_value if item_source_value and item_source_value.startswith(("http://", "https://")) else None
    title_aliases = []
    if item_source_value and not source_url:
        title_aliases.append(item_source_value)
    title_aliases.extend(override.get("title_aliases") or [])

    return {
        "asset_id": asset_id,
        "content_center_item_id": (row.get("Content Center Item Id") or "").strip() or None,
        "board_id": (row.get("Board Id") or "").strip() or None,
        "title": (override.get("title") or row.get("Item Name") or "").strip() or None,
        "title_aliases": dedupe_preserve_order(title_aliases),
        "content_type": (override.get("content_type") or row.get("Item Type") or "").strip() or None,
        "summary": (override.get("summary") or "").strip() or None,
        "themes": dedupe_preserve_order(override.get("themes") or _fallback_themes(row)),
        "followup_angle": (override.get("followup_angle") or "").strip() or None,
        "cta_type": (override.get("cta_type") or "").strip() or None,
        "destination_board": (override.get("destination_board") or row.get("Board Id") or "").strip() or None,
        "status": (row.get("Item Status") or "active").strip() or "active",
        "source": (row.get("Source") or SOURCE_NAME).strip() or SOURCE_NAME,
        "source_url": source_url,
        "item_source_value": item_source_value,
        "item_tags": parse_tag_list(row.get("Item Tags")),
        "category_1": (row.get("Category 1") or "").strip() or None,
    }


def _upsert_asset(conn, asset: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO asset_catalog (
                asset_id,
                content_center_item_id,
                board_id,
                title,
                title_aliases,
                content_type,
                summary,
                themes,
                followup_angle,
                cta_type,
                destination_board,
                status,
                source,
                source_url,
                item_source_value,
                item_tags,
                category_1,
                updated_at
            )
            VALUES (
                %(asset_id)s,
                %(content_center_item_id)s,
                %(board_id)s,
                %(title)s,
                %(title_aliases)s::jsonb,
                %(content_type)s,
                %(summary)s,
                %(themes)s::jsonb,
                %(followup_angle)s,
                %(cta_type)s,
                %(destination_board)s,
                %(status)s,
                %(source)s,
                %(source_url)s,
                %(item_source_value)s,
                %(item_tags)s::jsonb,
                %(category_1)s,
                NOW()
            )
            ON CONFLICT (asset_id)
            DO UPDATE SET
                content_center_item_id = EXCLUDED.content_center_item_id,
                board_id = EXCLUDED.board_id,
                title = EXCLUDED.title,
                title_aliases = EXCLUDED.title_aliases,
                content_type = EXCLUDED.content_type,
                summary = EXCLUDED.summary,
                themes = EXCLUDED.themes,
                followup_angle = EXCLUDED.followup_angle,
                cta_type = EXCLUDED.cta_type,
                destination_board = EXCLUDED.destination_board,
                status = EXCLUDED.status,
                source = EXCLUDED.source,
                source_url = EXCLUDED.source_url,
                item_source_value = EXCLUDED.item_source_value,
                item_tags = EXCLUDED.item_tags,
                category_1 = EXCLUDED.category_1,
                updated_at = NOW()
            """,
            {
                **asset,
                "title_aliases": json.dumps(asset["title_aliases"]),
                "themes": json.dumps(asset["themes"]),
                "item_tags": json.dumps(asset["item_tags"]),
            },
        )


def run(
    *,
    path: str | None = None,
    overrides_path: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
) -> None:
    logger = configure_logging("import_folloze_content_catalog")
    report_path = Path(path).expanduser() if path else _discover_latest_report()
    report_updated_at, rows = _load_rows(report_path)
    overrides = load_asset_overrides(overrides_path)
    if limit is not None:
        rows = rows[:limit]

    logger.info(
        "Starting. dry_run=%s path=%s report_updated_at=%s rows=%s overrides=%s",
        dry_run,
        report_path,
        report_updated_at.isoformat(),
        len(rows),
        len(overrides),
    )

    assets = [_build_asset_record(row, overrides) for row in rows]

    if dry_run:
        logger.info("Dry run: prepared %s asset record(s)", len(assets))
        for asset in assets[:10]:
            logger.info(
                "  asset=%s cc=%s board=%s title=%s themes=%s",
                asset["asset_id"],
                asset["content_center_item_id"],
                asset["board_id"],
                asset["title"],
                asset["themes"],
            )
        return

    with db_conn() as conn:
        for asset in assets:
            _upsert_asset(conn, asset)

    logger.info("Done. imported=%s asset record(s)", len(assets))
