"""
Job: derive_contact_signal_state

Derive prompt-ready `contact_signal_state` and lightweight `account_signal_state`
from normalized signal_events.

This is the first operational memory layer for the signals engine. It turns raw
events into interpretable flags and summary fields the generation layer can use.

Usage:
    python main.py derive_contact_signal_state
    python main.py derive_contact_signal_state --dry-run
    python main.py derive_contact_signal_state --lookback-days 30 --contact-id <uuid>
"""
from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any

import psycopg2.extras

from db.connection import db_conn
from jobs.folloze_asset_catalog import resolve_asset_by_title
from runtime_config import configure_logging

logger = logging.getLogger(__name__)


def _to_utc_iso(value: Any) -> str | None:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
    return str(value)


def _bool_flag(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _engagement_weight(level: str | None) -> float:
    return {
        "high": 3.0,
        "medium": 2.0,
        "low": 1.0,
    }.get((level or "").strip().lower(), 0.0)


def _fetch_contacts_with_signals(conn, *, lookback_days: int, contact_id_filter: str | None) -> list[dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                c.contact_id::text,
                c.account_id::text,
                c.email,
                c.first_name,
                c.last_name,
                c.title,
                c.persona_type,
                a.company_name,
                a.domain
            FROM contacts c
            LEFT JOIN accounts a
              ON a.account_id = c.account_id
            WHERE EXISTS (
                SELECT 1
                FROM signal_events se
                WHERE se.contact_id = c.contact_id
                  AND se.event_timestamp_utc >= NOW() - (%s || ' days')::interval
            )
              AND (%s IS NULL OR c.contact_id = %s::uuid)
            ORDER BY c.created_at ASC
            """,
            (lookback_days, contact_id_filter, contact_id_filter),
        )
        return [dict(row) for row in cur.fetchall()]


def _fetch_contact_events(conn, *, contact_ids: list[str], lookback_days: int) -> dict[str, list[dict[str, Any]]]:
    if not contact_ids:
        return {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT
                se.contact_id::text,
                se.account_id::text,
                se.event_type,
                se.event_timestamp_utc,
                se.content_id,
                se.board_id,
                se.campaign_id,
                se.duration_seconds,
                se.payload_json
            FROM signal_events se
            WHERE se.contact_id = ANY(%s::uuid[])
              AND se.event_timestamp_utc >= NOW() - (%s || ' days')::interval
            ORDER BY se.event_timestamp_utc DESC, se.created_at DESC
            """,
            (contact_ids, lookback_days),
        )
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in cur.fetchall():
            grouped[row["contact_id"]].append(dict(row))
        return grouped


def _fetch_asset_map(conn, *, content_ids: set[str] | None = None) -> dict[str, dict[str, Any]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        if content_ids:
            cur.execute(
                """
                SELECT
                    asset_id,
                    content_center_item_id,
                    board_id,
                    title,
                    title_aliases,
                    summary,
                    themes,
                    destination_board
                FROM asset_catalog
                WHERE asset_id = ANY(%s)
                """,
                (list(content_ids),),
            )
        else:
            cur.execute(
                """
                SELECT
                    asset_id,
                    content_center_item_id,
                    board_id,
                    title,
                    title_aliases,
                    summary,
                    themes,
                    destination_board
                FROM asset_catalog
                """
            )
        return {row["asset_id"]: dict(row) for row in cur.fetchall()}


def _latest_snapshot(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in events:
        if event.get("event_type") == "engagement_snapshot":
            return event
    return None


def _derive_contact_state(contact: dict[str, Any], events: list[dict[str, Any]], asset_map: dict[str, dict[str, Any]]) -> dict[str, Any]:
    latest_snapshot = _latest_snapshot(events)
    snapshot_payload = (latest_snapshot or {}).get("payload_json") or {}
    snapshot_board_id = snapshot_payload.get("board_id") or next(
        (event.get("board_id") for event in events if event.get("board_id")),
        None,
    )

    board_views = [event for event in events if event.get("event_type") == "board_view"]
    item_views = [event for event in events if event.get("event_type") == "item_view"]
    open_content_items = [event for event in events if event.get("event_type") == "open_content_item"]
    cta_clicks = [event for event in events if event.get("event_type") == "cta_click"]

    content_events = [event for event in [*item_views, *open_content_items] if event.get("content_id")]
    content_stats: dict[str, dict[str, Any]] = {}
    theme_counter: Counter[str] = Counter()

    for event in content_events:
        content_id = event["content_id"]
        stats = content_stats.setdefault(content_id, {"views": 0, "duration": 0})
        stats["views"] += 1
        stats["duration"] += int(event.get("duration_seconds") or 0)
        asset = asset_map.get(content_id) or {}
        for theme in asset.get("themes") or []:
            if theme:
                theme_counter[str(theme)] += 1

    top_asset_id = None
    if content_stats:
        top_asset_id = max(
            content_stats,
            key=lambda cid: (
                content_stats[cid]["duration"],
                content_stats[cid]["views"],
                cid,
            ),
        )

    top_asset = asset_map.get(top_asset_id) if top_asset_id else None
    snapshot_asset_match = None
    if not top_asset and snapshot_payload.get("most_viewed_item"):
        snapshot_asset_match = resolve_asset_by_title(
            snapshot_payload.get("most_viewed_item"),
            asset_map,
            board_id=snapshot_board_id,
        )
        if snapshot_asset_match:
            top_asset = snapshot_asset_match["asset"]
            top_asset_id = top_asset.get("asset_id")

    top_asset_summary = None
    if top_asset:
        top_asset_summary = top_asset.get("summary") or top_asset.get("title")
    if not top_asset_summary:
        top_asset_summary = snapshot_payload.get("most_viewed_item")

    days_visited = snapshot_payload.get("number_of_days_visited")
    if days_visited is None:
        visited_days = {
            event["event_timestamp_utc"].astimezone(timezone.utc).date().isoformat()
            for event in events
            if event.get("event_timestamp_utc")
        }
        days_visited = len(visited_days)

    items_viewed = snapshot_payload.get("number_of_items_viewed")
    if items_viewed is None:
        items_viewed = len(content_stats)

    total_view_time_seconds = snapshot_payload.get("total_view_time")
    if total_view_time_seconds is None:
        total_view_time_seconds = sum(int(event.get("duration_seconds") or 0) for event in item_views)

    cta_click_count = snapshot_payload.get("number_of_cta_clicks")
    if cta_click_count is None:
        cta_click_count = len(cta_clicks)

    cta_click_flag = bool(cta_click_count or _bool_flag(snapshot_payload.get("cta_clicker")))
    repeat_visitor_flag = (days_visited or 0) >= 2

    distinct_content_ids = set(content_stats)
    max_single_duration = max((stats["duration"] for stats in content_stats.values()), default=0)
    single_asset_deep_view_flag = len(distinct_content_ids) == 1 and (
        max_single_duration >= 45 or (items_viewed or 0) == 1 and (total_view_time_seconds or 0) >= 45
    )
    multi_asset_same_theme_flag = any(count >= 2 for count in theme_counter.values()) and len(distinct_content_ids) >= 2

    top_theme = theme_counter.most_common(1)[0][0] if theme_counter else None
    if not top_theme and top_asset:
        asset_themes = top_asset.get("themes") or []
        if asset_themes:
            top_theme = str(asset_themes[0])
    recommended_destination_board = (
        (top_asset or {}).get("destination_board")
        or snapshot_board_id
        or next((event.get("board_id") for event in events if event.get("board_id")), None)
    )

    if cta_click_flag:
        engagement_level = "high"
        recommended_next_angle = "cta_click_followup"
        recommended_play_id = "intent_surge_plus_engaged_content_path"
    elif multi_asset_same_theme_flag:
        engagement_level = "high"
        recommended_next_angle = "multi_asset_theme_followup"
        recommended_play_id = "intent_surge_plus_engaged_content_path"
    elif single_asset_deep_view_flag:
        engagement_level = "medium"
        recommended_next_angle = "deep_asset_followup"
        recommended_play_id = "intent_surge_plus_engaged_content_path"
    elif repeat_visitor_flag:
        engagement_level = "medium"
        recommended_next_angle = "repeat_visitor_followup"
        recommended_play_id = None
    elif board_views or latest_snapshot:
        engagement_level = "low"
        recommended_next_angle = "board_recap_followup"
        recommended_play_id = None
    else:
        engagement_level = None
        recommended_next_angle = None
        recommended_play_id = None

    evidence_points = 0
    evidence_points += 1 if latest_snapshot else 0
    evidence_points += 1 if board_views else 0
    evidence_points += 1 if content_events else 0
    evidence_points += 1 if cta_click_flag else 0
    evidence_points += 1 if top_theme else 0
    signal_confidence = min(1.0, 0.2 * evidence_points)

    last_activity_at = next((event.get("event_timestamp_utc") for event in events if event.get("event_timestamp_utc")), None)
    if last_activity_at is not None and not isinstance(last_activity_at, datetime):
        last_activity_at = None

    return {
        "contact_id": contact["contact_id"],
        "account_id": contact.get("account_id"),
        "last_activity_at": last_activity_at,
        "engagement_level": engagement_level,
        "top_theme": top_theme,
        "top_asset_id": top_asset_id,
        "top_asset_summary": top_asset_summary,
        "repeat_visitor_flag": repeat_visitor_flag,
        "cta_click_flag": cta_click_flag,
        "single_asset_deep_view_flag": single_asset_deep_view_flag,
        "multi_asset_same_theme_flag": multi_asset_same_theme_flag,
        "days_visited": days_visited,
        "number_of_items_viewed": items_viewed,
        "total_view_time_seconds": total_view_time_seconds,
        "signal_confidence": signal_confidence,
        "recommended_next_angle": recommended_next_angle,
        "recommended_destination_board": recommended_destination_board,
        "recommended_play_id": recommended_play_id,
        "state_payload_json": {
            "email": contact.get("email"),
            "title": contact.get("title"),
            "company_name": contact.get("company_name"),
            "board_view_count": len(board_views),
            "item_view_count": len(item_views),
            "open_content_item_count": len(open_content_items),
            "cta_click_count": cta_click_count,
            "distinct_content_ids": sorted(distinct_content_ids),
            "content_stats": content_stats,
            "theme_counts": dict(theme_counter),
            "latest_snapshot_payload": snapshot_payload,
            "snapshot_asset_match": snapshot_asset_match,
            "last_event_types": [event.get("event_type") for event in events[:10]],
            "evidence_points": evidence_points,
            "last_activity_at": _to_utc_iso(last_activity_at),
        },
    }


def _upsert_contact_state(conn, state: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO contact_signal_state (
                contact_id,
                account_id,
                last_activity_at,
                engagement_level,
                top_theme,
                top_asset_id,
                top_asset_summary,
                repeat_visitor_flag,
                cta_click_flag,
                single_asset_deep_view_flag,
                multi_asset_same_theme_flag,
                days_visited,
                number_of_items_viewed,
                total_view_time_seconds,
                signal_confidence,
                recommended_next_angle,
                recommended_destination_board,
                recommended_play_id,
                state_payload_json,
                updated_at
            )
            VALUES (
                %(contact_id)s::uuid,
                %(account_id)s::uuid,
                %(last_activity_at)s,
                %(engagement_level)s,
                %(top_theme)s,
                %(top_asset_id)s,
                %(top_asset_summary)s,
                %(repeat_visitor_flag)s,
                %(cta_click_flag)s,
                %(single_asset_deep_view_flag)s,
                %(multi_asset_same_theme_flag)s,
                %(days_visited)s,
                %(number_of_items_viewed)s,
                %(total_view_time_seconds)s,
                %(signal_confidence)s,
                %(recommended_next_angle)s,
                %(recommended_destination_board)s,
                %(recommended_play_id)s,
                %(state_payload_json)s::jsonb,
                NOW()
            )
            ON CONFLICT (contact_id)
            DO UPDATE SET
                account_id = EXCLUDED.account_id,
                last_activity_at = EXCLUDED.last_activity_at,
                engagement_level = EXCLUDED.engagement_level,
                top_theme = EXCLUDED.top_theme,
                top_asset_id = EXCLUDED.top_asset_id,
                top_asset_summary = EXCLUDED.top_asset_summary,
                repeat_visitor_flag = EXCLUDED.repeat_visitor_flag,
                cta_click_flag = EXCLUDED.cta_click_flag,
                single_asset_deep_view_flag = EXCLUDED.single_asset_deep_view_flag,
                multi_asset_same_theme_flag = EXCLUDED.multi_asset_same_theme_flag,
                days_visited = EXCLUDED.days_visited,
                number_of_items_viewed = EXCLUDED.number_of_items_viewed,
                total_view_time_seconds = EXCLUDED.total_view_time_seconds,
                signal_confidence = EXCLUDED.signal_confidence,
                recommended_next_angle = EXCLUDED.recommended_next_angle,
                recommended_destination_board = EXCLUDED.recommended_destination_board,
                recommended_play_id = EXCLUDED.recommended_play_id,
                state_payload_json = EXCLUDED.state_payload_json,
                updated_at = NOW()
            WHERE contact_signal_state.account_id IS DISTINCT FROM EXCLUDED.account_id
               OR contact_signal_state.last_activity_at IS DISTINCT FROM EXCLUDED.last_activity_at
               OR contact_signal_state.engagement_level IS DISTINCT FROM EXCLUDED.engagement_level
               OR contact_signal_state.top_theme IS DISTINCT FROM EXCLUDED.top_theme
               OR contact_signal_state.top_asset_id IS DISTINCT FROM EXCLUDED.top_asset_id
               OR contact_signal_state.top_asset_summary IS DISTINCT FROM EXCLUDED.top_asset_summary
               OR contact_signal_state.repeat_visitor_flag IS DISTINCT FROM EXCLUDED.repeat_visitor_flag
               OR contact_signal_state.cta_click_flag IS DISTINCT FROM EXCLUDED.cta_click_flag
               OR contact_signal_state.single_asset_deep_view_flag IS DISTINCT FROM EXCLUDED.single_asset_deep_view_flag
               OR contact_signal_state.multi_asset_same_theme_flag IS DISTINCT FROM EXCLUDED.multi_asset_same_theme_flag
               OR contact_signal_state.days_visited IS DISTINCT FROM EXCLUDED.days_visited
               OR contact_signal_state.number_of_items_viewed IS DISTINCT FROM EXCLUDED.number_of_items_viewed
               OR contact_signal_state.total_view_time_seconds IS DISTINCT FROM EXCLUDED.total_view_time_seconds
               OR contact_signal_state.signal_confidence IS DISTINCT FROM EXCLUDED.signal_confidence
               OR contact_signal_state.recommended_next_angle IS DISTINCT FROM EXCLUDED.recommended_next_angle
               OR contact_signal_state.recommended_destination_board IS DISTINCT FROM EXCLUDED.recommended_destination_board
               OR contact_signal_state.recommended_play_id IS DISTINCT FROM EXCLUDED.recommended_play_id
               OR contact_signal_state.state_payload_json IS DISTINCT FROM EXCLUDED.state_payload_json
            """,
            {
                **state,
                "state_payload_json": json.dumps(state["state_payload_json"]),
            },
        )


def _derive_account_states(contact_states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for state in contact_states:
        if state.get("account_id"):
            grouped[state["account_id"]].append(state)

    results = []
    for account_id, states in grouped.items():
        theme_counter = Counter(state.get("top_theme") for state in states if state.get("top_theme"))
        play_counter = Counter(state.get("recommended_play_id") for state in states if state.get("recommended_play_id"))
        last_meaningful = max(
            (
                state.get("last_activity_at")
                for state in states
                if isinstance(state.get("last_activity_at"), datetime)
            ),
            default=None,
        )
        intent_strength = sum(_engagement_weight(state.get("engagement_level")) for state in states)
        results.append(
            {
                "account_id": account_id,
                "topic_distribution_json": dict(theme_counter),
                "buying_group_coverage_score": float(len(states)),
                "intent_strength": float(intent_strength),
                "competitive_context": None,
                "open_opp_flag": False,
                "last_meaningful_engagement_at": last_meaningful,
                "recommended_play_id": play_counter.most_common(1)[0][0] if play_counter else None,
                "state_payload_json": {
                    "contact_ids": [state["contact_id"] for state in states],
                    "engagement_levels": Counter(state.get("engagement_level") for state in states),
                    "top_themes": dict(theme_counter),
                },
            }
        )
    return results


def _upsert_account_state(conn, state: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO account_signal_state (
                account_id,
                topic_distribution_json,
                buying_group_coverage_score,
                intent_strength,
                competitive_context,
                open_opp_flag,
                last_meaningful_engagement_at,
                recommended_play_id,
                state_payload_json,
                updated_at
            )
            VALUES (
                %(account_id)s::uuid,
                %(topic_distribution_json)s::jsonb,
                %(buying_group_coverage_score)s,
                %(intent_strength)s,
                %(competitive_context)s,
                %(open_opp_flag)s,
                %(last_meaningful_engagement_at)s,
                %(recommended_play_id)s,
                %(state_payload_json)s::jsonb,
                NOW()
            )
            ON CONFLICT (account_id)
            DO UPDATE SET
                topic_distribution_json = EXCLUDED.topic_distribution_json,
                buying_group_coverage_score = EXCLUDED.buying_group_coverage_score,
                intent_strength = EXCLUDED.intent_strength,
                competitive_context = EXCLUDED.competitive_context,
                open_opp_flag = EXCLUDED.open_opp_flag,
                last_meaningful_engagement_at = EXCLUDED.last_meaningful_engagement_at,
                recommended_play_id = EXCLUDED.recommended_play_id,
                state_payload_json = EXCLUDED.state_payload_json,
                updated_at = NOW()
            WHERE account_signal_state.topic_distribution_json IS DISTINCT FROM EXCLUDED.topic_distribution_json
               OR account_signal_state.buying_group_coverage_score IS DISTINCT FROM EXCLUDED.buying_group_coverage_score
               OR account_signal_state.intent_strength IS DISTINCT FROM EXCLUDED.intent_strength
               OR account_signal_state.competitive_context IS DISTINCT FROM EXCLUDED.competitive_context
               OR account_signal_state.open_opp_flag IS DISTINCT FROM EXCLUDED.open_opp_flag
               OR account_signal_state.last_meaningful_engagement_at IS DISTINCT FROM EXCLUDED.last_meaningful_engagement_at
               OR account_signal_state.recommended_play_id IS DISTINCT FROM EXCLUDED.recommended_play_id
               OR account_signal_state.state_payload_json IS DISTINCT FROM EXCLUDED.state_payload_json
            """,
            {
                **state,
                "topic_distribution_json": json.dumps(state["topic_distribution_json"]),
                "state_payload_json": json.dumps(state["state_payload_json"]),
            },
        )


def run(*, dry_run: bool = False, lookback_days: int = 60, contact_id_filter: str | None = None) -> None:
    logger = configure_logging("derive_contact_signal_state")

    with db_conn() as conn:
        contacts = _fetch_contacts_with_signals(
            conn,
            lookback_days=lookback_days,
            contact_id_filter=contact_id_filter,
        )
        contact_ids = [contact["contact_id"] for contact in contacts]
        events_by_contact = _fetch_contact_events(conn, contact_ids=contact_ids, lookback_days=lookback_days)
        asset_map = _fetch_asset_map(conn)

        states = [
            _derive_contact_state(contact, events_by_contact.get(contact["contact_id"], []), asset_map)
            for contact in contacts
        ]
        account_states = _derive_account_states(states)

        logger.info(
            "Derived %s contact state row(s) and %s account state row(s)",
            len(states),
            len(account_states),
        )

        if dry_run:
            for state in states[:10]:
                logger.info(
                    "  contact=%s engagement=%s cta=%s repeat=%s top_asset=%s top_theme=%s angle=%s",
                    state["contact_id"],
                    state["engagement_level"],
                    state["cta_click_flag"],
                    state["repeat_visitor_flag"],
                    state["top_asset_id"],
                    state["top_theme"],
                    state["recommended_next_angle"],
                )
            return

        for state in states:
            _upsert_contact_state(conn, state)
        for account_state in account_states:
            _upsert_account_state(conn, account_state)

    logger.info("Done.")
