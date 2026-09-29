from __future__ import annotations

import logging
import time
from typing import Any

try:
    import psycopg2
    from psycopg2 import pool
except ImportError as exc:
    psycopg2 = None
    pool = None
    _PSYCOPG_IMPORT_ERROR = exc
else:
    _PSYCOPG_IMPORT_ERROR = None

from runtime_config import get_required_setting

logger = logging.getLogger(__name__)

_pool = None

_DEAD_CONN_ERRORS = (
    (psycopg2.OperationalError, psycopg2.InterfaceError)
    if psycopg2 is not None
    else (RuntimeError,)
)
_RETRY_DELAYS = (1, 2, 4)  # seconds between retries; 3 retries = 4 total attempts


def _require_psycopg2() -> None:
    if psycopg2 is None or pool is None:
        raise RuntimeError("psycopg2 is required for database access in this environment") from _PSYCOPG_IMPORT_ERROR


def get_pool() -> Any:
    global _pool
    _require_psycopg2()
    if _pool is None:
        url = get_required_setting("FOLLOZE_DATABASE_URL", keychain_service="folloze-outbound-db")
        _pool = pool.ThreadedConnectionPool(minconn=1, maxconn=10, dsn=url)
        conn = _pool.getconn()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT to_regclass('public.accounts'), to_regclass('public.contacts')"
                )
                tables = cur.fetchone()
        finally:
            _pool.putconn(conn)
        if not tables or any(value is None for value in tables):
            raise RuntimeError("Folloze runtime connected to a database without the expected schema")
    return _pool


def _reset_pool() -> None:
    """Discard the current pool. A fresh one is created on the next get_pool() call."""
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
        _pool = None


def get_conn():
    """Acquire a live connection, retrying up to 3 times with exponential backoff.

    Neon drops idle connections after ~5 minutes. On OperationalError or
    InterfaceError, the pool is reset and the acquire is retried with 1s/2s/4s
    delays between attempts.
    """
    last_exc: Exception | None = None
    _require_psycopg2()
    for delay in (None, *_RETRY_DELAYS):
        if delay is not None:
            _reset_pool()
            time.sleep(delay)
        try:
            conn = get_pool().getconn()
            conn.isolation_level  # raises InterfaceError if the connection is dead
            return conn
        except _DEAD_CONN_ERRORS as exc:
            last_exc = exc
            logger.warning("get_conn: attempt failed (%s)%s", exc,
                           f" — retrying in {delay}s" if delay else " — retrying")
    logger.warning("get_conn: all retries exhausted")
    raise last_exc


def release_conn(conn) -> None:
    try:
        get_pool().putconn(conn)
    except _DEAD_CONN_ERRORS as exc:
        logger.warning("release_conn: connection is dead, discarding (%s)", exc)


def try_advisory_xact_lock(conn, lock_name: str) -> bool:
    """Acquire a transaction-scoped advisory lock by name.

    Returns True when the lock is acquired and False when another transaction
    already holds it. The lock is released automatically at transaction end.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", (lock_name,))
        row = cur.fetchone()
    return bool(row and row[0])


class db_conn:
    """Context manager: acquires a connection, commits on success, rolls back on error."""

    def __enter__(self):
        self.conn = get_conn()
        self.conn.autocommit = False
        return self.conn

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
        except _DEAD_CONN_ERRORS as exc:
            logger.warning("db_conn.__exit__: connection dead during cleanup (%s)", exc)
        release_conn(self.conn)
        return False
