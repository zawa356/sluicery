from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select

from sluicery.config import Settings
from sluicery.db.models import Playlist, Storage
from sluicery.web.app import create_app
from sluicery.web.auth import ensure_initial_user


def _hidden(response, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}"\s+value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _client(base_env, session_factory) -> TestClient:
    settings = Settings()
    settings.ADMIN_PASSWORD = "correct-password"
    ensure_initial_user(session_factory, settings)
    client = TestClient(create_app(settings=settings, session_factory=session_factory))
    login = client.get("/login")
    client.post(
        "/login",
        data={
            "csrf_token": _hidden(login, "csrf_token"),
            "username": settings.ADMIN_USERNAME,
            "password": "correct-password",
        },
    )
    return client


def test_export_download_never_contains_storage_credentials_or_cookie(
    base_env, session_factory
) -> None:
    with session_factory() as db:
        storage = Storage(
            name="remote",
            kind="remote",
            config_json={"protocol": "smb", "host": "nas", "share": "media"},
            credentials_encrypted={"user": "secret-user", "password": "secret-password"},
        )
        playlist = Playlist(
            name="list",
            folder_name="list",
            url="https://example.com/list",
            kind_hint="video",
            cookie_enabled=True,
            cookies_encrypted={"netscape": "secret-cookie"},
        )
        db.add_all([storage, playlist])
        db.commit()
    client = _client(base_env, session_factory)

    response = client.get("/config-transfer/export")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/yaml")
    assert "sluicery-config.yaml" in response.headers["content-disposition"]
    assert "secret-user" not in response.text
    assert "secret-password" not in response.text
    assert "secret-cookie" not in response.text
    assert "requires_credentials: true" in response.text
    assert "requires_cookie_reentry: true" in response.text


def test_import_requires_preview_and_confirmation_then_applies_db_only(
    base_env, session_factory, tmp_path
) -> None:
    client = _client(base_env, session_factory)
    page = client.get("/config-transfer")
    csrf = _hidden(page, "csrf_token")
    yaml_body = b"""
version: 1
storages:
  - ref: storage-1
    name: imported-media
    kind: local
    enabled: true
    config:
      path: imported
profiles: []
playlists: []
playlist_profiles: []
settings:
  download.retries: 9
"""
    marker = tmp_path / "media-marker"
    marker.write_text("unchanged", encoding="utf-8")

    direct = client.post(
        "/config-transfer/apply",
        data={"csrf_token": csrf, "confirmed": "yes"},
    )
    assert direct.status_code == 422

    preview = client.post(
        "/config-transfer/preview",
        data={"csrf_token": csrf, "collision_mode": "skip"},
        files={"config_file": ("config.yaml", yaml_body, "application/yaml")},
    )
    assert preview.status_code == 200
    assert "新規 2 / 上書き 0 / スキップ 0" in preview.text
    token = _hidden(preview, "confirmation_token")

    unchecked = client.post(
        "/config-transfer/apply",
        data={
            "csrf_token": _hidden(preview, "csrf_token"),
            "confirmation_token": token,
        },
    )
    assert unchecked.status_code == 422
    applied = client.post(
        "/config-transfer/apply",
        data={
            "csrf_token": _hidden(preview, "csrf_token"),
            "confirmation_token": token,
            "confirmed": "yes",
        },
        follow_redirects=False,
    )
    assert applied.status_code == 303
    with session_factory() as db:
        storage = db.scalar(select(Storage).where(Storage.name == "imported-media"))
        assert storage is not None and storage.config_json == {"path": "imported"}
    assert marker.read_text(encoding="utf-8") == "unchanged"
