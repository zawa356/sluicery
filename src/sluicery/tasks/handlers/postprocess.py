"""空の後処理チェーンを素通しするpostprocess Taskハンドラ。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sluicery.db.models import PlaylistProfile, Profile, Target
from sluicery.tasks.handlers.dummy import ProgressCallback
from sluicery.tasks.pipeline import dependency_payload, execution_task_id
from sluicery.tasks.queue import TaskOutcome, TaskResult


class PostprocessHandler:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def cancel(self) -> None:
        pass

    def run(self, payload: dict, on_progress: ProgressCallback) -> TaskResult:
        target_id = payload.get("target_id")
        if not isinstance(target_id, int):
            return TaskResult(TaskOutcome.FAILED, "postprocess payloadが不正です")
        with self._session_factory() as session:
            previous = dependency_payload(session, execution_task_id(payload))
            chain = session.scalar(
                select(Profile.postprocess_chain_json)
                .join(PlaylistProfile, Profile.id == PlaylistProfile.profile_id)
                .join(Target, PlaylistProfile.id == Target.playlist_profile_id)
                .where(Target.id == target_id)
            )
        if chain not in (None, [], {}):
            return TaskResult(
                TaskOutcome.UNAVAILABLE,
                "postprocess_chain_jsonの実処理は現バージョンでは未実装です",
            )
        on_progress({"status": "passthrough", "percent": 100.0})
        return TaskResult(TaskOutcome.SUCCEEDED, payload_update=previous)


__all__ = ["PostprocessHandler"]
