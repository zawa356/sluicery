from __future__ import annotations

import argparse
from pathlib import Path

from sluicery import cli_integrity
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
from sluicery.storage.local import LocalStorageAdapter


def _args(*, storage: str | None = None, playlist: str | None = None):
    return argparse.Namespace(
        command="integrity",
        integrity_command="check",
        storage=storage,
        playlist=playlist,
    )


def _artifact(session_factory, root: Path) -> tuple[int, int, int]:
    with session_factory() as session:
        storage = Storage(
            name="media",
            kind=StorageKind.LOCAL,
            config_json={"path": str(root)},
        )
        profile = Profile(
            name="video",
            kind=ProfileKind.VIDEO,
            layout_strategy=LayoutStrategy.FLAT,
        )
        playlist = Playlist(
            name="list",
            folder_name="list",
            url="https://example.com/list",
            kind_hint=PlaylistKindHint.VIDEO,
        )
        session.add_all([storage, profile, playlist])
        session.flush()
        assignment = PlaylistProfile(
            playlist_id=playlist.id,
            profile_id=profile.id,
            storage_id=storage.id,
        )
        session.add(assignment)
        session.flush()
        item = Item(
            playlist_id=playlist.id,
            source_id="source-id",
            source_url="https://example.com/source-id",
        )
        session.add(item)
        session.flush()
        target = Target(
            item_id=item.id,
            playlist_profile_id=assignment.id,
            status=TargetStatus.DOWNLOADED,
        )
        session.add(target)
        session.flush()
        artifact = Artifact(
            target_id=target.id,
            role=ArtifactRole.SOURCE,
            storage_id=storage.id,
            relative_path="old/title [source-id].mkv",
        )
        session.add(artifact)
        session.commit()
        return storage.id, playlist.id, artifact.id


def test_integrity_cli_filters_and_relinks_database_only(
    session_factory, tmp_path: Path, capsys, monkeypatch
) -> None:
    root = tmp_path / "media"
    moved = root / "new" / "renamed [source-id].mkv"
    moved.parent.mkdir(parents=True)
    moved.write_bytes(b"media")
    _storage_id, _playlist_id, artifact_id = _artifact(session_factory, root)
    monkeypatch.setattr(
        cli_integrity,
        "create_storage_adapter",
        lambda _storage, _settings: LocalStorageAdapter(
            str(root), media_root=tmp_path
        ),
    )

    result = cli_integrity.dispatch(
        _args(storage="media", playlist="list"),
        open_session=session_factory,
    )

    assert result == 0
    assert "relink=1" in capsys.readouterr().out
    assert moved.read_bytes() == b"media"
    with session_factory() as session:
        artifact = session.get(Artifact, artifact_id)
        assert artifact is not None
        assert artifact.relative_path == "new/renamed [source-id].mkv"


def test_integrity_cli_rejects_unknown_selector(session_factory, capsys) -> None:
    result = cli_integrity.dispatch(
        _args(storage="unknown"),
        open_session=session_factory,
    )

    assert result == 1
    assert "Storage" in capsys.readouterr().err
