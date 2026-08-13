from __future__ import annotations

import pytest

from sluicery.core import settings as core_settings


def test_default_returned_without_override(db_session) -> None:
    assert core_settings.get(db_session, "staging.warn_pct") == 80
    assert core_settings.is_overridden(db_session, "staging.warn_pct") is False


def test_override_returned_after_set(db_session) -> None:
    core_settings.set_override(db_session, "staging.warn_pct", 95)
    assert core_settings.get(db_session, "staging.warn_pct") == 95
    assert core_settings.is_overridden(db_session, "staging.warn_pct") is True


def test_default_returned_after_unset(db_session) -> None:
    core_settings.set_override(db_session, "staging.warn_pct", 95)
    core_settings.unset_override(db_session, "staging.warn_pct")
    assert core_settings.get(db_session, "staging.warn_pct") == 80
    assert core_settings.is_overridden(db_session, "staging.warn_pct") is False


def test_unknown_key_raises(db_session) -> None:
    with pytest.raises(core_settings.UnknownSettingKeyError):
        core_settings.get(db_session, "nonexistent.key")


def test_boolean_override_rejects_ambiguous_text(db_session) -> None:
    with pytest.raises(ValueError, match="true または false"):
        core_settings.set_override(db_session, "ytdlp.auto_install", "perhaps")


def test_typed_accessor_reflects_overrides(db_session) -> None:
    accessor = core_settings.OperationalSettings(db_session)
    assert accessor.download_retries == 5
    core_settings.set_override(db_session, "download.retries", 8)
    assert accessor.download_retries == 8


def test_list_all_reports_override_state(db_session) -> None:
    core_settings.set_override(db_session, "retry.max_attempts", 3)
    entries = {e.key: e for e in core_settings.list_all(db_session)}
    assert entries["retry.max_attempts"].is_override is True
    assert entries["retry.max_attempts"].value == 3
    assert entries["staging.warn_pct"].is_override is False
    assert entries["staging.warn_pct"].value == 80


def test_phase4_kind_defaults_and_list_override(db_session) -> None:
    assert (
        core_settings.get(db_session, "defaults.video.format_selector")
        == "bv*[height<=1080]+ba/b[height<=1080]/b"
    )
    assert core_settings.get(db_session, "defaults.music.audio_format") == "opus"
    core_settings.set_override(
        db_session,
        "defaults.music.parse_metadata",
        '["uploader:%(artist)s"]',
    )
    assert core_settings.get(db_session, "defaults.music.parse_metadata") == ["uploader:%(artist)s"]


def test_phase6_worker_defaults(db_session) -> None:
    accessor = core_settings.OperationalSettings(db_session)
    assert accessor.worker_poll_interval_sec == 3
    assert accessor.worker_poll_jitter_sec == 1
    assert accessor.worker_heartbeat_interval_sec == 30
    assert accessor.worker_stale_threshold_sec == 180
    assert accessor.worker_retry_base_sec == 60
    assert accessor.worker_retry_max_sec == 3600
    assert accessor.worker_max_attempts == 5
    assert accessor.worker_blocked_retry_sec == 300
    assert accessor.worker_blocked_retry_403_sec == 3600
    assert accessor.worker_progress_write_interval_sec == 2
    assert accessor.worker_progress_write_percent_step == 5
    assert accessor.worker_shutdown_grace_sec == 20
    assert accessor.worker_enable_test_tasks is False
