"""検証済みStaging成果物をStorageへ配置するpublish Taskハンドラ。"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path, PurePosixPath
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sluicery.core.settings import OperationalSettings
from sluicery.core.target_state import advance_target, transition_target
from sluicery.db.models import (
    Artifact,
    Item,
    Playlist,
    PlaylistProfile,
    Storage,
    Target,
    TargetStatus,
)
from sluicery.storage import create_storage_adapter
from sluicery.storage.base import (
    StorageAdapter,
    StorageOperationError,
    evaluate_capacity,
)
from sluicery.storage.errors import StorageClassification
from sluicery.tasks.handlers.dummy import ProgressCallback
from sluicery.tasks.pipeline import dependency_payload, execution_task_id
from sluicery.tasks.queue import TaskOutcome, TaskResult


class AdapterFactory(Protocol):
    def __call__(self, storage: Storage, settings: OperationalSettings) -> StorageAdapter: ...


class PublishHandler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        staging_dir: Path,
        adapter_factory: AdapterFactory = create_storage_adapter,
    ) -> None:
        self._session_factory = session_factory
        self._staging_dir = staging_dir
        self._adapter_factory = adapter_factory
        self._adapter: StorageAdapter | None = None
        self._log_paths: list[Path] = []
        self._observed_runner: object | None = None

    def cancel(self) -> None:
        adapter = self._adapter
        runner = getattr(adapter, "_runner", None)
        if runner is not None and hasattr(runner, "cancel"):
            runner.cancel()

    @property
    def log_paths(self) -> tuple[Path, ...]:
        current = getattr(self._observed_runner, "log_paths", ())
        return tuple(dict.fromkeys([*self._log_paths, *current]))

    def run(self, payload: dict, on_progress: ProgressCallback) -> TaskResult:
        target_id = payload.get("target_id")
        work_id = payload.get("work_id")
        if not isinstance(target_id, int) or not isinstance(work_id, str):
            return TaskResult(TaskOutcome.FAILED, "publish payloadが不正です")
        with self._session_factory() as session:
            previous = dependency_payload(session, execution_task_id(payload))
            graph = session.execute(
                select(Storage, PlaylistProfile, Playlist, Item)
                .join(PlaylistProfile, Storage.id == PlaylistProfile.storage_id)
                .join(Target, PlaylistProfile.id == Target.playlist_profile_id)
                .join(Item, Target.item_id == Item.id)
                .join(Playlist, Item.playlist_id == Playlist.id)
                .where(Target.id == target_id)
            ).one_or_none()
            if graph is None or not graph[0].enabled:
                return self._failure(
                    target_id,
                    StorageClassification.UNREACHABLE,
                    "出力先Storageが無効、または見つかりません",
                )
            storage, assignment, playlist, item = graph
            dedup_source = None
            dedup_source_storage_id: int | None = None
            dedup_source_adapter: StorageAdapter | None = None
            if playlist.dedup_hardlink:
                dedup_row = session.execute(
                    select(Artifact.relative_path, Artifact.storage_id)
                    .join(Target, Artifact.target_id == Target.id)
                    .join(Item, Target.item_id == Item.id)
                    .join(
                        PlaylistProfile,
                        Target.playlist_profile_id == PlaylistProfile.id,
                    )
                    .where(
                        Item.source_id == item.source_id,
                        PlaylistProfile.profile_id == assignment.profile_id,
                        Artifact.missing_since.is_(None),
                        Target.id != target_id,
                    )
                    .order_by(Artifact.id)
                    .limit(1)
                ).first()
                if dedup_row is not None:
                    dedup_source = dedup_row.relative_path
                    dedup_source_storage_id = int(dedup_row.storage_id)
            ops = OperationalSettings(session)
            adapter = self._adapter_factory(storage, ops)
            if dedup_source is not None and dedup_source_storage_id is not None:
                if dedup_source_storage_id == storage.id:
                    dedup_source_adapter = adapter
                else:
                    source_storage = session.get(Storage, dedup_source_storage_id)
                    if source_storage is not None and source_storage.enabled:
                        dedup_source_adapter = self._adapter_factory(source_storage, ops)
            self._adapter = adapter
            self._observed_runner = getattr(adapter, "_runner", None)
            warn_bytes = ops.storage_free_space_warn_bytes
            stop_bytes = ops.storage_free_space_stop_bytes
            try:
                advance_target(session, target_id, TargetStatus.PROCESSING)
            except (LookupError, ValueError):
                return TaskResult(TaskOutcome.FAILED, "Targetをprocessingへ遷移できません")

        raw_path = previous.get("file_path")
        if not isinstance(raw_path, str):
            return self._failure(
                target_id, StorageClassification.FAILED, "postprocess結果にfile_pathがありません"
            )
        source = Path(raw_path)
        work_root = (self._staging_dir / work_id).resolve()
        try:
            resolved = source.resolve(strict=True)
        except OSError as exc:
            return self._failure(target_id, StorageClassification.FAILED, str(exc))
        if not resolved.is_relative_to(work_root):
            return self._failure(
                target_id, StorageClassification.FAILED, "publish元がwork-id境界外です"
            )
        destination = resolved.relative_to(work_root).as_posix()

        try:
            capacity = evaluate_capacity(
                adapter.free_space(), warn_bytes=warn_bytes, stop_bytes=stop_bytes
            )
            if capacity.should_block:
                return self._failure(
                    target_id,
                    StorageClassification.NO_SPACE,
                    "Storageの空き容量が停止閾値を下回っています",
                )
            source_size = resolved.stat().st_size
            source_sha256 = _sha256(resolved) if dedup_source is not None else None
            existing_size = _existing_size(adapter, destination)
            if existing_size is not None:
                if existing_size != source_size:
                    return self._failure(
                        target_id,
                        StorageClassification.FAILED,
                        "同名の保存先ファイルが期待サイズと一致しません",
                    )
                return TaskResult(
                    TaskOutcome.SUCCEEDED,
                    payload_update={
                        **previous,
                        "storage_id": storage.id,
                        "relative_path": destination,
                        "publish_size": existing_size,
                        "publish_resumed": True,
                    },
                )
            on_progress({"status": "publishing", "percent": 0.0})
            if dedup_source is not None and dedup_source_adapter is not None:
                try:
                    existing = dedup_source_adapter.inspect_file(dedup_source)
                    hardlinked = (
                        existing.size == source_size
                        and source_sha256 is not None
                        and existing.hashes.get("sha256") == source_sha256
                        and adapter.hardlink_from(
                            dedup_source_adapter,
                            dedup_source,
                            destination,
                            expected=existing,
                        )
                    )
                except StorageOperationError:
                    hardlinked = False
                if hardlinked:
                    self._record_dedup(
                        work_root,
                        f"hardlink created: {dedup_source} -> {destination}",
                    )
                    on_progress({"status": "published", "percent": 100.0})
                    return TaskResult(
                        TaskOutcome.SUCCEEDED,
                        payload_update={
                            **previous,
                            "storage_id": storage.id,
                            "relative_path": destination,
                            "publish_size": source_size,
                            "publish_resumed": False,
                            "publish_hardlinked": True,
                        },
                    )
                self._record_dedup(
                    work_root,
                    "hardlink unavailable; falling back to normal publish",
                )
            result = adapter.publish(resolved, destination)
        except StorageOperationError as exc:
            return self._failure(target_id, exc.classification, str(exc))
        finally:
            runner = getattr(adapter, "_runner", None)
            self._log_paths.extend(getattr(runner, "log_paths", ()))
            self._adapter = None
        if not result.success:
            return self._failure(target_id, result.classification, result.message)
        if not adapter.exists(destination):
            return self._failure(
                target_id,
                StorageClassification.FAILED,
                "publish成功後に最終保存先を確認できません",
            )
        on_progress({"status": "published", "percent": 100.0})
        return TaskResult(
            TaskOutcome.SUCCEEDED,
            payload_update={
                **previous,
                "storage_id": storage.id,
                "relative_path": result.dest_rel,
                "publish_size": result.size,
                "publish_resumed": False,
            },
        )

    def _record_dedup(self, work_root: Path, message: str) -> None:
        log_path = work_root / "dedup-hardlink.log"
        with log_path.open("a", encoding="utf-8") as output:
            output.write(message + "\n")
            output.flush()
            os.fsync(output.fileno())
        self._log_paths.append(log_path)

    def _failure(
        self,
        target_id: int,
        classification: StorageClassification,
        message: str,
    ) -> TaskResult:
        blocked = classification in {
            StorageClassification.UNREACHABLE,
            StorageClassification.NO_SPACE,
        }
        outcome = TaskOutcome.BLOCKED if blocked else TaskOutcome.FAILED
        status = TargetStatus.BLOCKED if blocked else TargetStatus.FAILED
        error = message[-4000:]
        with self._session_factory() as session:
            transition_target(
                session,
                target_id,
                status,
                error=error,
                blocked_reason=error if blocked else None,
                increment_retry=not blocked,
            )
        return TaskResult(outcome, error)


def _existing_size(adapter: StorageAdapter, destination: str) -> int | None:
    if not adapter.exists(destination):
        return None
    parent = PurePosixPath(destination).parent.as_posix()
    base = "" if parent == "." else parent
    for entry in adapter.list_recursive(base):
        if entry.relative_path == destination:
            return entry.size
    raise StorageOperationError(
        "保存先は存在しますがサイズを取得できません",
        classification=StorageClassification.FAILED,
        reason_code="existing_size_unknown",
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["PublishHandler"]
