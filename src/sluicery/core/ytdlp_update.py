"""yt-dlp更新、強化スモークテスト、ロールバックの安全境界。"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from sqlalchemy.orm import Session, sessionmaker

from sluicery.config import Settings
from sluicery.core.options import validate_source_url
from sluicery.core.settings import OperationalSettings
from sluicery.db.models import YtdlpReleaseSource
from sluicery.db.repositories.ytdlp_release import YtdlpReleaseRepository
from sluicery.downloader.protocol import PRINT_PREFIX, RESULT_PREFIX
from sluicery.downloader.version import (
    YtdlpInstallError,
    install,
    prune_old_versions,
    read_current_version,
    use,
    versions_dir,
    ytdlp_root,
)
from sluicery.downloader.ytdlp import TimeoutPolicy, YtdlpRunner
from sluicery.hooks import EventLogHook, Hook, emit_safely

DEFAULT_SMOKETEST_URL = "https://download.blender.org/peach/trailer/trailer_1080p.mov"
_DENO_DETECTED_RE = re.compile(r"JS runtimes:.*\bdeno\b", re.IGNORECASE)
_CHALLENGE_WARNING = "n challenge solving failed"
_SMOKE_ABSOLUTE_MAX_SEC = 30 * 60
_SMOKE_METADATA_MARKER = "sluicery-smoketest-metadata-v1"


class YtdlpUpdateBusyError(RuntimeError):
    """別の更新・ロールバックが進行中。"""


class YtdlpRollbackError(RuntimeError):
    """ロールバック候補が無い、または候補のスモークテストが失敗した。"""


@dataclass(frozen=True)
class SmokeTestResult:
    success: bool
    version: str
    reason_code: str
    checked_at: str
    duration_sec: float
    version_ok: bool
    deno_detected: bool
    metadata_ok: bool
    download_ok: bool
    challenge_warning_absent: bool
    extras_available: bool
    metadata_embedded: bool
    thumbnail_advertised: bool
    thumbnail_embedded: bool | None
    cleanup_ok: bool

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class UpdateResult:
    status: str
    candidate_version: str | None
    active_version: str | None
    candidate_smoke: SmokeTestResult | None
    rollback_smoke: SmokeTestResult | None = None


def _subprocess_env() -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LC_ALL": "C",
    }


def _probe_version(binary: Path, expected: str) -> bool:
    try:
        result = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            env=_subprocess_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and result.stdout.strip() == expected


def _probe_extras(venv_dir: Path) -> bool:
    try:
        result = subprocess.run(
            [
                str(venv_dir / "bin" / "python"),
                "-c",
                "import mutagen; import yt_dlp_ejs",
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            env=_subprocess_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _read_log(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _inspect_media(path: Path) -> tuple[bool, bool]:
    try:
        result = subprocess.run(
            [
                "/usr/local/bin/ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format_tags:stream_disposition",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=_subprocess_env(),
            check=False,
        )
        raw = json.loads(result.stdout) if result.returncode == 0 else {}
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return False, False
    if not isinstance(raw, dict):
        return False, False
    format_data = raw.get("format")
    tags = format_data.get("tags") if isinstance(format_data, dict) else None
    metadata_embedded = isinstance(tags, dict) and any(
        value == _SMOKE_METADATA_MARKER for value in tags.values()
    )
    streams = raw.get("streams")
    attached = False
    if isinstance(streams, list):
        for stream in streams:
            disposition = stream.get("disposition") if isinstance(stream, dict) else None
            if isinstance(disposition, dict) and disposition.get("attached_pic") == 1:
                attached = True
    return metadata_embedded, attached


def _thumbnail_present(venv_dir: Path, path: Path) -> bool:
    """yt-dlp venvのmutagenで埋込みcoverを確認する（ffprobeで見えないMP4 covr用）。"""
    script = """
import sys
from mutagen import File

media = File(sys.argv[1])
tags = getattr(media, "tags", None) or {}
keys = {str(key).lower() for key in tags.keys()}
if not any(key == "covr" or "apic" in key or "picture" in key for key in keys):
    raise SystemExit(1)
"""
    try:
        result = subprocess.run(
            [str(venv_dir / "bin" / "python"), "-c", script, str(path)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            env=_subprocess_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _embed_synthetic_thumbnail(venv_dir: Path, work_dir: Path) -> bool:
    """配信元に画像が無い場合もyt-dlp自身のthumbnail PPを実ファイルで検証する。"""
    thumbnail = work_dir / "smoke-thumbnail.png"
    media_path = work_dir / "smoke-thumbnail-target.m4a"
    try:
        media_generated = subprocess.run(
            [
                "/usr/local/bin/ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "anullsrc=r=8000:cl=mono",
                "-t",
                "0.2",
                "-c:a",
                "aac",
                "-y",
                str(media_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            env=_subprocess_env(),
            check=False,
        )
        thumbnail_generated = subprocess.run(
            [
                "/usr/local/bin/ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "color=c=blue:s=32x32:d=0.1",
                "-frames:v",
                "1",
                "-y",
                str(thumbnail),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            env=_subprocess_env(),
            check=False,
        )
        if (
            media_generated.returncode != 0
            or thumbnail_generated.returncode != 0
            or not media_path.is_file()
            or not thumbnail.is_file()
        ):
            return False
        script = """
import sys
from pathlib import Path
from yt_dlp import YoutubeDL
from yt_dlp.postprocessor.embedthumbnail import EmbedThumbnailPP

media, thumbnail = sys.argv[1:3]
with YoutubeDL({"quiet": True, "no_warnings": True, "ffmpeg_location": "/usr/local/bin"}) as ydl:
    EmbedThumbnailPP(ydl, already_have_thumbnail=True).run({
        "filepath": media,
        "ext": Path(media).suffix.lstrip(".").lower(),
        "vcodec": "unknown",
        "acodec": "unknown",
        "thumbnails": [{"filepath": thumbnail}],
    })
"""
        embedded = subprocess.run(
            [
                str(venv_dir / "bin" / "python"),
                "-c",
                script,
                str(media_path),
                str(thumbnail),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=60,
            env=_subprocess_env(),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return embedded.returncode == 0 and _thumbnail_present(venv_dir, media_path)


def _safe_media_path(work_dir: Path, value: object) -> Path | None:
    if not isinstance(value, str):
        return None
    candidate = Path(value).resolve()
    try:
        candidate.relative_to(work_dir.resolve())
    except ValueError:
        return None
    return candidate if candidate.is_file() else None


def run_smoketest(
    settings: Settings,
    *,
    version: str,
    source_url: str,
    idle_timeout_sec: int,
    absolute_timeout_sec: int,
    term_grace_sec: int,
    stderr_tail_kb: int,
) -> SmokeTestResult:
    """指定versionを実ダウンロードまで検査し、専用Stagingを必ず片付ける。"""
    started = time.monotonic()
    checked_at = datetime.now(UTC).isoformat()
    root = ytdlp_root(settings.DATA_DIR)
    venv_dir = versions_dir(root) / version
    binary = venv_dir / "bin" / "yt-dlp"
    source_url = validate_source_url(source_url)
    assert settings.STAGING_DIR is not None
    work_dir = settings.STAGING_DIR / f"ytdlp-smoketest-{uuid4().hex}"
    work_dir.mkdir(mode=0o700, parents=False, exist_ok=False)
    version_ok = _probe_version(binary, version)
    extras_available = _probe_extras(venv_dir)
    deno_detected = False
    metadata_ok = False
    download_ok = False
    challenge_absent = True
    metadata_embedded = False
    thumbnail_advertised = False
    thumbnail_embedded: bool | None = None
    reason_code = "ok"
    cleanup_ok = False
    try:
        runner = YtdlpRunner(
            binary,
            stderr_tail_kb=stderr_tail_kb,
            log_dir=settings.DATA_DIR / "logs",
        )
        probe = runner.run(
            [
                "--ignore-config",
                "--verbose",
                "--simulate",
                "--skip-download",
                "--no-playlist",
                "--print",
                f"{PRINT_PREFIX}%()j",
                "--",
                source_url,
            ],
            timeout=TimeoutPolicy(
                idle_sec=idle_timeout_sec,
                absolute_sec=min(absolute_timeout_sec, _SMOKE_ABSOLUTE_MAX_SEC),
                term_grace_sec=term_grace_sec,
            ),
            sensitive_values=(source_url,),
            mask_all_urls=True,
        )
        probe_log = _read_log(probe.log_path)
        deno_detected = bool(_DENO_DETECTED_RE.search(probe_log))
        challenge_absent = _CHALLENGE_WARNING not in probe_log.lower()
        if probe.returncode == 0 and probe.stdout_lines:
            try:
                parsed = json.loads(probe.stdout_lines[0])
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                metadata_ok = bool(parsed.get("id") or parsed.get("title"))
                thumbnail_advertised = bool(parsed.get("thumbnail") or parsed.get("thumbnails"))

        download = runner.run(
            [
                "--ignore-config",
                "--no-playlist",
                "--embed-metadata",
                "--embed-thumbnail",
                "--write-thumbnail",
                "--convert-thumbnails",
                "jpg",
                "--parse-metadata",
                f"{_SMOKE_METADATA_MARKER}:meta_comment",
                "--paths",
                f"home:{work_dir}",
                "--paths",
                f"temp:{work_dir / '.tmp'}",
                "--output",
                "smoke.%(ext)s",
                "--print",
                f'after_move:{RESULT_PREFIX}{{"file_path": %(filepath)j}}',
                "--",
                source_url,
            ],
            timeout=TimeoutPolicy(
                idle_sec=idle_timeout_sec,
                absolute_sec=min(absolute_timeout_sec, _SMOKE_ABSOLUTE_MAX_SEC),
                term_grace_sec=term_grace_sec,
            ),
            sensitive_values=(source_url,),
            mask_all_urls=True,
        )
        download_log = _read_log(download.log_path)
        challenge_absent = challenge_absent and (
            _CHALLENGE_WARNING not in download_log.lower()
        )
        media_path = None
        if download.returncode == 0 and download.result_metadata:
            media_path = _safe_media_path(
                work_dir, download.result_metadata[-1].get("file_path")
            )
        download_ok = media_path is not None and media_path.stat().st_size > 0
        if media_path is not None:
            metadata_embedded, attached = _inspect_media(media_path)
            thumbnail_embedded = attached or _thumbnail_present(venv_dir, media_path)
            if not thumbnail_advertised and not thumbnail_embedded:
                thumbnail_embedded = _embed_synthetic_thumbnail(
                    venv_dir, work_dir
                )

        checks = {
            "version_failed": version_ok,
            "deno_not_detected": deno_detected,
            "metadata_failed": metadata_ok,
            "download_failed": download_ok,
            "challenge_warning": challenge_absent,
            "extras_missing": extras_available,
            "metadata_not_embedded": metadata_embedded,
            "thumbnail_not_embedded": thumbnail_embedded is True,
        }
        reason_code = next((reason for reason, passed in checks.items() if not passed), "ok")
    except Exception:  # noqa: BLE001 - 外部CLI境界は安全なreasonだけを返す
        reason_code = "smoketest_exception"
    finally:
        try:
            shutil.rmtree(work_dir)
            cleanup_ok = not work_dir.exists()
        except OSError:
            cleanup_ok = False
    if not cleanup_ok and reason_code == "ok":
        reason_code = "cleanup_failed"
    return SmokeTestResult(
        success=reason_code == "ok",
        version=version,
        reason_code=reason_code,
        checked_at=checked_at,
        duration_sec=round(time.monotonic() - started, 3),
        version_ok=version_ok,
        deno_detected=deno_detected,
        metadata_ok=metadata_ok,
        download_ok=download_ok,
        challenge_warning_absent=challenge_absent,
        extras_available=extras_available,
        metadata_embedded=metadata_embedded,
        thumbnail_advertised=thumbnail_advertised,
        thumbnail_embedded=thumbnail_embedded,
        cleanup_ok=cleanup_ok,
    )


@contextmanager
def _update_lock(root: Path) -> Iterator[None]:
    root.mkdir(parents=True, exist_ok=True)
    with (root / ".update-lock").open("w") as lock_file:
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise YtdlpUpdateBusyError("yt-dlpの更新処理が既に実行中です") from exc
        try:
            yield
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def _smoke_parameters(
    session_factory: sessionmaker[Session],
) -> tuple[str, int, int, int, int, int]:
    with session_factory() as session:
        operational = OperationalSettings(session)
        return (
            operational.ytdlp_smoketest_url,
            operational.ytdlp_idle_timeout_sec,
            operational.ytdlp_absolute_timeout_sec,
            operational.ytdlp_term_grace_sec,
            operational.ytdlp_stderr_tail_kb,
            operational.ytdlp_keep_versions,
        )


def _run_configured_smoke(
    settings: Settings,
    session_factory: sessionmaker[Session],
    version: str,
) -> SmokeTestResult:
    source_url, idle, absolute, grace, stderr_kb, _keep = _smoke_parameters(session_factory)
    return run_smoketest(
        settings,
        version=version,
        source_url=source_url,
        idle_timeout_sec=idle,
        absolute_timeout_sec=absolute,
        term_grace_sec=grace,
        stderr_tail_kb=stderr_kb,
    )


def _record_smoke(
    session_factory: sessionmaker[Session], result: SmokeTestResult
) -> None:
    with session_factory() as session:
        release = YtdlpReleaseRepository(session).get_by_version(result.version)
        if release is not None:
            release.smoketest_result_json = result.to_dict()
            session.commit()


def _failure_event(
    hook: Hook,
    *,
    version: str | None,
    reason_code: str,
    rolled_back: bool,
) -> None:
    emit_safely(
        hook,
        "run_failed",
        {
            "component": "ytdlp_update",
            "version": version,
            "reason_code": reason_code,
            "rolled_back": rolled_back,
        },
    )


def _latest_rollback_version(
    session_factory: sessionmaker[Session], *, exclude: str | None
) -> str | None:
    with session_factory() as session:
        candidates = [
            release
            for release in YtdlpReleaseRepository(session).list_installed()
            if release.version != exclude and release.deactivated_at is not None
        ]
        if not candidates:
            return None
        return max(
            candidates,
            key=lambda release: release.deactivated_at
            or datetime.min.replace(tzinfo=UTC),
        ).version


def update_ytdlp(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    source: YtdlpReleaseSource = YtdlpReleaseSource.AUTO,
    hook: Hook | None = None,
) -> UpdateResult:
    """最新版を有効化して検査し、失敗時だけ健全な直前版へ戻す。"""
    root = ytdlp_root(settings.DATA_DIR)
    event_hook = hook or EventLogHook(session_factory)
    with _update_lock(root):
        previous = read_current_version(root)
        try:
            with session_factory() as session:
                candidate = install(root, session, source=source)
                candidate_version = candidate.version
                if previous != candidate_version:
                    use(root, session, candidate_version)
        except YtdlpInstallError:
            _failure_event(
                event_hook,
                version=None,
                reason_code="install_failed",
                rolled_back=False,
            )
            return UpdateResult("install_failed", None, previous, None)

        candidate_smoke = _run_configured_smoke(
            settings, session_factory, candidate_version
        )
        _record_smoke(session_factory, candidate_smoke)
        if candidate_smoke.success:
            _url, _idle, _absolute, _grace, _stderr, keep = _smoke_parameters(
                session_factory
            )
            with session_factory() as session:
                prune_old_versions(root, session, keep)
            status = "no_change" if previous == candidate_version else "updated"
            if status == "updated":
                emit_safely(
                    event_hook,
                    "ytdlp_updated",
                    {
                        "version": candidate_version,
                        "previous_version": previous,
                    },
                )
            return UpdateResult(status, candidate_version, candidate_version, candidate_smoke)

        rollback_smoke = None
        rolled_back = False
        active = candidate_version
        rollback_version = (
            previous
            if previous is not None and previous != candidate_version
            else _latest_rollback_version(session_factory, exclude=candidate_version)
        )
        if rollback_version is not None:
            rollback_smoke = _run_configured_smoke(
                settings, session_factory, rollback_version
            )
            _record_smoke(session_factory, rollback_smoke)
            if rollback_smoke.success:
                with session_factory() as session:
                    use(root, session, rollback_version)
                active = rollback_version
                rolled_back = True
                emit_safely(
                    event_hook,
                    "ytdlp_rollback",
                    {
                        "version": rollback_version,
                        "previous_version": candidate_version,
                    },
                )
        _failure_event(
            event_hook,
            version=candidate_version,
            reason_code=candidate_smoke.reason_code,
            rolled_back=rolled_back,
        )
        return UpdateResult(
            "rolled_back" if rolled_back else "failed",
            candidate_version,
            active,
            candidate_smoke,
            rollback_smoke,
        )


def rollback_ytdlp(
    settings: Settings,
    session_factory: sessionmaker[Session],
    *,
    hook: Hook | None = None,
) -> UpdateResult:
    """直前に有効だった版を先に検査し、健全な場合だけ切り替える。"""
    root = ytdlp_root(settings.DATA_DIR)
    event_hook = hook or EventLogHook(session_factory)
    with _update_lock(root):
        current = read_current_version(root)
        previous = _latest_rollback_version(session_factory, exclude=current)
        if previous is None:
            raise YtdlpRollbackError("ロールバックできる直前バージョンがありません")
        smoke = _run_configured_smoke(settings, session_factory, previous)
        _record_smoke(session_factory, smoke)
        if not smoke.success:
            _failure_event(
                event_hook,
                version=previous,
                reason_code=smoke.reason_code,
                rolled_back=False,
            )
            raise YtdlpRollbackError(
                f"直前バージョンの検証に失敗しました（理由: {smoke.reason_code}）"
            )
        with session_factory() as session:
            use(root, session, previous)
        emit_safely(
            event_hook,
            "ytdlp_rollback",
            {"version": previous, "previous_version": current},
        )
        return UpdateResult("rolled_back", previous, previous, smoke)


__all__ = [
    "DEFAULT_SMOKETEST_URL",
    "SmokeTestResult",
    "UpdateResult",
    "YtdlpRollbackError",
    "YtdlpUpdateBusyError",
    "rollback_ytdlp",
    "run_smoketest",
    "update_ytdlp",
]
