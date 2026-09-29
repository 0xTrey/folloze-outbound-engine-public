"""
Job: ingest_folloze_people_csv

Ingest a Folloze People Engagement CSV snapshot into signal_events.

This path gives us near-term aggregate enrichment before the Folloze API is
available. Each CSV row becomes one `engagement_snapshot` event keyed by the
report timestamp, Folloze lead id, and board id.

Usage:
    python main.py ingest_folloze_people_csv
    python main.py ingest_folloze_people_csv --path /path/to/report.csv
    python main.py ingest_folloze_people_csv --dry-run --limit 25
"""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

from connectors.crm_client import _clean_domain
from db.connection import db_conn
from runtime_config import configure_logging

SOURCE_NAME = "folloze_people_csv"
REPORT_GLOB = "*People Engagement*.csv"

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


def _parse_folloze_ts(value: str | None) -> datetime | None:
    text = (value or "").strip()
    if not text:
        return None
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S UTC").replace(tzinfo=timezone.utc)


def _maybe_int(value) -> int | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _bool_flag(value) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _load_rows(path: Path) -> tuple[datetime, list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.reader(handle)
        try:
            first_row = next(reader)
        except StopIteration as exc:
            raise ValueError(f"CSV is empty: {path}") from exc
        report_updated_at = _parse_report_updated_at(first_row[0] if first_row else "")
        # Skip the blank separator row if present.
        next(reader, None)
        header = next(reader, None)
        if not header:
            raise ValueError(f"CSV is missing a header row: {path}")

        dict_reader = csv.DictReader(handle, fieldnames=header)
        rows = [dict(row) for row in dict_reader if any((cell or "").strip() for cell in row.values())]
        return report_updated_at, rows


def _compute_file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _validate_report_scope(
    rows: list[dict[str, str]],
    *,
    report_updated_at: datetime,
    report_path: Path,
    expected_board_id: str | None = None,
    expected_campaign: str | None = None,
    max_report_age_hours: int | None = 72,
    now_utc: datetime | None = None,
) -> dict[str, str | None]:
    missing_board_rows = [
        index + 1
        for index, row in enumerate(rows)
        if not (row.get("Board Id") or "").strip()
    ]
    if missing_board_rows:
        raise ValueError(
            f"People Engagement report is missing Board Id on {len(missing_board_rows)} row(s); refusing to ingest an ambiguous report."
        )

    board_ids = sorted({(row.get("Board Id") or "").strip() for row in rows if (row.get("Board Id") or "").strip()})
    if len(board_ids) != 1:
        raise ValueError(
            f"People Engagement report mixes board ids {board_ids}; refusing to ingest mixed-board data."
        )
    board_id = board_ids[0]

    campaign_ids = sorted(
        {
            (row.get("Last Utm Campaign") or "").strip()
            for row in rows
            if (row.get("Last Utm Campaign") or "").strip()
        }
    )
    if len(campaign_ids) > 1:
        raise ValueError(
            f"People Engagement report mixes campaigns {campaign_ids}; refusing to ingest mixed-campaign data."
        )
    campaign_id = campaign_ids[0] if campaign_ids else None

    if expected_board_id and board_id != expected_board_id:
        raise ValueError(
            f"People Engagement report board_id={board_id} does not match expected_board_id={expected_board_id}."
        )
    if expected_campaign and campaign_id != expected_campaign:
        raise ValueError(
            f"People Engagement report campaign={campaign_id or 'missing'} does not match expected_campaign={expected_campaign}."
        )

    if max_report_age_hours is not None:
        now_utc = now_utc or datetime.now(timezone.utc)
        age_hours = (now_utc - report_updated_at).total_seconds() / 3600
        if age_hours > max_report_age_hours:
            raise ValueError(
                f"People Engagement report is {age_hours:.1f} hours old, beyond the max_report_age_hours={max_report_age_hours} safety window."
            )

    resolved_path = report_path.expanduser().resolve()
    return {
        "validated_board_id": board_id,
        "validated_campaign_id": campaign_id,
        "source_file_name": report_path.name,
        "source_file_path": str(resolved_path),
        "source_file_sha256": _compute_file_sha256(report_path),
    }


def _fetch_contact_lookup(conn, emails: set[str]) -> dict[str, dict]:
    if not emails:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT LOWER(email) AS email_key, contact_id::text, account_id::text
            FROM contacts
            WHERE LOWER(email) = ANY(%s)
            """,
            (list(emails),),
        )
        return {
            row[0]: {"contact_id": row[1], "account_id": row[2]}
            for row in cur.fetchall()
        }


def _fetch_account_lookup(conn, domains: set[str]) -> dict[str, str]:
    if not domains:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT domain, account_id::text
            FROM accounts
            WHERE domain = ANY(%s)
            """,
            (list(domains),),
        )
        return {row[0]: row[1] for row in cur.fetchall() if row[0]}


def _normalize_snapshot_row(
    row: dict[str, str],
    *,
    report_updated_at: datetime,
    local_contact: dict | None = None,
    local_account_id: str | None = None,
    report_metadata: dict[str, str | None] | None = None,
) -> dict:
    report_metadata = report_metadata or {}
    email = (row.get("Email") or "").strip().lower() or None
    board_id = (row.get("Board Id") or "").strip() or None
    lead_id = (row.get("Lead Id") or "").strip() or None
    last_action_on = _parse_folloze_ts(row.get("Last Action On"))
    joined_board_on = _parse_folloze_ts(row.get("Joined Board On"))
    last_invitation_on = _parse_folloze_ts(row.get("Last Invitation On"))
    event_timestamp = last_action_on or joined_board_on or last_invitation_on or report_updated_at

    payload = {
        "report_updated_at": report_updated_at.isoformat(),
        "lead_id": lead_id,
        "email": email,
        "first_name": (row.get("First Name") or "").strip() or None,
        "last_name": (row.get("Last Name") or "").strip() or None,
        "role": (row.get("Role") or "").strip() or None,
        "lead_status": (row.get("Lead Status") or "").strip() or None,
        "lead_event_status": (row.get("Lead Event Status") or "").strip() or None,
        "company": (row.get("Company") or "").strip() or None,
        "domain": _clean_domain((row.get("Domain") or "").strip().lower()),
        "board_id": board_id,
        "board_name": (row.get("Board Name") or "").strip() or None,
        "last_utm_source": (row.get("Last Utm Source") or "").strip() or None,
        "last_utm_medium": (row.get("Last Utm Medium") or "").strip() or None,
        "last_utm_campaign": (row.get("Last Utm Campaign") or "").strip() or None,
        "joined_board_on": joined_board_on.isoformat() if joined_board_on else None,
        "last_action_on": last_action_on.isoformat() if last_action_on else None,
        "number_of_board_views": _maybe_int(row.get("Number Of Board Views")),
        "number_of_items_viewed": _maybe_int(row.get("Number Of Items Viewed")),
        "number_of_forms_filled": _maybe_int(row.get("Number Of Forms Filled")),
        "number_of_links_clicked": _maybe_int(row.get("Number Of Links Clicked")),
        "number_of_messages_sent": _maybe_int(row.get("Number Of Messages Sent")),
        "number_of_request_to_contact": _maybe_int(row.get("Number Of Request To Contact")),
        "number_of_cta_clicks": _maybe_int(row.get("Number Of Cta Clicks")),
        "number_of_downloads": _maybe_int(row.get("Number Of Downloads")),
        "total_view_time": _maybe_int(row.get("Total View Time")),
        "number_of_days_visited": _maybe_int(row.get("Number Of Days Visited")),
        "number_of_emailed_to_clicked": _maybe_int(row.get("Number Of Emailed To Clicked")),
        "most_viewed_item": (row.get("Most Viewed Item") or "").strip() or None,
        "email_delivered": _bool_flag(row.get("Email Delivered (Y/N)?")),
        "email_opened": _bool_flag(row.get("Email Opened (Y/N)?")),
        "external_invitation": _bool_flag(row.get("External Invitation (Y/N)?")),
        "item_viewer": _bool_flag(row.get("Item Viewer (Y/N)?")),
        "cta_clicker": _bool_flag(row.get("Cta Clicker (Y/N)?")),
        "source_file_name": report_metadata.get("source_file_name"),
        "source_file_path": report_metadata.get("source_file_path"),
        "source_file_sha256": report_metadata.get("source_file_sha256"),
        "validated_board_id": report_metadata.get("validated_board_id"),
        "validated_campaign_id": report_metadata.get("validated_campaign_id"),
    }

    external_event_id = ":".join(
        [
            report_updated_at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            lead_id or "no-lead-id",
            board_id or "no-board-id",
        ]
    )

    return {
        "source": SOURCE_NAME,
        "external_event_id": external_event_id,
        "event_type": "engagement_snapshot",
        "event_timestamp_utc": event_timestamp,
        "ingested_timestamp_utc": datetime.now(timezone.utc),
        "contact_id": local_contact.get("contact_id") if local_contact else None,
        "account_id": local_contact.get("account_id") if local_contact and local_contact.get("account_id") else local_account_id,
        "crm_person_id": None,
        "crm_person_type": None,
        "content_id": None,
        "board_id": board_id,
        "campaign_id": payload["last_utm_campaign"],
        "touch_number": None,
        "duration_seconds": payload["total_view_time"],
        "url": None,
        "confidence": 0.95,
        "payload_json": payload,
    }


def _upsert_event(conn, event: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO signal_events (
                source,
                external_event_id,
                event_type,
                event_timestamp_utc,
                ingested_timestamp_utc,
                contact_id,
                account_id,
                crm_person_id,
                crm_person_type,
                content_id,
                board_id,
                campaign_id,
                touch_number,
                duration_seconds,
                url,
                confidence,
                payload_json
            )
            VALUES (
                %(source)s,
                %(external_event_id)s,
                %(event_type)s,
                %(event_timestamp_utc)s,
                %(ingested_timestamp_utc)s,
                %(contact_id)s,
                %(account_id)s,
                %(crm_person_id)s,
                %(crm_person_type)s,
                %(content_id)s,
                %(board_id)s,
                %(campaign_id)s,
                %(touch_number)s,
                %(duration_seconds)s,
                %(url)s,
                %(confidence)s,
                %(payload_json)s::jsonb
            )
            ON CONFLICT (source, external_event_id)
            DO UPDATE SET
                event_type = EXCLUDED.event_type,
                event_timestamp_utc = EXCLUDED.event_timestamp_utc,
                ingested_timestamp_utc = EXCLUDED.ingested_timestamp_utc,
                contact_id = EXCLUDED.contact_id,
                account_id = EXCLUDED.account_id,
                board_id = EXCLUDED.board_id,
                campaign_id = EXCLUDED.campaign_id,
                duration_seconds = EXCLUDED.duration_seconds,
                confidence = EXCLUDED.confidence,
                payload_json = EXCLUDED.payload_json
            """,
            {
                **event,
                "payload_json": json.dumps(event["payload_json"]),
            },
        )


def run(
    *,
    path: str | None = None,
    dry_run: bool = False,
    limit: int | None = None,
    expected_board_id: str | None = None,
    expected_campaign: str | None = None,
    max_report_age_hours: int | None = 72,
) -> None:
    logger = configure_logging("ingest_folloze_people_csv")
    report_path = Path(path).expanduser() if path else _discover_latest_report()
    report_updated_at, rows = _load_rows(report_path)
    report_metadata = _validate_report_scope(
        rows,
        report_updated_at=report_updated_at,
        report_path=report_path,
        expected_board_id=expected_board_id,
        expected_campaign=expected_campaign,
        max_report_age_hours=max_report_age_hours,
    )
    if limit is not None:
        rows = rows[:limit]

    logger.info(
        "Starting. dry_run=%s path=%s report_updated_at=%s rows=%s board_id=%s campaign=%s",
        dry_run,
        report_path,
        report_updated_at.isoformat(),
        len(rows),
        report_metadata.get("validated_board_id"),
        report_metadata.get("validated_campaign_id"),
    )

    emails = {
        (row.get("Email") or "").strip().lower()
        for row in rows
        if (row.get("Email") or "").strip()
    }
    domains = {
        _clean_domain((row.get("Domain") or "").strip().lower())
        for row in rows
        if (row.get("Domain") or "").strip()
    }

    with db_conn() as conn:
        contact_lookup = _fetch_contact_lookup(conn, emails)
        account_lookup = _fetch_account_lookup(conn, {domain for domain in domains if domain})

        events = []
        for row in rows:
            email = (row.get("Email") or "").strip().lower()
            local_contact = contact_lookup.get(email) if email else None
            local_account_id = None
            if not local_contact:
                domain = _clean_domain((row.get("Domain") or "").strip().lower())
                local_account_id = account_lookup.get(domain)
            events.append(
                _normalize_snapshot_row(
                    row,
                    report_updated_at=report_updated_at,
                    local_contact=local_contact,
                    local_account_id=local_account_id,
                    report_metadata=report_metadata,
                )
            )

        if dry_run:
            logger.info("Dry run: prepared %s snapshot event(s)", len(events))
            for event in events[:10]:
                payload = event["payload_json"]
                logger.info(
                    "  %s contact=%s account=%s board=%s items=%s ctas=%s most_viewed=%s",
                    event["external_event_id"],
                    event["contact_id"],
                    event["account_id"],
                    event["board_id"],
                    payload.get("number_of_items_viewed"),
                    payload.get("number_of_cta_clicks"),
                    payload.get("most_viewed_item"),
                )
            return

        for event in events:
            _upsert_event(conn, event)

    logger.info("Done. ingested=%s snapshot event(s)", len(events))
