from __future__ import annotations

from datetime import UTC, datetime

from sluicery.core.integrity import check_integrity
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
from sluicery.storage.base import RemoteFile, StorageOperationError


class FakeStorage:
    def __init__(
        self,
        files: list[str],
        *,
        exists_error: bool = False,
        scan_error: bool = False,
    ) -> None:
        self.files = files
        self.exists_error = exists_error
        self.scan_error = scan_error
        self.scan_count = 0

    def exists(self, rel: str) -> bool:
        if self.exists_error:
            raise StorageOperationError("unreachable")
        return rel in self.files

    def list_recursive(self, rel: str):
        self.scan_count += 1
        if self.scan_error:
            raise StorageOperationError("unreachable")
        yield from [RemoteFile(path, 1) for path in self.files]


def _graph(db_session, *, count: int = 1) -> tuple[Storage, list[Target], list[Artifact]]:
    storage = Storage(name="media", kind=StorageKind.LOCAL, config_json={"path": "/media"})
    profile = Profile(name="video", kind=ProfileKind.VIDEO, layout_strategy=LayoutStrategy.FLAT)
    playlist = Playlist(
        name="list",
        folder_name="list",
        url="https://example.com/list",
        kind_hint=PlaylistKindHint.VIDEO,
    )
    db_session.add_all([storage, profile, playlist])
    db_session.flush()
    assignment = PlaylistProfile(
        playlist_id=playlist.id,
        profile_id=profile.id,
        storage_id=storage.id,
    )
    db_session.add(assignment)
    db_session.flush()
    targets: list[Target] = []
    artifacts: list[Artifact] = []
    for index in range(count):
        source_id = f"source-{index}"
        item = Item(
            playlist_id=playlist.id,
            source_id=source_id,
            source_url=f"https://example.com/{source_id}",
        )
        db_session.add(item)
        db_session.flush()
        target = Target(
            item_id=item.id,
            playlist_profile_id=assignment.id,
            status=TargetStatus.DOWNLOADED,
        )
        db_session.add(target)
        db_session.flush()
        artifact = Artifact(
            target_id=target.id,
            role=ArtifactRole.SOURCE,
            storage_id=storage.id,
            relative_path=f"old/title [{source_id}].mkv",
        )
        db_session.add(artifact)
        targets.append(target)
        artifacts.append(artifact)
    db_session.commit()
    return storage, targets, artifacts


def test_relinks_moved_file_without_moving_it(db_session) -> None:
    storage, targets, artifacts = _graph(db_session)
    adapter = FakeStorage(["new/renamed [source-0].mkv"])

    report = check_integrity(db_session, lambda _: adapter)

    db_session.refresh(artifacts[0])
    db_session.refresh(targets[0])
    assert artifacts[0].relative_path == "new/renamed [source-0].mkv"
    assert artifacts[0].missing_since is None
    assert targets[0].status == TargetStatus.DOWNLOADED
    assert report.relinked == 1
    assert adapter.files == ["new/renamed [source-0].mkv"]


def test_marks_missing_only_after_successful_rescan(db_session) -> None:
    _storage, targets, artifacts = _graph(db_session)
    checked_at = datetime(2026, 8, 16, tzinfo=UTC)

    report = check_integrity(db_session, lambda _: FakeStorage([]), now=checked_at)

    db_session.refresh(artifacts[0])
    db_session.refresh(targets[0])
    assert artifacts[0].missing_since == checked_at
    assert targets[0].status == TargetStatus.MISSING
    assert report.missing == 1


def test_multiple_candidates_are_not_selected(db_session) -> None:
    _storage, targets, artifacts = _graph(db_session)
    adapter = FakeStorage(
        ["a/one [source-0].mkv", "b/two [source-0].mp4"]
    )

    report = check_integrity(db_session, lambda _: adapter)

    db_session.refresh(artifacts[0])
    db_session.refresh(targets[0])
    assert artifacts[0].relative_path == "old/title [source-0].mkv"
    assert targets[0].status == TargetStatus.MISSING
    issue = next(issue for issue in report.issues if issue.kind == "multiple_candidates")
    assert issue.candidates == (
        "a/one [source-0].mkv",
        "b/two [source-0].mp4",
    )


def test_storage_error_never_marks_missing(db_session) -> None:
    _storage, targets, artifacts = _graph(db_session)

    report = check_integrity(
        db_session,
        lambda _: FakeStorage([], exists_error=True),
    )

    db_session.refresh(artifacts[0])
    db_session.refresh(targets[0])
    assert artifacts[0].missing_since is None
    assert targets[0].status == TargetStatus.DOWNLOADED
    assert {issue.kind for issue in report.issues} == {"storage_error"}


def test_scan_error_never_marks_missing(db_session) -> None:
    _storage, targets, artifacts = _graph(db_session)

    report = check_integrity(db_session, lambda _: FakeStorage([], scan_error=True))

    db_session.refresh(artifacts[0])
    db_session.refresh(targets[0])
    assert artifacts[0].missing_since is None
    assert targets[0].status == TargetStatus.DOWNLOADED
    assert {issue.kind for issue in report.issues} == {"scan_error"}


def test_rescans_once_per_storage(db_session) -> None:
    _storage, _targets, _artifacts = _graph(db_session, count=20)
    adapter = FakeStorage([])

    report = check_integrity(db_session, lambda _: adapter)

    assert report.checked == 20
    assert report.missing == 20
    assert report.rescanned_storages == 1
    assert adapter.scan_count == 1


def test_restores_missing_target_when_file_returns(db_session) -> None:
    _storage, targets, artifacts = _graph(db_session)
    targets[0].status = TargetStatus.MISSING
    artifacts[0].missing_since = datetime(2026, 8, 15, tzinfo=UTC)
    db_session.commit()
    adapter = FakeStorage([artifacts[0].relative_path])

    report = check_integrity(db_session, lambda _: adapter)

    db_session.refresh(artifacts[0])
    db_session.refresh(targets[0])
    assert artifacts[0].missing_since is None
    assert targets[0].status == TargetStatus.DOWNLOADED
    assert report.restored == 1


def test_tracked_same_id_file_is_not_used_for_relink(db_session) -> None:
    _storage, _targets, artifacts = _graph(db_session, count=2)
    artifacts[1].relative_path = "tracked/other [source-0].mkv"
    db_session.commit()
    adapter = FakeStorage([artifacts[1].relative_path])

    check_integrity(db_session, lambda _: adapter)

    db_session.refresh(artifacts[0])
    assert artifacts[0].relative_path == "old/title [source-0].mkv"
    assert artifacts[0].missing_since is not None


def test_source_id_matching_is_anchored_at_extension() -> None:
    from sluicery.core.integrity import _matches_source_id

    assert _matches_source_id("folder/title [abc].mkv", "abc")
    assert not _matches_source_id("folder/title [abc] extra.mkv", "abc")
    assert not _matches_source_id("folder/title [xabc].mkv", "abc")
