from __future__ import annotations

import pytest
from sqlalchemy import func, select

from sluicery.core.sync import (
    SyncAlreadyRunningError,
    enqueue_discover_run,
    execute_download_run,
    queue_download_phase,
)
from sluicery.db.models import (
    Item,
    ItemMembership,
    LayoutStrategy,
    Playlist,
    PlaylistKindHint,
    PlaylistProfile,
    Profile,
    ProfileKind,
    RunStatus,
    Storage,
    StorageKind,
    Target,
    TargetStatus,
    Task,
    TaskType,
)
from sluicery.storage.base import (
    ConnectionStage,
    ConnectionStageResult,
    ConnectionTestResult,
    StageStatus,
)
from sluicery.storage.errors import StorageClassification


class _Adapter:
    def __init__(self, *, ok: bool = True, free_bytes: int | None = 10**12) -> None:
        self.ok = ok
        self.free_bytes = free_bytes
        self.connection_calls = 0

    def test_connection(self):
        self.connection_calls += 1
        return ConnectionTestResult(
            (
                ConnectionStageResult(
                    ConnectionStage.CONNECTIVITY,
                    StageStatus.SUCCESS if self.ok else StageStatus.FAILED,
                    "ok" if self.ok else "failed",
                    StorageClassification.OK if self.ok else StorageClassification.UNREACHABLE,
                    "ok" if self.ok else "unreachable",
                ),
            )
        )

    def free_space(self):
        return self.free_bytes


def _graph(db_session):
    storage = Storage(name="s", kind=StorageKind.LOCAL, config_json={"path": "out"})
    profile = Profile(name="p", kind=ProfileKind.VIDEO, layout_strategy=LayoutStrategy.FLAT)
    playlist = Playlist(
        name="p",
        folder_name="p",
        url="https://example.com/list",
        kind_hint=PlaylistKindHint.VIDEO,
    )
    db_session.add_all([storage, profile, playlist])
    db_session.flush()
    assignment = PlaylistProfile(
        playlist_id=playlist.id, profile_id=profile.id, storage_id=storage.id
    )
    db_session.add(assignment)
    db_session.commit()
    return playlist, storage, assignment


def _target(
    db_session,
    playlist,
    assignment,
    source_id: str,
    index: int,
    status: TargetStatus,
    *,
    retry_count: int = 0,
    membership: ItemMembership = ItemMembership.ACTIVE,
):
    item = Item(
        playlist_id=playlist.id,
        source_id=source_id,
        source_url=f"https://example.com/watch/{source_id}",
        playlist_index=index,
        membership=membership,
    )
    db_session.add(item)
    db_session.flush()
    target = Target(
        item_id=item.id,
        playlist_profile_id=assignment.id,
        status=status,
        retry_count=retry_count,
    )
    db_session.add(target)
    db_session.commit()
    return target


def test_download_phase_selects_retryable_targets_in_playlist_order_and_limits(db_session):
    playlist, _, assignment = _graph(db_session)
    later = _target(db_session, playlist, assignment, "later", 2, TargetStatus.PENDING)
    first = _target(
        db_session, playlist, assignment, "first", 1, TargetStatus.FAILED, retry_count=1
    )
    exhausted = _target(
        db_session, playlist, assignment, "exhausted", 3, TargetStatus.FAILED, retry_count=5
    )
    unavailable = _target(
        db_session, playlist, assignment, "unavailable", 4, TargetStatus.UNAVAILABLE
    )
    downloaded = _target(db_session, playlist, assignment, "downloaded", 5, TargetStatus.DOWNLOADED)
    _target(
        db_session,
        playlist,
        assignment,
        "delisted",
        0,
        TargetStatus.PENDING,
        membership=ItemMembership.DELISTED,
    )
    adapter = _Adapter()

    stats = queue_download_phase(
        db_session,
        playlist.id,
        run_id=None,
        max_targets=1,
        max_attempts=5,
        adapter_factory=lambda _storage, _settings: adapter,  # type: ignore[arg-type]
    )

    db_session.refresh(first)
    db_session.refresh(later)
    db_session.refresh(exhausted)
    db_session.refresh(unavailable)
    db_session.refresh(downloaded)
    assert stats.targets_queued == 1
    assert stats.targets_remaining == 1
    assert stats.downloaded == 1
    assert stats.failed == 1
    assert first.status == TargetStatus.QUEUED
    assert later.status == TargetStatus.PENDING
    assert exhausted.status == TargetStatus.FAILED
    assert unavailable.status == TargetStatus.UNAVAILABLE
    assert downloaded.status == TargetStatus.DOWNLOADED
    assert db_session.scalar(select(func.count()).select_from(Task)) == 5
    assert adapter.connection_calls == 1


def test_unreachable_storage_blocks_all_targets_without_enqueuing(db_session):
    playlist, _, assignment = _graph(db_session)
    one = _target(db_session, playlist, assignment, "one", 1, TargetStatus.PENDING)
    two = _target(db_session, playlist, assignment, "two", 2, TargetStatus.FAILED)
    adapter = _Adapter(ok=False)

    stats = queue_download_phase(
        db_session,
        playlist.id,
        max_targets=50,
        max_attempts=5,
        adapter_factory=lambda _storage, _settings: adapter,  # type: ignore[arg-type]
    )

    db_session.refresh(one)
    db_session.refresh(two)
    assert stats.targets_queued == 0
    assert stats.blocked == 2
    assert one.status == two.status == TargetStatus.BLOCKED
    assert one.blocked_reason == "Storage事前確認失敗: connectivity/unreachable"
    assert db_session.scalar(select(func.count()).select_from(Task)) == 0
    assert adapter.connection_calls == 1


def test_blocked_target_returns_to_pending_after_storage_recovers(db_session):
    playlist, _, assignment = _graph(db_session)
    target = _target(db_session, playlist, assignment, "one", 1, TargetStatus.BLOCKED)
    adapter = _Adapter()

    stats = queue_download_phase(
        db_session,
        playlist.id,
        max_targets=50,
        max_attempts=5,
        adapter_factory=lambda _storage, _settings: adapter,  # type: ignore[arg-type]
    )

    db_session.refresh(target)
    assert stats.targets_queued == 1
    assert target.status == TargetStatus.QUEUED
    assert target.blocked_reason is None


def test_low_capacity_blocks_target_before_chain_creation(db_session):
    playlist, _, assignment = _graph(db_session)
    target = _target(db_session, playlist, assignment, "one", 1, TargetStatus.PENDING)
    adapter = _Adapter(free_bytes=0)

    stats = queue_download_phase(
        db_session,
        playlist.id,
        adapter_factory=lambda _storage, _settings: adapter,  # type: ignore[arg-type]
    )

    db_session.refresh(target)
    assert stats.blocked == 1
    assert target.status == TargetStatus.BLOCKED
    assert db_session.scalar(select(func.count()).select_from(Task)) == 0


def test_discover_run_and_task_are_created_without_storing_url_in_payload(db_session):
    playlist, _, _ = _graph(db_session)

    run, task = enqueue_discover_run(db_session, playlist.id, dry_run=True, max_attempts=3)

    assert run.kind == "discover"
    assert run.status == RunStatus.RUNNING
    assert task.type == TaskType.DISCOVER
    assert task.run_id == run.id
    assert task.max_attempts == 3
    assert task.payload_json == {"playlist_id": playlist.id, "dry_run": True}
    assert playlist.url not in str(task.payload_json)


def test_second_sync_for_same_playlist_is_rejected(db_session):
    playlist, _, _ = _graph(db_session)
    enqueue_discover_run(db_session, playlist.id)

    with pytest.raises(SyncAlreadyRunningError):
        enqueue_discover_run(db_session, playlist.id)


def test_download_run_finishes_when_chains_have_been_enqueued(db_session):
    playlist, _, assignment = _graph(db_session)
    _target(db_session, playlist, assignment, "one", 1, TargetStatus.PENDING)
    adapter = _Adapter()

    run = execute_download_run(
        db_session,
        playlist.id,
        max_targets=50,
        max_attempts=5,
        adapter_factory=lambda _storage, _settings: adapter,  # type: ignore[arg-type]
    )

    assert run.status == RunStatus.SUCCEEDED
    assert run.finished_at is not None
    assert run.stats_json is not None
    assert run.stats_json["targets_queued"] == 1
    assert db_session.scalar(select(func.count()).select_from(Task)) == 5


def test_download_run_is_failed_when_storage_blocks_every_target(db_session):
    playlist, _, assignment = _graph(db_session)
    _target(db_session, playlist, assignment, "one", 1, TargetStatus.PENDING)
    adapter = _Adapter(ok=False)

    run = execute_download_run(
        db_session,
        playlist.id,
        adapter_factory=lambda _storage, _settings: adapter,  # type: ignore[arg-type]
    )

    assert run.status == RunStatus.FAILED
    assert run.stats_json is not None
    assert run.stats_json["targets_queued"] == 0
    assert run.stats_json["blocked"] == 1


def test_download_run_is_failed_for_new_blocked_target_despite_existing_downloaded(
    db_session,
):
    playlist, _, assignment = _graph(db_session)
    _target(db_session, playlist, assignment, "done", 1, TargetStatus.DOWNLOADED)
    _target(db_session, playlist, assignment, "blocked", 2, TargetStatus.PENDING)
    adapter = _Adapter(ok=False)

    run = execute_download_run(
        db_session,
        playlist.id,
        adapter_factory=lambda _storage, _settings: adapter,  # type: ignore[arg-type]
    )

    assert run.status == RunStatus.FAILED
    assert run.stats_json is not None
    assert run.stats_json["downloaded"] == 1
    assert run.stats_json["blocked"] == 1


def test_download_run_with_nothing_to_do_succeeds(db_session):
    playlist, _, _ = _graph(db_session)

    run = execute_download_run(
        db_session,
        playlist.id,
        adapter_factory=lambda _storage, _settings: _Adapter(),  # type: ignore[arg-type]
    )

    assert run.status == RunStatus.SUCCEEDED
    assert run.stats_json is not None
    assert run.stats_json["targets_queued"] == 0
