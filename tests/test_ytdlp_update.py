from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from sluicery.config import Settings
from sluicery.core import ytdlp_update
from sluicery.core.ytdlp_update import SmokeTestResult
from sluicery.db.models import (
    YtdlpRelease,
    YtdlpReleaseSource,
    YtdlpReleaseStatus,
)
from sluicery.downloader.errors import Classification
from sluicery.downloader.ytdlp import RunResult, TimeoutPolicy, YtdlpRunner


def _smoke(version: str, success: bool, reason: str = "ok") -> SmokeTestResult:
    return SmokeTestResult(
        success=success,
        version=version,
        reason_code=reason,
        checked_at="2026-08-16T00:00:00+00:00",
        duration_sec=1.0,
        version_ok=True,
        deno_detected=True,
        metadata_ok=True,
        download_ok=success,
        challenge_warning_absent=True,
        extras_available=True,
        metadata_embedded=success,
        thumbnail_advertised=False,
        thumbnail_embedded=None,
        cleanup_ok=True,
    )


def _releases(session_factory) -> tuple[YtdlpRelease, YtdlpRelease]:
    with session_factory() as db:
        old = YtdlpRelease(
            version="2026.01.01",
            source=YtdlpReleaseSource.INITIAL,
            status=YtdlpReleaseStatus.ACTIVE,
            activated_at=datetime.now(UTC) - timedelta(days=10),
            deactivated_at=datetime.now(UTC) - timedelta(days=1),
        )
        new = YtdlpRelease(
            version="2026.02.01",
            source=YtdlpReleaseSource.AUTO,
            status=YtdlpReleaseStatus.INSTALLED,
        )
        db.add_all([old, new])
        db.commit()
        return old, new


class _Hook:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


def test_failed_candidate_rolls_back_only_after_old_version_passes(
    base_env, session_factory, monkeypatch
) -> None:
    settings = Settings()
    _old, new = _releases(session_factory)
    switched: list[str] = []
    hook = _Hook()
    monkeypatch.setattr(ytdlp_update, "read_current_version", lambda _root: "2026.01.01")
    monkeypatch.setattr(ytdlp_update, "install", lambda *_args, **_kwargs: new)
    monkeypatch.setattr(
        ytdlp_update, "use", lambda _root, _session, version: switched.append(version)
    )
    monkeypatch.setattr(
        ytdlp_update,
        "_run_configured_smoke",
        lambda _settings, _factory, version: _smoke(
            version, version == "2026.01.01", "download_failed"
        ),
    )

    result = ytdlp_update.update_ytdlp(settings, session_factory, hook=hook)

    assert result.status == "rolled_back"
    assert result.active_version == "2026.01.01"
    assert switched == ["2026.02.01", "2026.01.01"]
    assert hook.events == [
        (
            "run_failed",
            {
                "component": "ytdlp_update",
                "version": "2026.02.01",
                "reason_code": "download_failed",
                "rolled_back": True,
            },
        )
    ]


def test_both_versions_failing_keeps_new_candidate_active(
    base_env, session_factory, monkeypatch
) -> None:
    settings = Settings()
    _old, new = _releases(session_factory)
    switched: list[str] = []
    monkeypatch.setattr(ytdlp_update, "read_current_version", lambda _root: "2026.01.01")
    monkeypatch.setattr(ytdlp_update, "install", lambda *_args, **_kwargs: new)
    monkeypatch.setattr(
        ytdlp_update, "use", lambda _root, _session, version: switched.append(version)
    )
    monkeypatch.setattr(
        ytdlp_update,
        "_run_configured_smoke",
        lambda _settings, _factory, version: _smoke(version, False, "metadata_failed"),
    )

    result = ytdlp_update.update_ytdlp(settings, session_factory, hook=_Hook())

    assert result.status == "failed"
    assert result.active_version == "2026.02.01"
    assert result.rollback_smoke is not None and not result.rollback_smoke.success
    assert switched == ["2026.02.01"]


def test_same_current_candidate_failure_checks_deactivated_previous(
    base_env, session_factory, monkeypatch
) -> None:
    settings = Settings()
    _old, new = _releases(session_factory)
    switched: list[str] = []
    monkeypatch.setattr(ytdlp_update, "read_current_version", lambda _root: new.version)
    monkeypatch.setattr(ytdlp_update, "install", lambda *_args, **_kwargs: new)
    monkeypatch.setattr(
        ytdlp_update, "use", lambda _root, _session, version: switched.append(version)
    )
    monkeypatch.setattr(
        ytdlp_update,
        "_run_configured_smoke",
        lambda _settings, _factory, version: _smoke(
            version, version == "2026.01.01", "download_failed"
        ),
    )

    result = ytdlp_update.update_ytdlp(settings, session_factory, hook=_Hook())

    assert result.status == "rolled_back"
    assert result.active_version == "2026.01.01"
    assert result.rollback_smoke is not None and result.rollback_smoke.success
    assert switched == ["2026.01.01"]


def test_media_metadata_requires_smoketest_marker(monkeypatch, tmp_path) -> None:
    media = tmp_path / "smoke.mov"
    media.write_bytes(b"media")

    def ffprobe_with(tags: dict[str, str]):
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"format": {"tags": tags}, "streams": []}),
        )

    monkeypatch.setattr(
        ytdlp_update.subprocess,
        "run",
        lambda *_args, **_kwargs: ffprobe_with({"major_brand": "qt"}),
    )
    assert ytdlp_update._inspect_media(media) == (False, False)

    monkeypatch.setattr(
        ytdlp_update.subprocess,
        "run",
        lambda *_args, **_kwargs: ffprobe_with(
            {"comment": ytdlp_update._SMOKE_METADATA_MARKER}
        ),
    )
    assert ytdlp_update._inspect_media(media) == (True, False)


def test_smoketest_downloads_inside_dedicated_staging_and_cleans_it(
    base_env, monkeypatch
) -> None:
    settings = Settings()
    version = "2026.02.01"
    venv = ytdlp_update.versions_dir(ytdlp_update.ytdlp_root(settings.DATA_DIR)) / version
    (venv / "bin").mkdir(parents=True)
    monkeypatch.setattr(ytdlp_update, "_probe_version", lambda *_args: True)
    monkeypatch.setattr(ytdlp_update, "_probe_extras", lambda *_args: True)
    monkeypatch.setattr(ytdlp_update, "_inspect_media", lambda _path: (True, False))
    monkeypatch.setattr(ytdlp_update, "_thumbnail_present", lambda *_args: False)
    monkeypatch.setattr(ytdlp_update, "_embed_synthetic_thumbnail", lambda *_args: True)
    calls = 0
    seen: list[tuple[list[str], bool]] = []

    def fake_run(
        self: YtdlpRunner,
        args: list[str],
        *,
        timeout: TimeoutPolicy,
        on_progress=None,
        cwd=None,
        sensitive_values: tuple[str, ...] = (),
        mask_all_urls: bool = False,
    ) -> RunResult:
        nonlocal calls
        calls += 1
        seen.append((args, mask_all_urls))
        log = settings.DATA_DIR / f"smoke-{calls}.log"
        log.write_text("[debug] JS runtimes: deno-2.9.5\n", encoding="utf-8")
        if calls == 1:
            return RunResult(
                returncode=0,
                classification=Classification.OK,
                reason_code="ok",
                stdout_lines=[json.dumps({"id": "cc-item"})],
                log_path=log,
            )
        home = next(value[5:] for value in args if value.startswith("home:"))
        media = Path(home) / "smoke.mov"
        media.write_bytes(b"media")
        return RunResult(
            returncode=0,
            classification=Classification.OK,
            reason_code="ok",
            result_metadata=[{"file_path": str(media)}],
            log_path=log,
        )

    monkeypatch.setattr(YtdlpRunner, "run", fake_run)
    result = ytdlp_update.run_smoketest(
        settings,
        version=version,
        source_url="https://example.com/cc",
        idle_timeout_sec=10,
        absolute_timeout_sec=20,
        term_grace_sec=1,
        stderr_tail_kb=1,
    )

    assert result.success
    assert result.deno_detected and result.download_ok
    assert result.metadata_embedded and result.thumbnail_embedded is True
    assert result.thumbnail_advertised is False
    assert result.cleanup_ok
    assert list(settings.STAGING_DIR.glob("ytdlp-smoketest-*")) == []
    assert all("--ignore-config" in args and masked for args, masked in seen)
    download_args = seen[1][0]
    marker_index = download_args.index("--parse-metadata") + 1
    assert ytdlp_update._SMOKE_METADATA_MARKER in download_args[marker_index]
