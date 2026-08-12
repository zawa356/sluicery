"""ffprobeでStaging成果物を検証するverify Taskハンドラ。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from sluicery.db.models import TargetStatus
from sluicery.db.repositories.target import TargetRepository
from sluicery.runner.ffprobe import FFprobeRunner
from sluicery.tasks.handlers.dummy import ProgressCallback
from sluicery.tasks.pipeline import dependency_payload, execution_task_id
from sluicery.tasks.queue import TaskOutcome, TaskResult


class VerifyHandler:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        runner: FFprobeRunner,
        timeout_sec: int = 60,
    ) -> None:
        self._session_factory = session_factory
        self._runner = runner
        self._timeout_sec = timeout_sec

    def cancel(self) -> None:
        self._runner.cancel()

    def run(self, payload: dict, on_progress: ProgressCallback) -> TaskResult:
        target_id = payload.get("target_id")
        if not isinstance(target_id, int):
            return TaskResult(TaskOutcome.FAILED, "verify payloadが不正です")
        with self._session_factory() as session:
            previous = dependency_payload(session, execution_task_id(payload))
            TargetRepository(session).compare_and_set_status(
                target_id,
                {TargetStatus.DOWNLOADING, TargetStatus.PROCESSING, TargetStatus.FAILED},
                TargetStatus.PROCESSING,
            )
        raw_path = previous.get("file_path")
        if not isinstance(raw_path, str):
            return self._failed(target_id, "download結果にfile_pathがありません")
        file_path = Path(raw_path)
        try:
            size = file_path.stat().st_size
        except OSError as exc:
            return self._failed(target_id, f"ファイルが存在しません: {exc}")
        if not file_path.is_file() or size <= 0:
            return self._failed(target_id, "ファイルが空、または通常ファイルではありません")

        on_progress({"status": "probing", "percent": 0.0})
        probe = self._runner.probe(file_path, timeout_sec=self._timeout_sec)
        if probe.terminated_by == "cancel":
            return TaskResult(TaskOutcome.CANCELLED)
        if probe.returncode != 0:
            return self._failed(target_id, probe.stderr_tail or "ffprobeが失敗しました")
        if probe.metadata is None:
            return self._failed(target_id, "ffprobeのJSONを解析できません")
        try:
            media = _extract_metadata(probe.metadata)
        except (TypeError, ValueError) as exc:
            return self._failed(target_id, f"ffprobeメタデータが不正です: {exc}")
        on_progress({"status": "verified", "percent": 100.0})
        return TaskResult(
            TaskOutcome.SUCCEEDED,
            payload_update={
                **previous,
                **media,
                "filesize": size,
                "verified_at": datetime.now(UTC).isoformat(),
            },
        )

    def _failed(self, target_id: int, message: str) -> TaskResult:
        error = message[-4000:]
        with self._session_factory() as session:
            TargetRepository(session).compare_and_set_status(
                target_id,
                {TargetStatus.DOWNLOADING, TargetStatus.PROCESSING, TargetStatus.FAILED},
                TargetStatus.FAILED,
                error=error,
                increment_retry=True,
            )
        return TaskResult(
            TaskOutcome.FAILED,
            error,
            payload_update={"verify_error": error},
        )


def _extract_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    format_data = metadata.get("format")
    streams = metadata.get("streams")
    if not isinstance(format_data, dict) or not isinstance(streams, list):
        raise ValueError("formatまたはstreamsがありません")
    duration = _duration_seconds(format_data.get("duration"))
    video_codec = None
    audio_codec = None
    format_id = None
    for stream in streams:
        if not isinstance(stream, dict):
            continue
        codec_type = stream.get("codec_type")
        codec_name = stream.get("codec_name")
        if codec_type == "video" and video_codec is None and isinstance(codec_name, str):
            video_codec = codec_name
        if codec_type == "audio" and audio_codec is None and isinstance(codec_name, str):
            audio_codec = codec_name
        if format_id is None and isinstance(stream.get("index"), int):
            format_id = str(stream["index"])
    container = format_data.get("format_name")
    return {
        "container": container if isinstance(container, str) else None,
        "format_id": format_id,
        "video_codec": video_codec,
        "audio_codec": audio_codec,
        "duration": duration,
    }


def _duration_seconds(value: object) -> int | None:
    if value in (None, "N/A"):
        return None
    duration = float(value)  # type: ignore[arg-type]
    if duration < 0:
        raise ValueError("durationが負です")
    return round(duration)


__all__ = ["VerifyHandler"]
