"""
Tests for runtime_config.notify().

  I — Discord send succeeds
  J — Discord fails (logs warning, no raise)
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import logging
from unittest.mock import patch
import pytest

from runtime_config import notify


# ── I: Discord succeeds ───────────────────────────────────────────────────────

def test_notify_discord_succeeds():
    with patch("subprocess.run") as mock_run:
        notify("test subject", "test body")

    mock_run.assert_called_once()
    assert "openclaw" in mock_run.call_args.args[0]


# ── I variant: custom discord_body is used ────────────────────────────────────

def test_notify_custom_discord_body():
    with patch("subprocess.run") as mock_run:
        notify("subject", "body", discord_body="custom message")

    call_args = mock_run.call_args.args[0]
    assert "custom message" in call_args


# ── J: Discord fails — logs warning, no raise ────────────────────────────────

def test_notify_discord_fails_no_raise(caplog):
    with patch("subprocess.run", side_effect=FileNotFoundError("openclaw not found")), \
         caplog.at_level(logging.WARNING, logger="runtime_config"):
        notify("subject", "body")  # must not raise

    assert any("discord" in r.message.lower() for r in caplog.records)
