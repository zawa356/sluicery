from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from sluicery.core.integrity import (
    check_integrity,
    list_orphan_files,
    manual_link,
    set_missing_action,
    undo_manual_link,
)
from sluicery.db.models import (
    Artifact,
    ArtifactRole,
    Item,
    LayoutStrategy,
    MissingPolicy,
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
        scan_timeout: bool = False,
    ) -> None:
        self.files = files
        self.exists_error = exists_error
        self.scan_error = scan_error
        self.scan_timeout = scan_timeout
        self.scan_count = 0
        self.scan_timeout_sec: float | None = None

    def exists(self, rel: str) -> bool:
        if self.exists_error:
            raise StorageOperationError("unreachable")
        return rel in self.files

    def list_recursive(self, rel: str, *, timeout_sec: float | None = None):
        self.scan_count += 1
        self.scan_timeout_sec = timeout_sec
        if self.scan_timeout:
            raise StorageOperationError("timeout", reason_code="timeout")
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


@pytest.mark.parametrize(
    ("policy", "expected"),
    [
        (MissingPolicy.REDOWNLOAD, TargetStatus.PENDING),
        (MissingPolicy.IGNORE, TargetStatus.IGNORED),
    ],
)
def test_playlist_missing_policy_is_applied(db_session, policy, expected) -> None:
    _storage, targets, _artifacts = _graph(db_session)
    item = db_session.get(Item, targets[0].item_id)
    assert item is not None
    playlist = db_session.get(Playlist, item.playlist_id)
    assert playlist is not None
    playlist.missing_policy = policy
    db_session.commit()

    check_integrity(db_session, lambda _: FakeStorage([]))

    db_session.refresh(targets[0])
    assert targets[0].status == expected


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


def test_one_candidate_claimed_by_multiple_artifacts_is_not_selected(db_session) -> None:
    storage, targets, artifacts = _graph(db_session)
    derived = Artifact(
        target_id=targets[0].id,
        role=ArtifactRole.DERIVED,
        storage_id=storage.id,
        relative_path="old/derived [source-0].mkv",
    )
    db_session.add(derived)
    db_session.commit()
    candidate = "new/shared [source-0].mkv"

    report = check_integrity(db_session, lambda _: FakeStorage([candidate]))

    db_session.refresh(artifacts[0])
    db_session.refresh(derived)
    assert artifacts[0].relative_path == "old/title [source-0].mkv"
    assert derived.relative_path == "old/derived [source-0].mkv"
    assert artifacts[0].missing_since is not None
    assert derived.missing_since is not None
    assert {issue.kind for issue in report.issues} == {"shared_candidate"}


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


def test_adapter_factory_error_never_marks_missing(db_session) -> None:
    _storage, targets, artifacts = _graph(db_session)

    def broken_factory(_storage):
        raise ValueError("invalid storage configuration")

    report = check_integrity(db_session, broken_factory)

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


def test_scan_timeout_stops_adapter_and_never_marks_missing(db_session) -> None:
    _storage, targets, artifacts = _graph(db_session)
    adapter = FakeStorage([], scan_timeout=True)

    report = check_integrity(
        db_session,
        lambda _: adapter,
        rescan_timeout_sec=7,
    )

    db_session.refresh(artifacts[0])
    db_session.refresh(targets[0])
    assert adapter.scan_timeout_sec == 7
    assert artifacts[0].missing_since is None
    assert targets[0].status == TargetStatus.DOWNLOADED
    assert {issue.kind for issue in report.issues} == {"scan_timeout"}


def test_scan_error_does_not_restore_partially_confirmed_target(db_session) -> None:
    storage, targets, artifacts = _graph(db_session)
    returned_at = datetime(2026, 8, 15, tzinfo=UTC)
    artifacts[0].missing_since = returned_at
    targets[0].status = TargetStatus.MISSING
    sibling = Artifact(
        target_id=targets[0].id,
        role=ArtifactRole.DERIVED,
        storage_id=storage.id,
        relative_path="old/derived [source-0].mkv",
    )
    db_session.add(sibling)
    db_session.commit()
    adapter = FakeStorage([artifacts[0].relative_path], scan_error=True)

    report = check_integrity(db_session, lambda _: adapter)

    db_session.refresh(artifacts[0])
    db_session.refresh(targets[0])
    assert artifacts[0].missing_since == returned_at
    assert sibling.missing_since is None
    assert targets[0].status == TargetStatus.MISSING
    assert report.restored == 0
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


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (MissingPolicy.LEAVE, TargetStatus.MISSING),
        (MissingPolicy.REDOWNLOAD, TargetStatus.PENDING),
        (MissingPolicy.IGNORE, TargetStatus.IGNORED),
    ],
)
def test_explicit_missing_action_does_not_touch_file(
    db_session, action, expected
) -> None:
    _storage, targets, artifacts = _graph(db_session)
    artifacts[0].missing_since = datetime(2026, 8, 16, tzinfo=UTC)
    targets[0].status = TargetStatus.MISSING
    db_session.commit()

    result = set_missing_action(db_session, targets[0].id, action)

    assert result == expected
    assert db_session.get(Target, targets[0].id).status == expected


def test_manual_link_and_undo_only_change_database(db_session) -> None:
    storage, targets, artifacts = _graph(db_session)
    artifacts[0].missing_since = datetime(2026, 8, 16, tzinfo=UTC)
    targets[0].status = TargetStatus.MISSING
    db_session.commit()
    adapter = FakeStorage(["orphan/renamed.mkv"])

    manual_link(db_session, artifacts[0].id, "orphan/renamed.mkv", adapter)

    db_session.refresh(artifacts[0])
    db_session.refresh(targets[0])
    assert artifacts[0].relative_path == "orphan/renamed.mkv"
    assert artifacts[0].manual_link_previous_path == "old/title [source-0].mkv"
    assert artifacts[0].missing_since is None
    assert targets[0].status == TargetStatus.DOWNLOADED
    assert adapter.files == ["orphan/renamed.mkv"]

    undo_manual_link(db_session, artifacts[0].id)

    db_session.refresh(artifacts[0])
    db_session.refresh(targets[0])
    assert artifacts[0].relative_path == "old/title [source-0].mkv"
    assert artifacts[0].manual_link_previous_path is None
    assert artifacts[0].missing_since is not None
    assert targets[0].status == TargetStatus.MISSING
    orphans, error = list_orphan_files(db_session, storage, adapter)
    assert error is None
    assert [entry.relative_path for entry in orphans] == ["orphan/renamed.mkv"]


def test_manual_link_rejects_tracked_candidate(db_session) -> None:
    _storage, targets, artifacts = _graph(db_session, count=2)
    artifacts[0].missing_since = datetime(2026, 8, 16, tzinfo=UTC)
    targets[0].status = TargetStatus.MISSING
    db_session.commit()
    adapter = FakeStorage([artifacts[1].relative_path])

    with pytest.raises(ValueError, match="追跡中"):
        manual_link(db_session, artifacts[0].id, artifacts[1].relative_path, adapter)


def test_tracked_same_id_file_is_not_used_for_relink(db_session) -> None:
    _storage, _targets, artifacts = _graph(db_session, count=2)
    artifacts[1].relative_path = "tracked/other [source-0].mkv"
    db_session.commit()
    adapter = FakeStorage([artifacts[1].relative_path])

    check_integrity(db_session, lambda _: adapter)

    db_session.refresh(artifacts[0])
    assert artifacts[0].relative_path == "old/title [source-0].mkv"
    assert artifacts[0].missing_since is not None


def test_playlist_filter_still_excludes_paths_tracked_by_other_playlists(
    db_session,
) -> None:
    storage, targets, artifacts = _graph(db_session)
    first_item = db_session.get(Item, targets[0].item_id)
    assert first_item is not None
    first_playlist = db_session.get(Playlist, first_item.playlist_id)
    assert first_playlist is not None
    profile = db_session.scalar(select(Profile))
    assert profile is not None
    other_playlist = Playlist(
        name="other-list",
        folder_name="other-list",
        url="https://example.com/other-list",
        kind_hint=PlaylistKindHint.VIDEO,
    )
    db_session.add(other_playlist)
    db_session.flush()
    assignment = PlaylistProfile(
        playlist_id=other_playlist.id,
        profile_id=profile.id,
        storage_id=storage.id,
    )
    db_session.add(assignment)
    db_session.flush()
    other_item = Item(
        playlist_id=other_playlist.id,
        source_id="source-0",
        source_url="https://example.com/other-source",
    )
    db_session.add(other_item)
    db_session.flush()
    other_target = Target(
        item_id=other_item.id,
        playlist_profile_id=assignment.id,
        status=TargetStatus.DOWNLOADED,
    )
    db_session.add(other_target)
    db_session.flush()
    tracked_path = "tracked/other [source-0].mkv"
    db_session.add(
        Artifact(
            target_id=other_target.id,
            role=ArtifactRole.SOURCE,
            storage_id=storage.id,
            relative_path=tracked_path,
        )
    )
    db_session.commit()

    check_integrity(
        db_session,
        lambda _: FakeStorage([tracked_path]),
        playlist_id=first_playlist.id,
    )

    db_session.refresh(artifacts[0])
    assert artifacts[0].relative_path == "old/title [source-0].mkv"
    assert artifacts[0].missing_since is not None


def test_storage_io_runs_without_an_open_database_transaction(db_session) -> None:
    _storage, _targets, _artifacts = _graph(db_session)

    class TransactionCheckingStorage(FakeStorage):
        def exists(self, rel: str) -> bool:
            assert not db_session.in_transaction()
            return super().exists(rel)

        def list_recursive(self, rel: str, *, timeout_sec: float | None = None):
            assert not db_session.in_transaction()
            yield from super().list_recursive(rel, timeout_sec=timeout_sec)

    check_integrity(db_session, lambda _: TransactionCheckingStorage([]))


def test_file_returned_after_scan_is_not_marked_missing(db_session) -> None:
    _storage, targets, artifacts = _graph(db_session)
    missing_at = datetime(2026, 8, 15, tzinfo=UTC)
    targets[0].status = TargetStatus.MISSING
    artifacts[0].missing_since = missing_at
    db_session.commit()

    class ReturningStorage(FakeStorage):
        def __init__(self) -> None:
            super().__init__([])
            self.exists_calls = 0

        def exists(self, rel: str) -> bool:
            self.exists_calls += 1
            return self.exists_calls >= 2

    report = check_integrity(db_session, lambda _: ReturningStorage())

    db_session.refresh(artifacts[0])
    db_session.refresh(targets[0])
    assert artifacts[0].missing_since is None
    assert targets[0].status == TargetStatus.DOWNLOADED
    assert report.missing == 0
    assert report.restored == 1
    assert report.issues == []


def test_concurrent_target_update_skips_stale_missing_decision(
    db_session, session_factory
) -> None:
    _storage, targets, artifacts = _graph(db_session)

    class ConcurrentUpdateStorage(FakeStorage):
        def __init__(self) -> None:
            super().__init__([])
            self.exists_calls = 0

        def exists(self, rel: str) -> bool:
            self.exists_calls += 1
            if self.exists_calls == 2:
                with session_factory() as other:
                    target = other.get(Target, targets[0].id)
                    assert target is not None
                    target.retry_count += 1
                    other.commit()
            return False

    report = check_integrity(db_session, lambda _: ConcurrentUpdateStorage())

    db_session.refresh(artifacts[0])
    db_session.refresh(targets[0])
    assert artifacts[0].missing_since is None
    assert targets[0].status == TargetStatus.DOWNLOADED
    assert targets[0].retry_count == 1
    assert report.missing == 0


def test_source_id_matching_is_anchored_at_extension() -> None:
    from sluicery.core.integrity import _matches_source_id

    assert _matches_source_id("folder/title [abc].mkv", "abc")
    assert not _matches_source_id("folder/title [abc] extra.mkv", "abc")
    assert not _matches_source_id("folder/title [xabc].mkv", "abc")
