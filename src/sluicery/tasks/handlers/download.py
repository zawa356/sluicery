"""単一TargetをStagingへ取得するdownload Taskハンドラ。"""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sluicery.core.options import build_download_args
from sluicery.db.models import Item, Playlist, PlaylistProfile, Profile, Target, TargetStatus
from sluicery.db.repositories.target import TargetRepository
from sluicery.downloader.errors import Classification
from sluicery.downloader.progress import ProgressEvent
from sluicery.downloader.ytdlp import TimeoutPolicy, YtdlpRunner
from sluicery.tasks.handlers.dummy import ProgressCallback
from sluicery.tasks.queue import TaskOutcome, TaskResult


class DownloadHandler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        staging_dir: Path,
        runner: YtdlpRunner,
        env_allow_exec: bool = False,
    ) -> None:
        self._session_factory = session_factory
        self._staging_dir = staging_dir
        self._runner = runner
        self._env_allow_exec = env_allow_exec

    def cancel(self) -> None:
        self._runner.cancel()

    def run(self, payload: dict, on_progress: ProgressCallback) -> TaskResult:
        target_id = payload.get("target_id")
        work_id = payload.get("work_id")
        if not isinstance(target_id, int) or not isinstance(work_id, str) or not work_id:
            return TaskResult(TaskOutcome.FAILED, "download payloadが不正です")

        with self._session_factory() as session:
            graph = _load_target_graph(session, target_id)
            if graph is None:
                return TaskResult(TaskOutcome.UNAVAILABLE, f"Target {target_id} が見つかりません")
            target, item, playlist_profile, profile, playlist = graph
            if not TargetRepository(session).compare_and_set_status(
                target_id,
                {
                    TargetStatus.PENDING,
                    TargetStatus.QUEUED,
                    TargetStatus.FAILED,
                    TargetStatus.DOWNLOADING,
                },
                TargetStatus.DOWNLOADING,
            ):
                return TaskResult(TaskOutcome.FAILED, "Targetをdownloadingへ遷移できません")
            built = build_download_args(
                target,
                source_url=item.source_url,
                session=session,
                staging_dir=self._staging_dir,
                work_id=work_id,
                playlist=playlist,
                profile=profile,
                playlist_profile=playlist_profile,
                env_allow_exec=self._env_allow_exec,
            )

        def report(event: ProgressEvent) -> None:
            percent = None
            if event.total_bytes and event.downloaded_bytes is not None:
                percent = min(100.0, event.downloaded_bytes / event.total_bytes * 100)
            on_progress(
                {
                    "status": event.status,
                    "percent": percent,
                    "downloaded_bytes": event.downloaded_bytes,
                    "total_bytes": event.total_bytes,
                    "speed": event.speed,
                    "eta": event.eta,
                }
            )

        result = self._runner.run(
            list(built.args),
            timeout=TimeoutPolicy(
                idle_sec=built.timeout.idle_sec,
                absolute_sec=built.timeout.absolute_sec,
                term_grace_sec=built.timeout.term_grace_sec,
            ),
            on_progress=report,
            sensitive_values=(item.source_url,),
        )
        if result.terminated_by == "cancel":
            return TaskResult(TaskOutcome.CANCELLED)
        if result.classification == Classification.OK:
            return self._success(target_id, work_id, result.stdout_lines)
        return self._failure(target_id, result.classification, result.stderr_tail)

    def _success(self, target_id: int, work_id: str, output_lines: list[str]) -> TaskResult:
        paths = [Path(line) for line in output_lines if line.strip()]
        if not paths:
            return self._failure(
                target_id, Classification.FAILED, "生成ファイルパスを取得できません"
            )
        file_path = paths[-1]
        work_root = (self._staging_dir / work_id).resolve()
        try:
            resolved = file_path.resolve(strict=True)
        except OSError as exc:
            return self._failure(
                target_id, Classification.FAILED, f"生成ファイルがありません: {exc}"
            )
        if not resolved.is_file() or not resolved.is_relative_to(work_root):
            return self._failure(
                target_id,
                Classification.FAILED,
                "生成ファイルがStagingのwork-id境界外です",
            )
        return TaskResult(
            TaskOutcome.SUCCEEDED,
            payload_update={
                "file_path": str(resolved),
                "filesize": resolved.stat().st_size,
            },
        )

    def _failure(self, target_id: int, classification: Classification, message: str) -> TaskResult:
        mapping = {
            Classification.FAILED: (TaskOutcome.FAILED, TargetStatus.FAILED, True),
            Classification.UNAVAILABLE: (
                TaskOutcome.UNAVAILABLE,
                TargetStatus.UNAVAILABLE,
                False,
            ),
            Classification.BLOCKED: (TaskOutcome.BLOCKED, TargetStatus.BLOCKED, False),
        }
        outcome, target_status, increment_retry = mapping[classification]
        error = message[-4000:] if message else classification.value
        with self._session_factory() as session:
            TargetRepository(session).compare_and_set_status(
                target_id,
                {
                    TargetStatus.PENDING,
                    TargetStatus.QUEUED,
                    TargetStatus.DOWNLOADING,
                    TargetStatus.FAILED,
                    TargetStatus.BLOCKED,
                },
                target_status,
                error=error,
                blocked_reason=error if target_status == TargetStatus.BLOCKED else None,
                increment_retry=increment_retry,
            )
        return TaskResult(outcome, error)


def _load_target_graph(
    session: Session, target_id: int
) -> tuple[Target, Item, PlaylistProfile, Profile, Playlist] | None:
    stmt = (
        select(Target, Item, PlaylistProfile, Profile, Playlist)
        .join(Item, Target.item_id == Item.id)
        .join(PlaylistProfile, Target.playlist_profile_id == PlaylistProfile.id)
        .join(Profile, PlaylistProfile.profile_id == Profile.id)
        .join(Playlist, Item.playlist_id == Playlist.id)
        .where(Target.id == target_id)
    )
    row = session.execute(stmt).one_or_none()
    return tuple(row) if row is not None else None


__all__ = ["DownloadHandler"]
