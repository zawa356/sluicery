from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from sluicery.config import Settings
from sluicery.db.models import (
    Artifact,
    ArtifactRole,
    Item,
    ItemMembership,
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


class FakeStorage:
    def __init__(self, files: list[str]) -> None:
        self.files = files

    def exists(self, relative_path: str) -> bool:
        return relative_path in self.files

    def list_recursive(self, relative_path: str):
        yield from (RemoteFile(path, 100) for path in self.files)


def _csrf(response) -> str:
    match = re.search(r'name="csrf_token"\s+value="([^"]+)"', response.text)
    assert match is not None
    return match.group(1)


def _client(session_factory) -> TestClient:
    settings = Settings()
    settings.ADMIN_PASSWORD = "correct-password"
    ensure_initial_user(session_factory, settings)
    client = TestClient(create_app(settings=settings, session_factory=session_factory))
    login = client.get("/login")
    response = client.post(
        "/login",
        data={
            "csrf_token": _csrf(login),
            "username": settings.ADMIN_USERNAME,
            "password": settings.ADMIN_PASSWORD,
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    return client


def _missing_artifact(session_factory) -> tuple[int, int]:
    with session_factory() as db:
        storage = Storage(
            name="media",
            kind=StorageKind.LOCAL,
            config_json={"path": "/media"},
        )
        profile = Profile(
            name="video",
            kind=ProfileKind.VIDEO,
            layout_strategy=LayoutStrategy.FLAT,
        )
        playlist = Playlist(
            name="Integrity Playlist",
            folder_name="integrity",
            url="https://example.com/integrity",
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
        item = Item(
            playlist_id=playlist.id,
            source_id="source-id",
            source_url="https://example.com/source-id",
            title="Missing title",
        )
        db.add(item)
        db.flush()
        target = Target(
            item_id=item.id,
            playlist_profile_id=assignment.id,
            status=TargetStatus.MISSING,
        )
        db.add(target)
        db.flush()
        artifact = Artifact(
            target_id=target.id,
            role=ArtifactRole.SOURCE,
            storage_id=storage.id,
            relative_path="old/title [source-id].mkv",
            missing_since=datetime(2026, 8, 16, tzinfo=UTC),
        )
        db.add(artifact)
        db.commit()
        return storage.id, artifact.id


def test_integrity_report_links_and_undoes_without_file_operation(
    base_env, session_factory, monkeypatch
) -> None:
    storage_id, artifact_id = _missing_artifact(session_factory)
    adapter = FakeStorage(["orphan/renamed.mkv"])
    monkeypatch.setattr(
        "sluicery.web.app.create_storage_adapter",
        lambda storage, operational: adapter,
    )
    client = _client(session_factory)

    report = client.get(f"/reports/integrity?storage_id={storage_id}")
    assert "Missing title" in report.text
    assert "orphan/renamed.mkv" in report.text
    linked = client.post(
        "/reports/integrity/link",
        data={
            "csrf_token": _csrf(report),
            "storage_id": str(storage_id),
            "artifact_id": str(artifact_id),
            "candidate_path": "orphan/renamed.mkv",
        },
        follow_redirects=False,
    )

    assert linked.status_code == 303
    assert adapter.files == ["orphan/renamed.mkv"]
    linked_report = client.get(linked.headers["location"])
    assert "手動リンク履歴" in linked_report.text
    with session_factory() as db:
        artifact = db.get(Artifact, artifact_id)
        assert artifact is not None
        assert artifact.relative_path == "orphan/renamed.mkv"

    unlinked = client.post(
        "/reports/integrity/unlink",
        data={
            "csrf_token": _csrf(linked_report),
            "storage_id": str(storage_id),
            "artifact_id": str(artifact_id),
        },
        follow_redirects=False,
    )

    assert unlinked.status_code == 303
    assert adapter.files == ["orphan/renamed.mkv"]
    with session_factory() as db:
        artifact = db.get(Artifact, artifact_id)
        assert artifact is not None
        assert artifact.relative_path == "old/title [source-id].mkv"
        assert artifact.missing_since is not None


def test_delisted_report_filters_and_only_exposes_read_operations(
    base_env, session_factory
) -> None:
    with session_factory() as db:
        storage = Storage(
            name="archive",
            kind=StorageKind.LOCAL,
            config_json={"path": "/archive"},
        )
        profile = Profile(
            name="source",
            kind=ProfileKind.VIDEO,
            layout_strategy=LayoutStrategy.FLAT,
        )
        db.add_all([storage, profile])
        db.flush()
        playlist_ids: list[int] = []
        for index, delisted_at in enumerate(
            (
                datetime(2026, 8, 14, 18, tzinfo=UTC),
                datetime(2026, 8, 16, 3, tzinfo=UTC),
            )
        ):
            playlist = Playlist(
                name=f"Delisted {index}",
                folder_name=f"delisted-{index}",
                url=f"https://example.com/delisted-{index}",
                kind_hint=PlaylistKindHint.VIDEO,
            )
            db.add(playlist)
            db.flush()
            playlist_ids.append(playlist.id)
            assignment = PlaylistProfile(
                playlist_id=playlist.id,
                profile_id=profile.id,
                storage_id=storage.id,
            )
            db.add(assignment)
            db.flush()
            item = Item(
                playlist_id=playlist.id,
                source_id=f"gone-{index}",
                source_url=f"https://example.com/gone-{index}",
                title=f"Gone title {index}",
                membership=ItemMembership.DELISTED,
                delisted_at=delisted_at,
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
                    relative_path=f"library/gone-{index}.mkv",
                )
            )
        db.commit()

    client = _client(session_factory)
    period = client.get("/reports/delisted?date_from=2026-08-15&date_to=2026-08-15")

    assert period.status_code == 200
    assert "Gone title 0" in period.text
    assert "library/gone-0.mkv" in period.text
    assert "Gone title 1" not in period.text
    post_actions = re.findall(
        r'<form[^>]+method="post"[^>]+action="([^"]+)"', period.text
    )
    assert post_actions == ["/logout"]
    playlist = client.get(f"/reports/delisted?playlist_id={playlist_ids[1]}")
    assert "Gone title 1" in playlist.text
    assert "Gone title 0" not in playlist.text
