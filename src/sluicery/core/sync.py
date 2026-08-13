"""Playlist の discover / download 二相同期に関するドメイン処理。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import urlsplit

from sqlalchemy.orm import Session

from sluicery.core.settings import OperationalSettings
from sluicery.core.target_state import transition_item, transition_target
from sluicery.db.models import (
    ItemMembership,
    Run,
    RunStatus,
    RunTrigger,
    Storage,
    TargetStatus,
    Task,
    TaskType,
    WorkerClass,
)
from sluicery.db.repositories.item import ItemRepository
from sluicery.db.repositories.playlist import PlaylistRepository
from sluicery.db.repositories.playlist_profile import PlaylistProfileRepository
from sluicery.db.repositories.run import RunRepository
from sluicery.db.repositories.target import TargetRepository
from sluicery.db.repositories.task import TaskRepository
from sluicery.storage import create_storage_adapter
from sluicery.storage.base import StorageAdapter, evaluate_capacity
from sluicery.tasks.pipeline import enqueue_target_pipeline


class AdapterFactory(Protocol):
    def __call__(self, storage: Storage, settings: OperationalSettings) -> StorageAdapter: ...


@dataclass(frozen=True)
class SyncStats:
    new_items: int = 0
    delisted_items: int = 0
    targets_queued: int = 0
    targets_remaining: int = 0
    downloaded: int = 0
    failed: int = 0
    blocked: int = 0
    empty_result: bool = False

    def to_dict(self) -> dict[str, int | bool]:
        return asdict(self)


def enqueue_discover_run(
    session: Session,
    playlist_id: int,
    *,
    dry_run: bool = False,
    trigger: RunTrigger = RunTrigger.MANUAL,
    max_attempts: int | None = None,
) -> tuple[Run, Task]:
    """discover Runとnetwork Taskを同じtransactionで作成する。"""
    playlist = PlaylistRepository(session).get(playlist_id)
    if playlist is None:
        raise LookupError(f"Playlist {playlist_id} が見つかりません")
    if not playlist.enabled or playlist.paused:
        raise ValueError("無効または一時停止中のPlaylistは同期できません")
    attempts = OperationalSettings(session).worker_max_attempts
    if max_attempts is not None:
        attempts = max_attempts
    if attempts < 1:
        raise ValueError("再試行上限は1以上で指定してください")
    run = RunRepository(session).start(
        trigger=trigger,
        kind="discover",
        playlist_id=playlist_id,
        commit=False,
    )
    task = TaskRepository(session).enqueue(
        task_type=TaskType.DISCOVER,
        target_ref_type="playlist",
        target_ref_id=playlist_id,
        payload={"playlist_id": playlist_id, "dry_run": dry_run},
        worker_class=WorkerClass.NETWORK,
        max_attempts=attempts,
        run_id=run.id,
        commit=False,
    )
    session.commit()
    session.refresh(run)
    session.refresh(task)
    return run, task


def execute_download_run(
    session: Session,
    playlist_id: int,
    *,
    trigger: RunTrigger = RunTrigger.MANUAL,
    max_targets: int | None = None,
    max_attempts: int | None = None,
    adapter_factory: AdapterFactory = create_storage_adapter,
) -> Run:
    """download Runを作成し、チェーン投入完了時点で統計と成否を確定する。"""
    run = RunRepository(session).start(
        trigger=trigger,
        kind="download",
        playlist_id=playlist_id,
    )
    try:
        stats = queue_download_phase(
            session,
            playlist_id,
            run_id=run.id,
            max_targets=max_targets,
            max_attempts=max_attempts,
            adapter_factory=adapter_factory,
        )
        status = RunStatus.SUCCEEDED
        # Runは過去の累積成否ではなく今回の投入を表す。既存downloadedがあっても、
        # 今回の候補がStorage事前確認で全てblockedなら全件失敗である。
        if stats.targets_queued == 0 and stats.blocked > 0:
            status = RunStatus.FAILED
        RunRepository(session).finish(run.id, status, stats.to_dict())
    except Exception:
        session.rollback()
        RunRepository(session).finish(run.id, RunStatus.FAILED, SyncStats().to_dict())
        raise
    session.refresh(run)
    return run


def parse_discover_entries(lines: list[str]) -> list[dict[str, Any]]:
    """yt-dlp の flat-playlist JSON 行を Item 用の値へ正規化する。

    壊れた行や、再取得に必要な ID / HTTP(S) URL を欠く行は採用しない。
    同じ source_id が複数回現れた場合は最後のメタデータを採用する。
    """
    entries: dict[str, dict[str, Any]] = {}
    for line in lines:
        try:
            raw = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(raw, dict):
            continue
        source_id = _text(raw.get("id"))
        source_url = _source_url(raw)
        if source_id is None or source_url is None:
            continue
        entries[source_id] = {
            "source_id": source_id,
            "source_url": source_url,
            "title": _text(raw.get("title")),
            "uploader": _text(raw.get("uploader")),
            "duration": _integer(raw.get("duration")),
            "upload_date": _text(raw.get("upload_date")),
            "playlist_index": _integer(raw.get("playlist_index")),
            "metadata_json": raw,
        }
    return list(entries.values())


def apply_discovery(
    session: Session,
    playlist_id: int,
    entries: list[dict[str, Any]],
    *,
    dry_run: bool = False,
    now: datetime | None = None,
) -> SyncStats:
    """discover 差分を適用する。空結果と dry-run はドメインDBを変更しない。"""
    observed_at = now or datetime.now(UTC)
    # 呼び出し側が直接組み立てたテスト入力にも重複が入り得るため、ここでも畳む。
    discovered = {entry["source_id"]: entry for entry in entries if entry.get("source_id")}
    if not discovered:
        return SyncStats(empty_result=True)

    item_repo = ItemRepository(session)
    existing_items = item_repo.list_for_playlist(playlist_id)
    existing = {item.source_id: item for item in existing_items}
    discovered_ids = set(discovered)
    new_source_ids = discovered_ids - set(existing)
    delisted = [
        item
        for item in existing_items
        if item.membership == ItemMembership.ACTIVE and item.source_id not in discovered_ids
    ]
    reappeared = [
        item
        for item in existing_items
        if item.membership == ItemMembership.DELISTED and item.source_id in discovered_ids
    ]
    enabled_profiles = PlaylistProfileRepository(session).list_enabled_for_playlist(playlist_id)
    stats = SyncStats(
        new_items=len(new_source_ids),
        delisted_items=len(delisted),
    )
    if dry_run:
        return stats

    rows: list[dict[str, Any]] = []
    for entry in discovered.values():
        row = dict(entry)
        row["last_seen_at"] = observed_at
        rows.append(row)
    upserted = item_repo.upsert_many(playlist_id, rows, commit=False)
    for item in delisted:
        transition_item(session, item.id, ItemMembership.DELISTED, now=observed_at, commit=False)
    for item in reappeared:
        transition_item(session, item.id, ItemMembership.ACTIVE, now=observed_at, commit=False)

    new_item_ids = [item.id for item in upserted if item.source_id in new_source_ids]
    TargetRepository(session).create_missing_for_items(
        new_item_ids,
        [profile.id for profile in enabled_profiles],
        commit=False,
    )
    if not PlaylistRepository(session).set_last_discover_at(playlist_id, observed_at, commit=False):
        session.rollback()
        raise LookupError(f"Playlist {playlist_id} が見つかりません")
    session.commit()
    return stats


def queue_download_phase(
    session: Session,
    playlist_id: int,
    *,
    run_id: int | None = None,
    max_targets: int | None = None,
    max_attempts: int | None = None,
    adapter_factory: AdapterFactory = create_storage_adapter,
    now: datetime | None = None,
) -> SyncStats:
    """Storageを事前確認し、取得可能なTargetへ上限付きでチェーンを投入する。"""
    queued_at = now or datetime.now(UTC)
    playlist = PlaylistRepository(session).get(playlist_id)
    if playlist is None:
        raise LookupError(f"Playlist {playlist_id} が見つかりません")
    if not playlist.enabled or playlist.paused:
        raise ValueError("無効または一時停止中のPlaylistは同期できません")

    settings = OperationalSettings(session)
    target_limit = settings.sync_max_targets_per_run if max_targets is None else max_targets
    retry_limit = settings.worker_max_attempts if max_attempts is None else max_attempts
    if target_limit < 1 or retry_limit < 1:
        raise ValueError("同期上限と再試行上限は1以上で指定してください")

    candidates = TargetRepository(session).list_download_candidates(
        playlist_id, retry_limit=retry_limit
    )
    storage_checks: dict[int, tuple[bool, str | None]] = {}
    eligible_ids: list[int] = []
    for target, _item, _assignment, storage in candidates:
        check = storage_checks.get(storage.id)
        if check is None:
            check = _check_storage(storage, settings, adapter_factory)
            storage_checks[storage.id] = check
        usable, reason = check
        if not usable:
            transition_target(
                session,
                target.id,
                TargetStatus.BLOCKED,
                error=reason,
                blocked_reason=reason,
            )
            continue
        if target.status in {TargetStatus.FAILED, TargetStatus.BLOCKED}:
            transition_target(session, target.id, TargetStatus.PENDING)
        eligible_ids.append(target.id)

    queued = 0
    remaining = 0
    for target_id in eligible_ids:
        if queued >= target_limit:
            remaining += 1
            continue
        chain = enqueue_target_pipeline(
            session,
            target_id,
            run_id=run_id,
            max_attempts=retry_limit,
        )
        if chain is not None:
            queued += 1

    PlaylistRepository(session).set_last_download_at(playlist_id, queued_at)
    counts = TargetRepository(session).count_by_status(playlist_id)
    return SyncStats(
        targets_queued=queued,
        targets_remaining=remaining,
        downloaded=counts.get(TargetStatus.DOWNLOADED, 0),
        failed=counts.get(TargetStatus.FAILED, 0),
        blocked=counts.get(TargetStatus.BLOCKED, 0),
    )


def _check_storage(
    storage: Storage,
    settings: OperationalSettings,
    adapter_factory: AdapterFactory,
) -> tuple[bool, str | None]:
    if not storage.enabled:
        return False, "Storageが無効です"
    try:
        adapter = adapter_factory(storage, settings)
        connection = adapter.test_connection()
        if not connection.ok:
            failure = next(
                (stage for stage in connection.stages if stage.status.value == "failed"), None
            )
            if failure is not None:
                return (
                    False,
                    f"Storage事前確認失敗: {failure.stage.value}/{failure.reason_code}",
                )
            return False, "Storage事前確認の後始末に失敗しました"
        capacity = evaluate_capacity(
            adapter.free_space(),
            warn_bytes=settings.storage_free_space_warn_bytes,
            stop_bytes=settings.storage_free_space_stop_bytes,
        )
        if capacity.should_block:
            return False, "Storageの空き容量が停止閾値を下回っています"
    except Exception as exc:  # noqa: BLE001 - adapter生成・外部I/O失敗はTarget blockedへ集約
        return False, f"Storage事前確認を実行できません: {type(exc).__name__}"
    return True, None


def _source_url(raw: dict[str, Any]) -> str | None:
    for key in ("webpage_url", "original_url", "url"):
        value = _text(raw.get(key))
        if value is None:
            continue
        try:
            parsed = urlsplit(value)
        except ValueError:
            continue
        if parsed.scheme.lower() in {"http", "https"} and parsed.hostname:
            return value
    return None


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value and value != "NA" else None


def _integer(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return round(value)
    return None


__all__ = [
    "SyncStats",
    "apply_discovery",
    "enqueue_discover_run",
    "execute_download_run",
    "parse_discover_entries",
    "queue_download_phase",
]
