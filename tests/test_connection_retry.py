"""
Tests for db/connection.py retry logic and safe cleanup.

Tests A-H cover:
  A — get_conn() succeeds on first attempt
  B — get_conn() fails once, succeeds on retry (pool reset verified)
  C — get_conn() fails all retries, raises
  D — pool.closeall() called on reset
  E — db_conn.__exit__ commit succeeds (normal path)
  F — db_conn.__exit__ rollback on dead connection (logs warning, no raise)
  G — db_conn.__exit__ commit on dead connection (logs warning, no raise)
  H — release_conn on dead connection (no raise)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from unittest.mock import MagicMock, patch, call, PropertyMock
import pytest
import psycopg2

import db.connection as conn_module
from db.connection import db_conn, release_conn, _reset_pool


def _fresh_module():
    """Reset module-level pool state before each test."""
    conn_module._pool = None


# ── A: first attempt succeeds ─────────────────────────────────────────────────

def test_get_conn_succeeds_first_attempt():
    _fresh_module()
    mock_pool = MagicMock()
    mock_conn = MagicMock()
    mock_pool.getconn.return_value = mock_conn

    with patch("db.connection.get_pool", return_value=mock_pool):
        result = conn_module.get_conn()

    assert result is mock_conn
    mock_pool.getconn.assert_called_once()


# ── B: fails once, succeeds on retry ─────────────────────────────────────────

def test_get_conn_retries_after_one_failure():
    _fresh_module()
    mock_pool = MagicMock()
    dead_conn = MagicMock()
    live_conn = MagicMock()

    # First getconn returns dead conn (isolation_level raises), second returns live conn
    type(dead_conn).isolation_level = PropertyMock(
        side_effect=psycopg2.InterfaceError("connection already closed")
    )
    mock_pool.getconn.side_effect = [dead_conn, live_conn]

    reset_calls = []

    with patch("db.connection.get_pool", return_value=mock_pool), \
         patch("db.connection._reset_pool", side_effect=lambda: reset_calls.append(1)), \
         patch("db.connection.time.sleep"):
        result = conn_module.get_conn()

    assert result is live_conn
    assert len(reset_calls) >= 1


# ── C: all retries exhausted, raises ─────────────────────────────────────────

def test_get_conn_raises_after_all_retries():
    _fresh_module()
    mock_pool = MagicMock()
    dead_conn = MagicMock()
    type(dead_conn).isolation_level = PropertyMock(
        side_effect=psycopg2.InterfaceError("dead")
    )
    mock_pool.getconn.return_value = dead_conn

    with patch("db.connection.get_pool", return_value=mock_pool), \
         patch("db.connection._reset_pool"), \
         patch("db.connection.time.sleep"):
        with pytest.raises(psycopg2.InterfaceError):
            conn_module.get_conn()


# ── D: pool.closeall() called on reset ───────────────────────────────────────

def test_reset_pool_calls_closeall():
    mock_pool = MagicMock()
    conn_module._pool = mock_pool

    _reset_pool()

    mock_pool.closeall.assert_called_once()
    assert conn_module._pool is None


# ── E: normal commit succeeds ─────────────────────────────────────────────────

def test_db_conn_exit_commits_on_success():
    mock_conn = MagicMock()
    with patch("db.connection.get_conn", return_value=mock_conn), \
         patch("db.connection.release_conn"):
        with db_conn() as c:
            assert c is mock_conn
    mock_conn.commit.assert_called_once()
    mock_conn.rollback.assert_not_called()


# ── F: rollback on dead connection — logs, no raise ──────────────────────────

def test_db_conn_exit_rollback_on_dead_connection(caplog):
    import logging
    mock_conn = MagicMock()
    mock_conn.rollback.side_effect = psycopg2.InterfaceError("dead")

    with patch("db.connection.get_conn", return_value=mock_conn), \
         patch("db.connection.release_conn"):
        with caplog.at_level(logging.WARNING, logger="db.connection"):
            with pytest.raises(RuntimeError):
                with db_conn():
                    raise RuntimeError("job error")

    assert any("dead" in r.message.lower() or "cleanup" in r.message.lower()
               for r in caplog.records)


# ── G: commit on dead connection — logs, no raise ────────────────────────────

def test_db_conn_exit_commit_on_dead_connection(caplog):
    import logging
    mock_conn = MagicMock()
    mock_conn.commit.side_effect = psycopg2.InterfaceError("dead")

    with patch("db.connection.get_conn", return_value=mock_conn), \
         patch("db.connection.release_conn"):
        with caplog.at_level(logging.WARNING, logger="db.connection"):
            with db_conn():
                pass  # no error — triggers commit path

    assert any("dead" in r.message.lower() or "cleanup" in r.message.lower()
               for r in caplog.records)


# ── H: release_conn on dead connection — no raise ────────────────────────────

def test_release_conn_on_dead_connection_no_raise():
    _fresh_module()
    mock_pool = MagicMock()
    mock_pool.putconn.side_effect = psycopg2.InterfaceError("dead")
    conn_module._pool = mock_pool

    dead_conn = MagicMock()
    # Should not raise
    release_conn(dead_conn)
