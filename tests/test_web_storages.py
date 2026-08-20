from __future__ import annotations

import re

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from sluicery.config import Settings
from sluicery.db.models import (
    Playlist,
    PlaylistKindHint,
    PlaylistProfile,
    Profile,
    Storage,
    StorageKind,
)
from sluicery.storage.base import (
    ConnectionStage,
    ConnectionStageResult,
    ConnectionTestResult,
    StageStatus,
)
from sluicery.web.app import create_app
from sluicery.web.auth import ensure_initial_user


def _csrf(response) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _client(base_env, session_factory) -> TestClient:
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
    return client


def test_local_storage_crud_and_delete_never_touches_media(
    base_env, session_factory, env_data_dirs
) -> None:
    client = _client(base_env, session_factory)
    marker = env_data_dirs["MEDIA_ROOT"] / "keep-me.bin"
    marker.write_bytes(b"media")
    new_page = client.get("/storages/new")

    created = client.post(
        "/storages/new",
        data={
            "csrf_token": _csrf(new_page),
            "name": "Local archive",
            "kind": "local",
            "enabled": "yes",
            "path": "archive",
        },
        follow_redirects=False,
    )

    assert created.status_code == 303
    edit = client.get(created.headers["location"])
    assert "Local archive" in edit.text
    with session_factory() as db:
        storage = db.scalar(select(Storage))
        assert storage is not None
        storage_id = storage.id
        assert storage.config_json == {"path": "archive"}
    deleted = client.post(
        f"/storages/{storage_id}/delete",
        data={"csrf_token": _csrf(edit)},
        follow_redirects=False,
    )
    assert deleted.status_code == 303
    assert marker.read_bytes() == b"media"


def test_remote_password_is_encrypted_write_only_and_blank_update_preserves_it(
    base_env, session_factory, engine
) -> None:
    client = _client(base_env, session_factory)
    secret = "storage-secret-that-must-not-leak"
    new_page = client.get("/storages/new")
    form = {
        "csrf_token": _csrf(new_page),
        "name": "NAS",
        "kind": "remote",
        "enabled": "yes",
        "protocol": "smb",
        "host": "nas.internal",
        "share": "media",
        "path_remote": "video",
        "port": "445",
        "user": "operator",
        "domain": "WORKGROUP",
        "password": secret,
    }

    created = client.post("/storages/new", data=form, follow_redirects=False)

    assert created.status_code == 303
    edit = client.get(created.headers["location"])
    assert secret not in edit.text
    assert "operator" not in edit.text
    assert "WORKGROUP" not in edit.text
    assert 'value=""' in edit.text
    assert "設定済み" in edit.text
    with engine.connect() as connection:
        raw = connection.execute(text("SELECT credentials_encrypted FROM storage")).scalar_one()
    assert secret not in str(raw)
    with session_factory() as db:
        storage = db.scalar(select(Storage))
        assert storage is not None
        storage_id = storage.id
        assert storage.credentials_encrypted["password"] == secret

    form.update(
        csrf_token=_csrf(edit),
        name="NAS renamed",
        user="",
        domain="",
        password="",
    )
    updated = client.post(f"/storages/{storage_id}/edit", data=form, follow_redirects=False)
    assert updated.status_code == 303
    with session_factory() as db:
        storage = db.get(Storage, storage_id)
        assert storage is not None
        assert storage.credentials_encrypted["password"] == secret


def test_remote_credentials_are_not_echoed_after_validation_error(
    base_env, session_factory
) -> None:
    client = _client(base_env, session_factory)
    new_page = client.get("/storages/new")

    invalid = client.post(
        "/storages/new",
        data={
            "csrf_token": _csrf(new_page),
            "name": "Invalid NAS",
            "kind": "remote",
            "protocol": "smb",
            "host": "",
            "share": "media",
            "port": "445",
            "user": "credential-user-secret",
            "domain": "credential-domain-secret",
            "password": "credential-password-secret",
        },
    )

    assert invalid.status_code == 422
    assert "credential-user-secret" not in invalid.text
    assert "credential-domain-secret" not in invalid.text
    assert "credential-password-secret" not in invalid.text


def test_connection_test_persists_and_displays_four_stages(
    base_env, session_factory, monkeypatch
) -> None:
    class FakeAdapter:
        def test_connection(self) -> ConnectionTestResult:
            return ConnectionTestResult(
                tuple(
                    ConnectionStageResult(stage, StageStatus.SUCCESS, f"{stage.value} ok")
                    for stage in ConnectionStage
                )
            )

        def free_space(self) -> int:
            return 1024

    monkeypatch.setattr(
        "sluicery.web.app.create_storage_adapter", lambda storage, settings: FakeAdapter()
    )
    client = _client(base_env, session_factory)
    with session_factory() as db:
        storage = Storage(name="Test", kind=StorageKind.LOCAL, config_json={"path": "out"})
        db.add(storage)
        db.commit()
        storage_id = storage.id
    edit = client.get(f"/storages/{storage_id}/edit")

    tested = client.post(
        f"/storages/{storage_id}/test",
        data={"csrf_token": _csrf(edit)},
        follow_redirects=False,
    )

    assert tested.status_code == 303
    result_page = client.get(tested.headers["location"])
    for stage in ConnectionStage:
        assert stage.value in result_page.text
    assert "1.0 KiB" in result_page.text
    with session_factory() as db:
        storage = db.get(Storage, storage_id)
        assert storage is not None
        assert storage.last_check_at is not None
        assert len(storage.last_check_result_json["stages"]) == 4


def test_referenced_storage_cannot_be_deleted(base_env, session_factory) -> None:
    client = _client(base_env, session_factory)
    with session_factory() as db:
        storage = Storage(name="Used", kind=StorageKind.LOCAL, config_json={"path": "out"})
        profile = Profile(name="Profile", kind="video", layout_strategy="flat")
        playlist = Playlist(
            name="List",
            folder_name="list",
            url="https://example.com/list",
            kind_hint=PlaylistKindHint.VIDEO,
        )
        db.add_all([storage, profile, playlist])
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
        storage_id = storage.id
    edit = client.get(f"/storages/{storage_id}/edit")

    response = client.post(
        f"/storages/{storage_id}/delete",
        data={"csrf_token": _csrf(edit)},
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session_factory() as db:
        assert db.get(Storage, storage_id) is not None


def test_mount_kind_is_hidden_without_privileged_overlay(
    base_env, session_factory
) -> None:
    client = _client(base_env, session_factory)

    page = client.get("/storages/new")

    assert 'value="mount"' not in page.text


def test_mount_kind_is_selectable_only_when_runtime_capabilities_are_available(
    base_env, session_factory, monkeypatch
) -> None:
    monkeypatch.setattr("sluicery.web.app.mount_storage_available", lambda: True)
    client = _client(base_env, session_factory)
    page = client.get("/storages/new")
    assert 'value="mount"' in page.text
    response = client.post(
        "/storages/new",
        data={
            "csrf_token": _csrf(page),
            "name": "Kernel NFS",
            "kind": "mount",
            "enabled": "yes",
            "mount_protocol": "nfs",
            "mount_host": "nas.invalid",
            "mount_share": "/exports/media",
            "mount_path": "library",
            "mount_port": "2049",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with session_factory() as db:
        storage = db.scalar(select(Storage).where(Storage.name == "Kernel NFS"))
        assert storage is not None
        assert storage.kind == StorageKind.MOUNT
        assert storage.config_json == {
            "protocol": "nfs",
            "host": "nas.invalid",
            "share": "/exports/media",
            "path": "library",
            "port": 2049,
        }
        assert storage.credentials_encrypted is None
