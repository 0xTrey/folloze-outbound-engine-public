import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

import runtime_config  # noqa: E402


def test_should_skip_for_pause_blocks_scheduled_outbound_jobs(tmp_path, monkeypatch):
    pause_path = tmp_path / "engine-paused.json"
    pause_path.write_text(
        '{"paused": true, "reason": "signal engine rebuild", "updated_at": "2026-04-03T11:30:00-05:00"}'
    )
    monkeypatch.setattr(runtime_config, "ENGINE_PAUSE_STATE_PATH", pause_path)
    monkeypatch.delenv("FOLLOZE_ALLOW_WHEN_PAUSED", raising=False)

    skip, message = runtime_config.should_skip_for_pause("assign_push")

    assert skip is True
    assert "signal engine rebuild" in message
    assert "FOLLOZE_ALLOW_WHEN_PAUSED=1" in message


def test_should_skip_for_pause_allows_nonblocked_jobs(tmp_path, monkeypatch):
    pause_path = tmp_path / "engine-paused.json"
    pause_path.write_text('{"paused": true, "reason": "signal engine rebuild"}')
    monkeypatch.setattr(runtime_config, "ENGINE_PAUSE_STATE_PATH", pause_path)

    skip, message = runtime_config.should_skip_for_pause("derive_contact_signal_state")

    assert skip is False
    assert message == ""


def test_should_skip_for_pause_respects_override(tmp_path, monkeypatch):
    pause_path = tmp_path / "engine-paused.json"
    pause_path.write_text('{"paused": true, "reason": "signal engine rebuild"}')
    monkeypatch.setattr(runtime_config, "ENGINE_PAUSE_STATE_PATH", pause_path)
    monkeypatch.setenv("FOLLOZE_ALLOW_WHEN_PAUSED", "1")

    skip, message = runtime_config.should_skip_for_pause("pull_events")

    assert skip is False
    assert message == ""
