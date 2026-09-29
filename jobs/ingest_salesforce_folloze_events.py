"""
Job: ingest_salesforce_folloze_events

Pull normalized Folloze Task events from Salesforce into signal_events.

This is the first production slice of the new signals engine:
  - reads first-party Folloze behavior from Salesforce Tasks
  - stores raw normalized events in signal_events
  - uses kv_config for a compound high-water-mark cursor
  - keeps CRM identity in the raw event so entity resolution can happen later

Usage:
    python main.py ingest_salesforce_folloze_events
    python main.py ingest_salesforce_folloze_events --dry-run --limit 50
    python main.py ingest_salesforce_folloze_events --reset-cursor --since-days 30
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

from connectors.crm_client import CRMClient, _clean_domain, _escape_sf_value
from db.connection import db_conn
from runtime_config import configure_logging

SOURCE_NAME = "salesforce_folloze_task"
CURSOR_KEY = "signals.salesforce_folloze_events.cursor"

TASK_FIELDS = (
    "Id, Subject, ActivityDate, CreatedDate, WhoId, Who.Type, Who.Name, Who.Email, "
    "Description, Folloze_Asset_ID__c, Folloze_Asset_Name__c, Folloze_Board_ID__c, "
    "Folloze_Duration_seconds__c, Folloze_UTM_Campaign__c, Folloze_UTM_Source__c, "
    "Folloze_UTM_Medium__c"
)

logger = logging.getLogger(__name__)


def _parse_sf_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%S%z"):
        try:
            return datetime.strptime(value, fmt).astimezone(timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"Unsupported Salesforce datetime: {value!r}")


def _format_soql_datetime(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _normalize_event_type(subject: str | None) -> str:
    raw = (subject or "").strip()
    raw = re.sub(r"^\[Folloze\]\s*", "", raw, flags=re.IGNORECASE)
    normalized = re.sub(r"[^a-z0-9]+", "_", raw.lower()).strip("_")
    return normalized or "unknown"


def _maybe_int(value) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _email_domain(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    return _clean_domain(email.split("@", 1)[1])


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


def _fetch_account_lookup(conn, *, crm_account_ids: set[str], domains: set[str]) -> tuple[dict[str, str], dict[str, str]]:
    crm_lookup: dict[str, str] = {}
    domain_lookup: dict[str, str] = {}

    with conn.cursor() as cur:
        if crm_account_ids:
            cur.execute(
                """
                SELECT crm_account_id, account_id::text
                FROM accounts
                WHERE crm_account_id = ANY(%s)
                """,
                (list(crm_account_ids),),
            )
            crm_lookup = {row[0]: row[1] for row in cur.fetchall() if row[0]}

        if domains:
            cur.execute(
                """
                SELECT domain, account_id::text
                FROM accounts
                WHERE domain = ANY(%s)
                """,
                (list(domains),),
            )
            domain_lookup = {row[0]: row[1] for row in cur.fetchall() if row[0]}

    return crm_lookup, domain_lookup


def _resolve_people_and_accounts(conn, client: CRMClient, tasks: list[dict]) -> tuple[dict[str, dict], dict[str, dict], dict[str, str], dict[str, str]]:
    person_lookup = client.get_people_by_ids([task.get("WhoId") for task in tasks if task.get("WhoId")])
    emails = {
        (person.get("Email") or "").strip().lower()
        for person in person_lookup.values()
        if (person.get("Email") or "").strip()
    }
    contact_lookup = _fetch_contact_lookup(conn, emails)

    crm_account_ids = {
        (person.get("AccountId") or "").strip()
        for person in person_lookup.values()
        if (person.get("AccountId") or "").strip()
    }
    domains = {
        _email_domain((person.get("Email") or "").strip().lower())
        for person in person_lookup.values()
        if (person.get("Email") or "").strip()
    }
    crm_account_lookup, domain_account_lookup = _fetch_account_lookup(
        conn,
        crm_account_ids=crm_account_ids,
        domains={domain for domain in domains if domain},
    )
    return person_lookup, contact_lookup, crm_account_lookup, domain_account_lookup


def _normalize_task_record(
    task: dict,
    *,
    resolved_person: dict | None = None,
    local_contact: dict | None = None,
    local_account_id: str | None = None,
) -> dict:
    created_at = _parse_sf_datetime(task.get("CreatedDate"))
    if created_at is None:
        raise ValueError(f"Task {task.get('Id')} missing CreatedDate")

    who = resolved_person or task.get("Who") or {}
    event_type = _normalize_event_type(task.get("Subject"))
    board_id = task.get("Folloze_Board_ID__c") or None
    content_id = task.get("Folloze_Asset_ID__c") or None
    if event_type in {"board_view", "anchor"} and content_id == board_id:
        content_id = None

    email = (who.get("Email") or "").strip().lower() or None

    payload = {
        "task_id": task.get("Id"),
        "subject": task.get("Subject"),
        "activity_date": task.get("ActivityDate"),
        "created_date": task.get("CreatedDate"),
        "description": task.get("Description"),
        "crm_person_id": task.get("WhoId"),
        "crm_person_type": who.get("_sf_type") or who.get("Type"),
        "crm_person_name": who.get("Name"),
        "crm_person_email": email,
        "crm_account_id": who.get("AccountId"),
        "local_contact_id": local_contact.get("contact_id") if local_contact else None,
        "local_account_id": local_account_id,
        "asset_id": task.get("Folloze_Asset_ID__c"),
        "asset_name": task.get("Folloze_Asset_Name__c"),
        "board_id": board_id,
        "duration_seconds": _maybe_int(task.get("Folloze_Duration_seconds__c")),
        "utm_campaign": task.get("Folloze_UTM_Campaign__c"),
        "utm_source": task.get("Folloze_UTM_Source__c"),
        "utm_medium": task.get("Folloze_UTM_Medium__c"),
        "sf_url": (task.get("attributes") or {}).get("url"),
    }

    return {
        "source": SOURCE_NAME,
        "external_event_id": task.get("Id"),
        "event_type": event_type,
        "event_timestamp_utc": created_at,
        "ingested_timestamp_utc": datetime.now(timezone.utc),
        "contact_id": local_contact.get("contact_id") if local_contact else None,
        "account_id": local_contact.get("account_id") if local_contact and local_contact.get("account_id") else local_account_id,
        "crm_person_id": task.get("WhoId") or None,
        "crm_person_type": (who.get("_sf_type") or who.get("Type")) or None,
        "content_id": content_id,
        "board_id": board_id,
        "campaign_id": task.get("Folloze_UTM_Campaign__c") or None,
        "touch_number": None,
        "duration_seconds": payload["duration_seconds"],
        "url": payload["sf_url"],
        "confidence": 1.0,
        "payload_json": payload,
    }


def _build_query(
    *,
    cursor_created_at: datetime | None,
    cursor_task_id: str | None,
    fallback_since: datetime,
    limit: int,
) -> str:
    if cursor_created_at:
        created = _format_soql_datetime(cursor_created_at)
        cursor_id = _escape_sf_value(cursor_task_id or "")
        where_clause = (
            "Subject LIKE '[Folloze]%' "
            f"AND (CreatedDate > {created} "
            f"OR (CreatedDate = {created} AND Id > '{cursor_id}'))"
        )
    else:
        since = _format_soql_datetime(fallback_since)
        where_clause = f"Subject LIKE '[Folloze]%' AND CreatedDate >= {since}"

    return (
        f"SELECT {TASK_FIELDS} "
        f"FROM Task "
        f"WHERE {where_clause} "
        f"ORDER BY CreatedDate ASC, Id ASC "
        f"LIMIT {int(limit)}"
    )


def _get_cursor(conn) -> tuple[datetime | None, str | None]:
    with conn.cursor() as cur:
        cur.execute("SELECT value FROM kv_config WHERE key = %s", (CURSOR_KEY,))
        row = cur.fetchone()
    if not row or not row[0]:
        return None, None

    raw = json.loads(row[0])
    return _parse_sf_datetime(raw.get("created_at")), raw.get("task_id")


def _set_cursor(conn, created_at: datetime, task_id: str) -> None:
    payload = json.dumps(
        {
            "created_at": _format_soql_datetime(created_at),
            "task_id": task_id,
        }
    )
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO kv_config (key, value, updated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (key)
            DO UPDATE SET value = EXCLUDED.value,
                          updated_at = NOW()
            """,
            (CURSOR_KEY, payload),
        )


def _clear_cursor(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM kv_config WHERE key = %s", (CURSOR_KEY,))


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
                crm_person_id = EXCLUDED.crm_person_id,
                crm_person_type = EXCLUDED.crm_person_type,
                content_id = EXCLUDED.content_id,
                board_id = EXCLUDED.board_id,
                campaign_id = EXCLUDED.campaign_id,
                touch_number = EXCLUDED.touch_number,
                duration_seconds = EXCLUDED.duration_seconds,
                url = EXCLUDED.url,
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
    dry_run: bool = False,
    limit: int = 500,
    since_days: int = 7,
    reset_cursor: bool = False,
) -> None:
    logger = configure_logging("ingest_salesforce_folloze_events")
    client = CRMClient()
    fallback_since = datetime.now(timezone.utc) - timedelta(days=since_days)

    total_upserted = 0
    total_batches = 0

    with db_conn() as conn:
        if reset_cursor:
            _clear_cursor(conn)
            logger.info("Cleared Salesforce Folloze event cursor")

        cursor_created_at, cursor_task_id = _get_cursor(conn)
        logger.info(
            "Starting. dry_run=%s limit=%s since_days=%s cursor_created_at=%s cursor_task_id=%s",
            dry_run,
            limit,
            since_days,
            cursor_created_at.isoformat() if cursor_created_at else None,
            cursor_task_id,
        )

        while True:
            query = _build_query(
                cursor_created_at=cursor_created_at,
                cursor_task_id=cursor_task_id,
                fallback_since=fallback_since,
                limit=limit,
            )
            tasks = client._run_sf_query(query)
            if not tasks:
                break

            total_batches += 1
            person_lookup, contact_lookup, crm_account_lookup, domain_account_lookup = _resolve_people_and_accounts(
                conn,
                client,
                tasks,
            )
            events = []
            for task in tasks:
                resolved_person = person_lookup.get(task.get("WhoId"))
                email = ((resolved_person or {}).get("Email") or "").strip().lower()
                local_contact = contact_lookup.get(email) if email else None
                local_account_id = None
                if resolved_person and resolved_person.get("AccountId"):
                    local_account_id = crm_account_lookup.get(resolved_person.get("AccountId"))
                if not local_account_id and email:
                    local_account_id = domain_account_lookup.get(_email_domain(email))
                events.append(
                    _normalize_task_record(
                        task,
                        resolved_person=resolved_person,
                        local_contact=local_contact,
                        local_account_id=local_account_id,
                    )
                )
            last_event = events[-1]
            last_created_at = last_event["event_timestamp_utc"]
            last_task_id = last_event["external_event_id"]

            if dry_run:
                logger.info("Batch %s dry run: %s Salesforce task(s)", total_batches, len(events))
                for event in events[:10]:
                    logger.info(
                        "  %s %s crm_person=%s contact=%s board=%s content=%s campaign=%s",
                        event["external_event_id"],
                        event["event_type"],
                        event["crm_person_id"],
                        event["contact_id"],
                        event["board_id"],
                        event["content_id"],
                        event["campaign_id"],
                    )
            else:
                for event in events:
                    _upsert_event(conn, event)
                _set_cursor(conn, last_created_at, last_task_id)

            total_upserted += len(events)
            cursor_created_at = last_created_at
            cursor_task_id = last_task_id

            logger.info(
                "Processed batch %s: %s task(s), cursor now %s / %s",
                total_batches,
                len(events),
                last_created_at.isoformat(),
                last_task_id,
            )

            if len(tasks) < limit:
                break

    logger.info(
        "Done. batches=%s upserted_or_previewed=%s",
        total_batches,
        total_upserted,
    )
