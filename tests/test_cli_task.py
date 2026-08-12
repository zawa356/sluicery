from __future__ import annotations

import json

from sluicery import cli
from sluicery.core import settings as core_settings
from sluicery.db.models import TaskStatus
from sluicery.db.repositories.task import TaskRepository


def test_task_enqueue_is_disabled_by_default(session_factory, monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "_open_session", lambda: session_factory())

    assert cli.main(["task", "enqueue", "noop"]) == 1
    assert "検証用Taskは無効" in capsys.readouterr().err


def test_task_cli_enqueue_list_show_cancel_retry(session_factory, monkeypatch, capsys) -> None:
    with session_factory() as session:
        core_settings.set_override(session, "worker.enable_test_tasks", True)
    monkeypatch.setattr(cli, "_open_session", lambda: session_factory())

    assert cli.main(
        [
            "task",
            "enqueue",
            "sleep",
            "--worker-class",
            "compute",
            "--priority",
            "7",
            "--payload",
            '{"sec": 30}',
        ]
    ) == 0
    task_id = int(capsys.readouterr().out.strip())

    assert cli.main(["task", "list", "--worker-class", "compute"]) == 0
    assert f"{task_id}\tsleep\tcompute\tqueued" in capsys.readouterr().out

    assert cli.main(["task", "show", str(task_id)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["priority"] == 7
    assert shown["payload"] == {"sec": 30}

    assert cli.main(["task", "cancel", str(task_id)]) == 0
    capsys.readouterr()
    with session_factory() as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None and task.status == TaskStatus.CANCELLED

    assert cli.main(["task", "retry", str(task_id)]) == 0
    capsys.readouterr()
    with session_factory() as session:
        task = TaskRepository(session).get(task_id)
        assert task is not None and task.status == TaskStatus.PENDING
