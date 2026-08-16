from __future__ import annotations

import json
import re
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from sluicery.config import Settings
from sluicery.db.models import (
    Playlist,
    PlaylistKindHint,
    PlaylistProfile,
    Profile,
    Storage,
    StorageKind,
)
from sluicery.downloader.errors import Classification
from sluicery.downloader.version import InstallStatus, StatusResult
from sluicery.downloader.ytdlp import RunResult, TimeoutPolicy, YtdlpRunner
from sluicery.web.app import create_app
from sluicery.web.auth import ensure_initial_user


def _csrf(response) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _client(base_env, session_factory) -> tuple[TestClient, Settings]:
    settings = Settings()
    settings.ADMIN_PASSWORD = "correct-password"
    ensure_initial_user(session_factory, settings)
    client = TestClient(create_app(settings=settings, session_factory=session_factory))
    csrf = _csrf(client.get("/login"))
    client.post(
        "/login",
        data={
            "csrf_token": csrf,
            "username": settings.ADMIN_USERNAME,
            "password": "correct-password",
        },
    )
    return client, settings


def _profile_form(csrf: str, **overrides: str) -> dict[str, str]:
    values = {
        "csrf_token": csrf,
        "name": "Web Video",
        "description": "test",
        "kind": "video",
        "layout_strategy": "flat",
        "format_selector": "",
        "container": "mkv",
        "audio_format": "",
        "audio_quality": "",
        "subtitle_langs": "ja,en",
        "concurrent_fragments": "",
        "output_template": "",
        "ytdlp_args": "",
        "audio_extract": "inherit",
        "embed_metadata": "true",
        "embed_thumbnail": "false",
        "embed_chapters": "inherit",
        "subtitle_auto": "false",
        "subtitle_embed": "true",
    }
    values.update(overrides)
    return values


def test_profile_crud_preserves_explicit_tristates(base_env, session_factory) -> None:
    client, _settings = _client(base_env, session_factory)
    new_page = client.get("/profiles/new")

    created = client.post(
        "/profiles/new",
        data=_profile_form(_csrf(new_page)),
        follow_redirects=False,
    )

    assert created.status_code == 303
    edit = client.get(created.headers["location"])
    assert edit.status_code == 200
    assert "継承" in edit.text and "有効" in edit.text and "無効" in edit.text
    with session_factory() as db:
        profile = db.scalar(select(Profile))
        assert profile is not None
        assert profile.audio_extract is None
        assert profile.embed_metadata is True
        assert profile.embed_thumbnail is False
        assert profile.subtitle_auto is False
        profile_id = profile.id

    updated = client.post(
        f"/profiles/{profile_id}/edit",
        data=_profile_form(_csrf(edit), name="Renamed", audio_extract="true"),
        follow_redirects=False,
    )
    assert updated.status_code == 303
    with session_factory() as db:
        profile = db.get(Profile, profile_id)
        assert profile is not None
        assert profile.name == "Renamed"
        assert profile.audio_extract is True


def test_profile_preview_shows_origins_and_masks_secret_url(base_env, session_factory) -> None:
    client, _settings = _client(base_env, session_factory)
    with session_factory() as db:
        profile = Profile(
            name="Preview",
            kind="video",
            layout_strategy="flat",
            embed_metadata=None,
        )
        playlist = Playlist(
            name="Preview List",
            folder_name="preview",
            url="https://example.com/list?token=secret-preview-token",
            kind_hint=PlaylistKindHint.VIDEO,
        )
        storage = Storage(
            name="Local",
            kind=StorageKind.LOCAL,
            config_json={"path": "out"},
        )
        db.add_all([profile, playlist, storage])
        db.flush()
        db.add(
            PlaylistProfile(
                playlist_id=playlist.id,
                profile_id=profile.id,
                storage_id=storage.id,
                subpath="preview",
            )
        )
        db.commit()
        profile_id = profile.id

    page = client.get(f"/profiles/{profile_id}/edit")

    assert page.status_code == 200
    assert "コマンドラインプレビュー" in page.text
    assert "Preview List" in page.text
    assert "L1" in page.text and "L2" in page.text
    assert "secret-preview-token" not in page.text
    assert "token=********" in page.text
    assert "フォーマット検査" in page.text
    assert "検査する" in page.text


def test_profile_format_probe_shows_formats_and_rate_limits(
    base_env, session_factory, monkeypatch
) -> None:
    client, _settings = _client(base_env, session_factory)
    with session_factory() as db:
        profile = Profile(
            name="Probe",
            kind="video",
            layout_strategy="flat",
            format_selector="137+140",
        )
        db.add(profile)
        db.commit()
        profile_id = profile.id

    monkeypatch.setattr(
        "sluicery.web.app.get_status",
        lambda _root: StatusResult(InstallStatus.READY, "test", "test"),
    )
    monkeypatch.setattr(
        "sluicery.web.app.current_ytdlp_bin", lambda _root: Path("/fake/yt-dlp")
    )
    calls: list[tuple[list[str], tuple[str, ...], TimeoutPolicy, bool]] = []

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
        calls.append((args, sensitive_values, timeout, mask_all_urls))
        return RunResult(
            returncode=0,
            classification=Classification.OK,
            reason_code="ok",
            stdout_lines=[
                json.dumps(
                    {
                        "format_id": "137+140",
                        "formats": [
                            {
                                "format_id": "137",
                                "ext": "mp4",
                                "resolution": "1920x1080",
                                "filesize": 1000,
                            },
                            {
                                "format_id": "140",
                                "ext": "m4a",
                                "filesize_approx": 250,
                            },
                        ],
                        "requested_formats": [
                            {"format_id": "137", "filesize": 1000},
                            {"format_id": "140", "filesize_approx": 250},
                        ],
                    }
                )
            ],
        )

    monkeypatch.setattr(YtdlpRunner, "run", fake_run)
    edit = client.get(f"/profiles/{profile_id}/edit")
    source_url = "https://example.com/item?token=probe-secret"
    response = client.post(
        f"/profiles/{profile_id}/formats",
        data={"csrf_token": _csrf(edit), "source_url": source_url},
    )

    assert response.status_code == 200
    assert "137 + 140" in response.text
    assert "1.2 KiB" in response.text
    assert "1920x1080" in response.text
    assert source_url not in response.text
    assert calls[0][1] == (source_url,)
    assert calls[0][2].absolute_sec == 300
    assert calls[0][3] is True
    assert "--ignore-config" in calls[0][0]
    assert calls[0][0][-2:] == ["--", source_url]

    limited = client.post(
        f"/profiles/{profile_id}/formats",
        data={"csrf_token": _csrf(response), "source_url": "https://example.com/other"},
    )
    assert limited.status_code == 429
    assert "連続実行はできません" in limited.text
    assert len(calls) == 1


def test_referenced_profile_cannot_be_deleted(base_env, session_factory) -> None:
    client, _settings = _client(base_env, session_factory)
    with session_factory() as db:
        profile = Profile(name="Used", kind="video", layout_strategy="flat")
        playlist = Playlist(
            name="List",
            folder_name="list",
            url="https://example.com/list",
            kind_hint=PlaylistKindHint.VIDEO,
        )
        storage = Storage(name="Local", kind=StorageKind.LOCAL, config_json={"path": "out"})
        db.add_all([profile, playlist, storage])
        db.flush()
        db.add(
            PlaylistProfile(
                playlist_id=playlist.id,
                profile_id=profile.id,
                storage_id=storage.id,
                subpath="list",
            )
        )
        db.commit()
        profile_id = profile.id
    csrf = _csrf(client.get(f"/profiles/{profile_id}/edit"))

    response = client.post(
        f"/profiles/{profile_id}/delete",
        data={"csrf_token": csrf},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session_factory() as db:
        assert db.get(Profile, profile_id) is not None
