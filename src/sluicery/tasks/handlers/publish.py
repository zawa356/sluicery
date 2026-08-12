"""検証済みStaging成果物をStorageへ配置するpublish Taskハンドラ。"""

from __future__ import annotations

from pathlib import Path, PurePosixPath
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sluicery.core.settings import OperationalSettings
from sluicery.db.models import PlaylistProfile, Storage, Target, TargetStatus
from sluicery.db.repositories.target import TargetRepository
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

    def cancel(self) -> None:
        adapter = self._adapter
        runner = getattr(adapter, "_runner", None)
        if runner is not None and hasattr(runner, "cancel"):
            runner.cancel()

    def run(self, payload: dict, on_progress: ProgressCallback) -> TaskResult:
        target_id = payload.get("target_id")
        work_id = payload.get("work_id")
        if not isinstance(target_id, int) or not isinstance(work_id, str):
            return TaskResult(TaskOutcome.FAILED, "publish payloadが不正です")
        with self._session_factory() as session:
            previous = dependency_payload(session, execution_task_id(payload))
            storage = session.scalar(
                select(Storage)
                .join(PlaylistProfile, Storage.id == PlaylistProfile.storage_id)
                .join(Target, PlaylistProfile.id == Target.playlist_profile_id)
                .where(Target.id == target_id)
            )
            if storage is None or not storage.enabled:
                return self._failure(
                    target_id,
                    StorageClassification.UNREACHABLE,
                    "出力先Storageが無効、または見つかりません",
                )
            ops = OperationalSettings(session)
            adapter = self._adapter_factory(storage, ops)
            self._adapter = adapter
            warn_bytes = ops.storage_free_space_warn_bytes
            stop_bytes = ops.storage_free_space_stop_bytes

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
            result = adapter.publish(resolved, destination)
        except StorageOperationError as exc:
            return self._failure(target_id, exc.classification, str(exc))
        finally:
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
            TargetRepository(session).compare_and_set_status(
                target_id,
                {
                    TargetStatus.DOWNLOADING,
                    TargetStatus.PROCESSING,
                    TargetStatus.FAILED,
                    TargetStatus.BLOCKED,
                },
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


__all__ = ["PublishHandler"]
