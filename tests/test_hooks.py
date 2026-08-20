from __future__ import annotations

import threading
import time
from pathlib import Path

from sqlalchemy import select

from sluicery.db.models import EventLog
from sluicery.hooks import (
    EVENT_TYPES,
    EventLogHook,
    emit_safely,
    flush_pending_hooks,
    sanitize_event_payload,
)


def test_hook_failure_does_not_escape() -> None:
    class BrokenHook:
        def emit(self, event_type: str, payload: dict) -> None:
            raise RuntimeError("broken")

    emit_safely(BrokenHook(), "target_downloaded", {"target_id": 1})


def test_event_log_hook_is_async_and_preserves_submission_order(
    session_factory, monkeypatch
) -> None:
    from sluicery.hooks import eventlog as eventlog_module

    original = eventlog_module.EventLogRepository.record
    release = threading.Event()
    started = threading.Event()

    def delayed(self, event_type: str, payload: dict):
        started.set()
        release.wait(2)
        return original(self, event_type, payload)

    monkeypatch.setattr(eventlog_module.EventLogRepository, "record", delayed)
    hook = EventLogHook(session_factory, enabled_events=EVENT_TYPES)
    before = time.monotonic()
    hook.emit("run_started", {"run_id": 1, "kind": "discover"})
    hook.emit("run_finished", {"run_id": 1, "kind": "discover", "status": "succeeded"})
    elapsed = time.monotonic() - before

    assert started.wait(1)
    assert elapsed < 0.2
    release.set()
    assert flush_pending_hooks()
    with session_factory() as session:
        assert [row.event_type for row in session.scalars(select(EventLog))] == [
            "run_started",
            "run_finished",
        ]


def test_config_subscription_filters_events(session_factory, tmp_path: Path) -> None:
    config = tmp_path / "hooks.yaml"
    config.write_text(
        """
version: 1
subscriptions:
  event_log:
    enabled: true
    events: [target_failed]
""",
        encoding="utf-8",
    )
    hook = EventLogHook(session_factory, config_path=config)

    hook.emit("run_started", {"run_id": 1})
    hook.emit("target_failed", {"target_id": 2, "task_id": 3, "reason_code": "failed"})

    assert flush_pending_hooks()
    with session_factory() as session:
        rows = list(session.scalars(select(EventLog)))
        assert [row.event_type for row in rows] == ["target_failed"]


def test_invalid_config_disables_hook_without_breaking_main(
    session_factory, tmp_path: Path
) -> None:
    config = tmp_path / "hooks.yaml"
    config.write_text("version: invalid\n", encoding="utf-8")

    hook = EventLogHook(session_factory, config_path=config)
    hook.emit("run_started", {"run_id": 1})

    assert flush_pending_hooks()
    with session_factory() as session:
        assert list(session.scalars(select(EventLog))) == []


def test_payload_whitelist_removes_secret_url_cookie_and_unknown_values() -> None:
    payload = sanitize_event_payload(
        "artifact_published",
        {
            "artifact_id": 1,
            "relative_path": "folder/media.mkv?token=secret-value",
            "source_url": "https://example.com/private",
            "cookie": "secret-cookie",
            "unknown": "not-allowed",
        },
    )

    assert payload == {"artifact_id": 1, "relative_path": "********"}
    serialized = str(payload)
    assert "secret-value" not in serialized
    assert "example.com" not in serialized
    assert "secret-cookie" not in serialized

    url_value = sanitize_event_payload(
        "artifact_missing", {"artifact_id": 2, "relative_path": "https://example.com/media"}
    )
    assert url_value == {"artifact_id": 2, "relative_path": "********"}
