from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from sluicery.config import Settings
from sluicery.core.folder_move import (
    FolderMoveConfirmationSigner,
    FolderMoveExecutionError,
    build_folder_move_plan,
    execute_folder_move,
)
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
    Run,
    RunStatus,
    Storage,
    StorageKind,
    Target,
    TargetStatus,
)
from sluicery.storage.local import LocalStorageAdapter
from sluicery.web.app import create_app
from sluicery.web.auth import ensure_initial_user


class _RecordingHook:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


class _FailSecondMove:
    def __init__(self, delegate: LocalStorageAdapter) -> None:
        self.delegate = delegate
        self.failed = False

    def inspect_file(self, rel: str):
        return self.delegate.inspect_file(rel)

    def exists(self, rel: str) -> bool:
        return self.delegate.exists(rel)

    def move(self, src_rel: str, dest_rel: str) -> None:
        if src_rel.endswith("second.mkv") and not self.failed:
            self.failed = True
            raise RuntimeError("synthetic move failure")
        self.delegate.move(src_rel, dest_rel)


def _graph(session_factory, media_root: Path) -> tuple[int, LocalStorageAdapter]:
    storage_root = media_root / "library"
    (storage_root / "old-folder").mkdir(parents=True)
    (storage_root / "old-folder" / "first.mkv").write_bytes(b"first")
    (storage_root / "old-folder" / "second.mkv").write_bytes(b"second")
    with session_factory() as db:
        storage = Storage(
            name="Local",
            kind=StorageKind.LOCAL,
            config_json={"path": "library"},
        )
        profile = Profile(
            name="Video",
            kind=ProfileKind.VIDEO,
            layout_strategy=LayoutStrategy.FLAT,
        )
        playlist = Playlist(
            name="Original name",
            folder_name="old-folder",
            url="https://example.com/playlist",
            kind_hint=PlaylistKindHint.VIDEO,
        )
        db.add_all([storage, profile, playlist])
        db.flush()
        assignment = PlaylistProfile(
            playlist_id=playlist.id,
            profile_id=profile.id,
            storage_id=storage.id,
            subpath="{playlist.folder_name}",
        )
        db.add(assignment)
        db.flush()
        for index, filename in enumerate(("first.mkv", "second.mkv"), start=1):
            item = Item(
                playlist_id=playlist.id,
                source_id=f"source-{index}",
                source_url=f"https://example.com/item/{index}",
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
            db.add(
                Artifact(
                    target_id=target.id,
                    role=ArtifactRole.SOURCE,
                    storage_id=storage.id,
                    relative_path=f"old-folder/{filename}",
                    filesize=(storage_root / "old-folder" / filename).stat().st_size,
                )
            )
        db.commit()
        playlist_id = playlist.id
    return playlist_id, LocalStorageAdapter("library", media_root=media_root)


def _plan(session_factory, playlist_id: int, adapter: LocalStorageAdapter):
    with session_factory() as db:
        return build_folder_move_plan(
            db,
            playlist_id,
            "new-folder",
            adapter_factory=lambda _storage: adapter,
        )


def test_folder_move_preview_is_read_only_and_execute_moves_each_artifact(
    session_factory, env_data_dirs, secret_key
) -> None:
    playlist_id, adapter = _graph(session_factory, env_data_dirs["MEDIA_ROOT"])
    plan = _plan(session_factory, playlist_id, adapter)

    assert plan.movable
    assert plan.move_count == 2
    assert adapter.exists("old-folder/first.mkv")
    assert not adapter.exists("new-folder/first.mkv")
    with session_factory() as db:
        assert db.get(Playlist, playlist_id).folder_name == "old-folder"

    signer = FolderMoveConfirmationSigner(secret_key)
    token = signer.issue(plan)
    hook = _RecordingHook()
    result = execute_folder_move(
        session_factory,
        plan,
        adapter_factory=lambda _storage, _settings: adapter,
        confirmation_token=token,
        confirmation_signer=signer,
        confirmation_ttl_sec=300,
        hook=hook,
    )

    assert result.moved_count == 2
    assert not adapter.exists("old-folder/first.mkv")
    assert adapter.exists("new-folder/first.mkv")
    with session_factory() as db:
        playlist = db.get(Playlist, playlist_id)
        assert playlist is not None and playlist.folder_name == "new-folder"
        assert list(db.scalars(select(Artifact.relative_path).order_by(Artifact.id))) == [
            "new-folder/first.mkv",
            "new-folder/second.mkv",
        ]
        run = db.get(Run, result.run_id)
        assert run is not None and run.status == RunStatus.SUCCEEDED
        assert run.stats_json["moved_count"] == 2
    assert [event for event, _payload in hook.events] == [
        "run_started",
        "run_finished",
    ]


def test_folder_move_failure_commits_progress_and_can_be_retried(
    session_factory, env_data_dirs, secret_key
) -> None:
    playlist_id, local = _graph(session_factory, env_data_dirs["MEDIA_ROOT"])
    failing = _FailSecondMove(local)
    with session_factory() as db:
        first_plan = build_folder_move_plan(
            db,
            playlist_id,
            "new-folder",
            adapter_factory=lambda _storage: failing,
        )
    signer = FolderMoveConfirmationSigner(secret_key)

    with pytest.raises(FolderMoveExecutionError, match="再実行できます"):
        execute_folder_move(
            session_factory,
            first_plan,
            adapter_factory=lambda _storage, _settings: failing,
            confirmation_token=signer.issue(first_plan),
            confirmation_signer=signer,
            confirmation_ttl_sec=300,
            hook=_RecordingHook(),
        )

    with session_factory() as db:
        playlist = db.get(Playlist, playlist_id)
        assert playlist is not None and playlist.folder_name == "old-folder"
        paths = list(db.scalars(select(Artifact.relative_path).order_by(Artifact.id)))
        assert paths == ["new-folder/first.mkv", "old-folder/second.mkv"]
        failed_run = db.scalar(select(Run).order_by(Run.id.desc()))
        assert failed_run is not None and failed_run.status == RunStatus.FAILED
        assert failed_run.stats_json["moved_count"] == 1

    retry_plan = _plan(session_factory, playlist_id, local)
    assert retry_plan.move_count == 1
    assert retry_plan.unaffected_count == 1
    result = execute_folder_move(
        session_factory,
        retry_plan,
        adapter_factory=lambda _storage, _settings: local,
        confirmation_token=signer.issue(retry_plan),
        confirmation_signer=signer,
        confirmation_ttl_sec=300,
        hook=_RecordingHook(),
    )
    assert result.moved_count == 1
    with session_factory() as db:
        assert db.get(Playlist, playlist_id).folder_name == "new-folder"


def _hidden(response, name: str) -> str:
    match = re.search(rf'name="{re.escape(name)}"\s+value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _client(base_env, session_factory, hook: _RecordingHook) -> TestClient:
    settings = Settings()
    settings.ADMIN_PASSWORD = "correct-password"
    ensure_initial_user(session_factory, settings)
    client = TestClient(
        create_app(settings=settings, session_factory=session_factory, hook=hook)
    )
    login = client.get("/login")
    response = client.post(
        "/login",
        data={
            "csrf_token": _hidden(login, "csrf_token"),
            "username": settings.ADMIN_USERNAME,
            "password": "correct-password",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def test_web_name_edit_never_moves_files_and_explicit_action_shows_count(
    base_env,
    session_factory,
    env_data_dirs,
    monkeypatch,
) -> None:
    playlist_id, adapter = _graph(session_factory, env_data_dirs["MEDIA_ROOT"])
    monkeypatch.setattr(
        "sluicery.web.app.create_storage_adapter",
        lambda _storage, _settings: adapter,
    )
    client = _client(base_env, session_factory, _RecordingHook())

    edit_page = client.get(f"/playlists/{playlist_id}/edit")
    edited = client.post(
        f"/playlists/{playlist_id}/edit",
        data={
            "csrf_token": _hidden(edit_page, "csrf_token"),
            "name": "Renamed only",
            "folder_name": "tampered-folder",
            "url": "https://example.com/playlist",
            "kind_hint": "video",
            "missing_policy": "leave",
            "ytdlp_args": "",
            "discover_cron": "",
            "download_cron": "",
            "enabled": "yes",
        },
        follow_redirects=False,
    )
    assert edited.status_code == 303
    assert adapter.exists("old-folder/first.mkv")
    with session_factory() as db:
        playlist = db.get(Playlist, playlist_id)
        assert playlist is not None
        assert playlist.name == "Renamed only"
        assert playlist.folder_name == "old-folder"

    page = client.get(f"/playlists/{playlist_id}/move-folder")
    assert "フォルダも移動する" in page.text
    preview = client.post(
        f"/playlists/{playlist_id}/move-folder/preview",
        data={
            "csrf_token": _hidden(page, "csrf_token"),
            "new_folder_name": "new-folder",
        },
    )
    assert preview.status_code == 200
    assert "移動対象 2件" in preview.text
    assert adapter.exists("old-folder/first.mkv")

    execute = client.post(
        f"/playlists/{playlist_id}/move-folder/execute",
        data={
            "csrf_token": _hidden(preview, "csrf_token"),
            "confirmation_token": _hidden(preview, "confirmation_token"),
            "new_folder_name": "new-folder",
            "confirmed": "yes",
        },
        follow_redirects=False,
    )
    assert execute.status_code == 303
    assert adapter.exists("new-folder/first.mkv")
    with session_factory() as db:
        assert db.get(Playlist, playlist_id).folder_name == "new-folder"
