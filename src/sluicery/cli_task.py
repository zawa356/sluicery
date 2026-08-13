"""Phase 7までの暫定Task検証CLI。Web UIの実行管理へ置換予定。"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from typing import Any

from sluicery.core.settings import OperationalSettings
from sluicery.core.target_state import sync_target_after_task
from sluicery.db.models import Task, TaskStatus, TaskType, WorkerClass
from sluicery.db.repositories.task import TaskRepository
from sluicery.tasks.handlers import DUMMY_HANDLER_FACTORIES


def configure_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("task", help="Phase 6のTaskキューを検証する（暫定）")
    commands = parser.add_subparsers(dest="task_command", required=True)

    enqueue = commands.add_parser("enqueue", help="検証用ダミーTaskを投入する")
    enqueue.add_argument("task_type", choices=sorted(DUMMY_HANDLER_FACTORIES))
    enqueue.add_argument(
        "--worker-class", choices=[item.value for item in WorkerClass], default="network"
    )
    enqueue.add_argument("--priority", type=int, default=0)
    enqueue.add_argument("--payload", default="{}", help="JSON object")

    list_parser = commands.add_parser("list", help="Task一覧を表示する")
    list_parser.add_argument("--status", choices=[item.value for item in TaskStatus])
    list_parser.add_argument("--worker-class", choices=[item.value for item in WorkerClass])

    for name, help_text in (
        ("show", "Task詳細を表示する"),
        ("cancel", "Taskをキャンセルする"),
        ("retry", "Taskを手動でpendingへ戻す"),
    ):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("task_id", type=int)


def _json_payload(raw: str) -> dict[str, Any]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"payloadが不正なJSONです: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("payloadはJSON objectで指定してください")
    return value


def _format_datetime(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _as_dict(task: Task) -> dict[str, Any]:
    return {
        "id": task.id,
        "type": task.type.value,
        "worker_class": task.worker_class.value,
        "priority": task.priority,
        "status": task.status.value,
        "attempts": task.attempts,
        "max_attempts": task.max_attempts,
        "depends_on_task_id": task.depends_on_task_id,
        "available_at": _format_datetime(task.available_at),
        "blocked_until": _format_datetime(task.blocked_until),
        "blocked_reason": task.blocked_reason,
        "worker_id": task.worker_id,
        "heartbeat_at": _format_datetime(task.heartbeat_at),
        "cancel_requested": task.cancel_requested,
        "started_at": _format_datetime(task.started_at),
        "finished_at": _format_datetime(task.finished_at),
        "error_message": task.error_message,
        "payload": task.payload_json,
    }


def dispatch(args: argparse.Namespace, *, open_session) -> int | None:
    if args.command != "task":
        return None
    session = open_session()
    try:
        repo = TaskRepository(session)
        if args.task_command == "enqueue":
            if not OperationalSettings(session).worker_enable_test_tasks:
                print(
                    "ERROR: 検証用Taskは無効です。"
                    "`sluicery settings set worker.enable_test_tasks true` 後に"
                    "workerを再起動してください。",
                    file=sys.stderr,
                )
                return 1
            try:
                payload = _json_payload(args.payload)
            except ValueError as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 1
            created_task = repo.create(
                type=TaskType(args.task_type),
                target_ref_type="phase6_test",
                target_ref_id=0,
                payload_json=payload,
                worker_class=WorkerClass(args.worker_class),
                priority=args.priority,
                status=TaskStatus.QUEUED,
                max_attempts=OperationalSettings(session).worker_max_attempts,
            )
            print(created_task.id)
            return 0
        if args.task_command == "list":
            tasks = repo.list_filtered(
                status=TaskStatus(args.status) if args.status else None,
                worker_class=WorkerClass(args.worker_class) if args.worker_class else None,
            )
            for task in tasks:
                print(
                    f"{task.id}\t{task.type.value}\t{task.worker_class.value}\t"
                    f"{task.status.value}\tattempts={task.attempts}/{task.max_attempts}"
                )
            return 0
        selected_task = repo.get(args.task_id)
        if selected_task is None:
            print(f"ERROR: Task {args.task_id} は存在しません", file=sys.stderr)
            return 1
        if args.task_command == "show":
            print(json.dumps(_as_dict(selected_task), ensure_ascii=False, indent=2))
            return 0
        if args.task_command == "cancel":
            if not repo.request_cancel(selected_task.id):
                print(
                    f"ERROR: Task {selected_task.id} はキャンセルできない状態です",
                    file=sys.stderr,
                )
                return 1
            session.refresh(selected_task)
            if selected_task.status == TaskStatus.CANCELLED:
                sync_target_after_task(
                    session,
                    selected_task,
                    TaskStatus.CANCELLED,
                )
            print(f"Task {selected_task.id} のキャンセルを要求しました")
            return 0
        if args.task_command == "retry":
            if not repo.retry(selected_task.id):
                print(f"ERROR: Task {selected_task.id} は再試行できない状態です", file=sys.stderr)
                return 1
            print(f"Task {selected_task.id} をpendingへ戻しました")
            return 0
    finally:
        session.close()
    return 2


__all__ = ["configure_parser", "dispatch"]
