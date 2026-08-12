"""publish済み成果物をArtifactとして確定するindex Taskハンドラ。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from sluicery.db.models import ArtifactRole, TargetStatus
from sluicery.db.repositories.artifact import ArtifactRepository
from sluicery.db.repositories.target import TargetRepository
from sluicery.hooks import EventLogHook, Hook, emit_safely
from sluicery.tasks.handlers.dummy import ProgressCallback
from sluicery.tasks.pipeline import dependency_payload, execution_task_id
from sluicery.tasks.queue import TaskOutcome, TaskResult


class IndexHandler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        staging_dir: Path,
        delete_staging: bool = True,
        hook: Hook | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._staging_dir = staging_dir
        self._delete_staging = delete_staging
        self._hook = hook or EventLogHook(session_factory)

    def cancel(self) -> None:
        pass

    def run(self, payload: dict, on_progress: ProgressCallback) -> TaskResult:
        target_id = payload.get("target_id")
        work_id = payload.get("work_id")
        if not isinstance(target_id, int) or not isinstance(work_id, str):
            return TaskResult(TaskOutcome.FAILED, "index payloadが不正です")
        task_id = execution_task_id(payload)
        with self._session_factory() as session:
            previous = dependency_payload(session, task_id)
            storage_id = previous.get("storage_id")
            relative_path = previous.get("relative_path")
            raw_source = previous.get("file_path")
            if (
                not isinstance(storage_id, int)
                or not isinstance(relative_path, str)
                or not isinstance(raw_source, str)
            ):
                return TaskResult(TaskOutcome.FAILED, "publish結果が不足しています")
            verified_at = _parse_datetime(previous.get("verified_at"))
            artifact = ArtifactRepository(session).create_source_if_missing(
                target_id=target_id,
                role=ArtifactRole.SOURCE,
                storage_id=storage_id,
                relative_path=relative_path,
                container=_text(previous.get("container")),
                format_id=_text(previous.get("format_id")),
                video_codec=_text(previous.get("video_codec")),
                audio_codec=_text(previous.get("audio_codec")),
                filesize=_integer(previous.get("filesize")),
                duration=_integer(previous.get("duration")),
                checksum=None,
                produced_by_task_id=task_id,
                verified_at=verified_at,
            )
            changed = TargetRepository(session).compare_and_set_status(
                target_id,
                {TargetStatus.PROCESSING, TargetStatus.DOWNLOADED},
                TargetStatus.DOWNLOADED,
                extra_values={"downloaded_at": datetime.now(UTC)},
            )
            if not changed:
                return TaskResult(TaskOutcome.FAILED, "Targetをdownloadedへ遷移できません")

        staging_deleted = False
        source = Path(raw_source)
        work_root = (self._staging_dir / work_id).resolve()
        if self._delete_staging:
            try:
                resolved = source.resolve(strict=True)
                if resolved.is_relative_to(work_root) and resolved.is_file():
                    resolved.unlink()
                    staging_deleted = True
            except OSError:
                # ArtifactとTargetの確定後なのでcleanup失敗は本体成功を覆さない。
                staging_deleted = False
        event_payload = {
            "target_id": target_id,
            "artifact_id": artifact.id,
            "storage_id": storage_id,
            "relative_path": relative_path,
        }
        emit_safely(self._hook, "target_downloaded", event_payload)
        emit_safely(self._hook, "artifact_published", event_payload)
        on_progress({"status": "indexed", "percent": 100.0})
        return TaskResult(
            TaskOutcome.SUCCEEDED,
            payload_update={
                **previous,
                "artifact_id": artifact.id,
                "staging_deleted": staging_deleted,
            },
        )


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


__all__ = ["IndexHandler"]
