from __future__ import annotations

import re
from pathlib import Path

from fastapi.testclient import TestClient

from sluicery.config import Settings
from sluicery.core.retention import RetentionExecutionResult, RetentionSafetyError
from sluicery.db.models import (
    Artifact,
    ArtifactRole,
    Item,
    LayoutStrategy,
    Playlist,
    PlaylistKindHint,
    PlaylistProfile,
    Profile,
    ProfileKind,
    Storage,
    StorageKind,
    Target,
    TargetStatus,
)
from sluicery.storage.base import RemoteFile
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


def _graph(session_factory) -> tuple[int, list[int]]:
    with session_factory() as db:
        storage = Storage(
            name="media", kind=StorageKind.LOCAL, config_json={"path": "library"}
        )
        profile = Profile(
            name="video", kind=ProfileKind.VIDEO, layout_strategy=LayoutStrategy.FLAT
        )
        playlist = Playlist(
            name="Retention List",
            folder_name="retention-list",
            url="https://example.com/list",
            kind_hint=PlaylistKindHint.VIDEO,
        )
        db.add_all([storage, profile, playlist])
        db.flush()
        assignment = PlaylistProfile(
            playlist_id=playlist.id,
            profile_id=profile.id,
            storage_id=storage.id,
        )
        db.add(assignment)
        db.flush()
        artifact_ids = []
        for index in range(3):
            item = Item(
                playlist_id=playlist.id,
                source_id=f"source-{index}",
                source_url=f"https://example.com/source-{index}",
                upload_date=f"20260{index + 1}01",
            )
            db.add(item)
            db.flush()
            target = Target(
                item_id=item.id,
                playlist_profile_id=assignment.id,
                status=TargetStatus.DOWNLOADED,
            )
            db.add(target)
            db.flush()
            artifact = Artifact(
                target_id=target.id,
                role=ArtifactRole.SOURCE,
                storage_id=storage.id,
                relative_path=f"retention/source-{index}.mkv",
                filesize=100 + index,
            )
            db.add(artifact)
            db.flush()
            artifact_ids.append(artifact.id)
        db.commit()
        return playlist.id, artifact_ids


def test_retention_enable_and_execute_both_require_dryrun_confirmation(
    base_env, session_factory, monkeypatch, tmp_path
) -> None:
    client = _client(base_env, session_factory)
    playlist_id, artifact_ids = _graph(session_factory)
    page = client.get(f"/playlists/{playlist_id}/retention")
    assert page.status_code == 200
    assert "無効（既定）" in page.text

    direct = client.post(
        f"/playlists/{playlist_id}/retention/save",
        data={"csrf_token": _hidden(page, "csrf_token"), "confirmed": "yes"},
    )
    assert direct.status_code == 422
    with session_factory() as db:
        assert db.get(Playlist, playlist_id).retention_policy_json is None

    preview = client.post(
        f"/playlists/{playlist_id}/retention/preview",
        data={
            "csrf_token": _hidden(page, "csrf_token"),
            "purpose": "enable",
            "enabled": "true",
            "keep_latest": "2",
            "max_age_days": "",
        },
    )
    assert preview.status_code == 200
    assert "候補 1 / 全Artifact 3件" in preview.text
    enable_token = _hidden(preview, "confirmation_token")

    unchecked = client.post(
        f"/playlists/{playlist_id}/retention/save",
        data={
            "csrf_token": _hidden(preview, "csrf_token"),
            "confirmation_token": enable_token,
        },
    )
    assert unchecked.status_code == 422
    save = client.post(
        f"/playlists/{playlist_id}/retention/save",
        data={
            "csrf_token": _hidden(preview, "csrf_token"),
            "confirmation_token": enable_token,
            "confirmed": "yes",
        },
        follow_redirects=False,
    )
    assert save.status_code == 303
    with session_factory() as db:
        assert db.get(Playlist, playlist_id).retention_policy_json == {
            "enabled": True,
            "keep_latest": 2,
            "max_age_days": None,
        }

    called = []

    def fake_execute(*_args, **_kwargs) -> RetentionExecutionResult:
        called.append(True)
        return RetentionExecutionResult(99, 1, 100, Path(tmp_path / "audit.log"))

    class _IdentityAdapter:
        def inspect_file(self, rel: str) -> RemoteFile:
            index = int(Path(rel).stem.removeprefix("source-"))
            return RemoteFile(rel, 100 + index, "1", {"sha256": f"{index:064x}"})

    monkeypatch.setattr("sluicery.web.app.execute_retention", fake_execute)
    monkeypatch.setattr(
        "sluicery.web.app.create_storage_adapter",
        lambda _storage, _settings: _IdentityAdapter(),
    )
    enabled_page = client.get(f"/playlists/{playlist_id}/retention")
    direct_execute = client.post(
        f"/playlists/{playlist_id}/retention/execute",
        data={
            "csrf_token": _hidden(enabled_page, "csrf_token"),
            "confirmed": "yes",
        },
    )
    assert direct_execute.status_code == 422
    assert called == []

    run_preview = client.post(
        f"/playlists/{playlist_id}/retention/preview",
        data={
            "csrf_token": _hidden(enabled_page, "csrf_token"),
            "purpose": "execute",
        },
    )
    execute_token = _hidden(run_preview, "confirmation_token")
    execute = client.post(
        f"/playlists/{playlist_id}/retention/execute",
        data={
            "csrf_token": _hidden(run_preview, "csrf_token"),
            "confirmation_token": execute_token,
            "confirmed": "yes",
        },
        follow_redirects=False,
    )
    assert execute.status_code == 303
    assert called == [True]
    with session_factory() as db:
        assert all(db.get(Artifact, artifact_id) is not None for artifact_id in artifact_ids)


def test_execute_preview_reports_unfinished_intent_as_422(
    base_env, session_factory, monkeypatch
) -> None:
    client = _client(base_env, session_factory)
    playlist_id, _artifact_ids = _graph(session_factory)
    with session_factory() as db:
        playlist = db.get(Playlist, playlist_id)
        assert playlist is not None
        playlist.retention_policy_json = {
            "enabled": True,
            "keep_latest": 2,
            "max_age_days": None,
        }
        db.commit()

    def refuse_unfinished(_data_dir: Path) -> None:
        raise RetentionSafetyError("未完了のretention削除意図があります")

    monkeypatch.setattr(
        "sluicery.web.app.assert_no_unfinished_retention_intents",
        refuse_unfinished,
    )
    page = client.get(f"/playlists/{playlist_id}/retention")
    response = client.post(
        f"/playlists/{playlist_id}/retention/preview",
        data={
            "csrf_token": _hidden(page, "csrf_token"),
            "purpose": "execute",
        },
    )

    assert response.status_code == 422
    assert "未完了のretention削除意図があります" in response.text
