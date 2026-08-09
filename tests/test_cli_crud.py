from __future__ import annotations

from pathlib import Path

from sqlalchemy import func, select

from sluicery import cli
from sluicery.db.models import (
    Artifact,
    ArtifactRole,
    Item,
    Playlist,
    PlaylistProfile,
    Profile,
    Storage,
    Target,
)


def _patch_sessions(monkeypatch, session_factory) -> None:
    monkeypatch.setattr(cli, "_open_session", lambda: session_factory())


def test_minimal_crud_attach_preview_and_safe_remove(
    monkeypatch,
    session_factory,
    base_env,
    capsys,
) -> None:
    _patch_sessions(monkeypatch, session_factory)

    assert (
        cli.main(
            [
                "storage",
                "add",
                "--kind",
                "local",
                "--name",
                "media",
                "--path",
                "/mnt/media",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "profile",
                "add",
                "--name",
                "video",
                "--kind",
                "video",
                "--embed-metadata",
                "--ytdlp-args=--password very-secret",
            ]
        )
        == 0
    )
    assert (
        cli.main(
            [
                "playlist",
                "add",
                "--name",
                "一覧",
                "--folder-name",
                "動画:一覧",
                "--url",
                "https://example.com/playlist",
            ]
        )
        == 0
    )
    assert cli.main(["playlist", "attach-profile", "一覧", "video"]) == 0
    assert cli.main(["profile", "edit", "video", "--inherit-embed-metadata"]) == 0

    capsys.readouterr()
    assert (
        cli.main(
            [
                "options",
                "preview",
                "--playlist",
                "一覧",
                "--profile",
                "video",
            ]
        )
        == 0
    )
    preview = capsys.readouterr().out
    assert "コマンド:" in preview
    assert "[L2]" in preview
    assert "[L3]" in preview
    assert "[L4]" in preview
    assert "解決済み出力パス:" in preview
    assert "very-secret" not in preview
    assert "********" in preview

    session = session_factory()
    try:
        playlist = session.scalar(select(Playlist).where(Playlist.name == "一覧"))
        assert playlist is not None
        assert playlist.folder_name == "動画_一覧"
        profile = session.scalar(select(Profile).where(Profile.name == "video"))
        assert profile is not None
        assert profile.embed_metadata is None
    finally:
        session.close()

    assert cli.main(["playlist", "detach-profile", "一覧", "video"]) == 0
    assert cli.main(["profile", "remove", "video"]) == 0
    assert cli.main(["storage", "remove", "media"]) == 0
    assert cli.main(["playlist", "remove", "一覧", "--keep-items"]) == 0

    session = session_factory()
    try:
        playlist = session.scalar(select(Playlist).where(Playlist.name == "一覧"))
        assert playlist is not None
        assert playlist.enabled is False
        assert playlist.paused is True
    finally:
        session.close()


def test_remote_storage_and_invalid_custom_profile_are_rejected(
    monkeypatch,
    session_factory,
    base_env,
    capsys,
) -> None:
    _patch_sessions(monkeypatch, session_factory)
    assert (
        cli.main(
            [
                "storage",
                "add",
                "--kind",
                "remote",
                "--name",
                "remote",
                "--path",
                "/unused",
            ]
        )
        == 1
    )
    assert "Phase 5" in capsys.readouterr().err

    assert (
        cli.main(
            [
                "profile",
                "add",
                "--name",
                "bad-custom",
                "--kind",
                "video",
                "--layout-strategy",
                "custom",
                "--output-template",
                "%(title)s.%(ext)s",
            ]
        )
        == 1
    )
    assert "[%(id)s]" in capsys.readouterr().err


def test_delete_items_removes_only_db_records_and_never_files(
    monkeypatch,
    session_factory,
    base_env,
    tmp_path: Path,
) -> None:
    _patch_sessions(monkeypatch, session_factory)
    marker = tmp_path / "media-file.mkv"
    marker.write_text("keep", encoding="utf-8")

    session = session_factory()
    try:
        storage = Storage(
            name="media",
            kind="local",
            enabled=True,
            config_json={"path": str(tmp_path)},
        )
        profile = Profile(
            name="video",
            kind="video",
            layout_strategy="flat",
            expert_mode=False,
            allow_exec=False,
            postprocess_chain_json=[],
        )
        playlist = Playlist(
            name="削除対象",
            folder_name="削除対象",
            url="https://example.com/list",
            enabled=True,
            kind_hint="video",
            paused=False,
            dedup_hardlink=False,
        )
        session.add_all([storage, profile, playlist])
        session.flush()
        association = PlaylistProfile(
            playlist_id=playlist.id,
            profile_id=profile.id,
            storage_id=storage.id,
            subpath="削除対象",
            enabled=True,
            sort_order=0,
        )
        session.add(association)
        session.flush()
        item = Item(
            playlist_id=playlist.id,
            source_id="source",
            source_url="https://example.com/item",
        )
        session.add(item)
        session.flush()
        target = Target(item_id=item.id, playlist_profile_id=association.id)
        session.add(target)
        session.flush()
        session.add(
            Artifact(
                target_id=target.id,
                role=ArtifactRole.SOURCE,
                storage_id=storage.id,
                relative_path=marker.name,
            )
        )
        playlist_id = playlist.id
        session.commit()
    finally:
        session.close()

    assert cli.main(["playlist", "remove", str(playlist_id), "--delete-items"]) == 0
    assert marker.read_text(encoding="utf-8") == "keep"

    session = session_factory()
    try:
        assert session.get(Playlist, playlist_id) is None
        assert session.scalar(select(func.count()).select_from(Item)) == 0
        assert session.scalar(select(func.count()).select_from(Target)) == 0
        assert session.scalar(select(func.count()).select_from(Artifact)) == 0
    finally:
        session.close()


def test_show_masks_credentials(
    monkeypatch,
    session_factory,
    base_env,
    capsys,
) -> None:
    _patch_sessions(monkeypatch, session_factory)
    session = session_factory()
    try:
        storage = Storage(
            name="secret-storage",
            kind="local",
            enabled=True,
            config_json={"path": "/mnt/media", "api_token": "token-value"},
            credentials_encrypted={"password": "plain-password"},
        )
        session.add(storage)
        session.commit()
    finally:
        session.close()

    assert cli.main(["storage", "show", "secret-storage"]) == 0
    output = capsys.readouterr().out
    assert "token-value" not in output
    assert "plain-password" not in output
    assert "********" in output
