"""discover / download 二相同期のCLI。"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable

from sqlalchemy.orm import Session

from sluicery.core.sync import enqueue_discover_run, execute_download_run
from sluicery.db.models import Playlist, Run, RunStatus, Task, TaskStatus
from sluicery.db.repositories.playlist import PlaylistRepository
from sluicery.db.repositories.run import RunRepository
from sluicery.db.repositories.task import TaskRepository

OpenSession = Callable[[], Session]
_DISCOVER_TERMINAL = {
    TaskStatus.SUCCEEDED,
    TaskStatus.FAILED,
    TaskStatus.UNAVAILABLE,
    TaskStatus.CANCELLED,
}
_STAT_KEYS = (
    "new_items",
    "delisted_items",
    "targets_queued",
    "targets_remaining",
    "downloaded",
    "failed",
    "blocked",
    "empty_result",
)


def configure_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("sync", help="Playlistをdiscover / download同期する")
    commands = parser.add_subparsers(dest="sync_command", required=True)
    for name, help_text in (
        ("discover", "一覧を取得してItem差分を反映する"),
        ("download", "取得対象の5段チェーンを投入する"),
        ("run", "discover成功後にdownloadを実行する"),
    ):
        command = commands.add_parser(name, help=help_text)
        selector = command.add_mutually_exclusive_group(required=True)
        selector.add_argument("--playlist", help="Playlistの名前またはID")
        selector.add_argument("--all", action="store_true", help="全ての有効なPlaylist")
        if name in {"discover", "run"}:
            command.add_argument(
                "--dry-run",
                action="store_true",
                help="discover差分だけを表示し、Item / Targetを変更しない",
            )


def dispatch(
    args: argparse.Namespace,
    *,
    open_session: OpenSession,
    poll_interval_sec: float = 0.5,
) -> int | None:
    if args.command != "sync":
        return None
    try:
        playlists = _resolve_playlists(open_session, args.playlist, args.all)
    except (LookupError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    if not playlists:
        print("対象となる有効なPlaylistはありません")
        return 0

    failed = False
    download_playlists: list[tuple[int, str]] = []
    try:
        for playlist_id, playlist_name in playlists:
            print(f"Playlist {playlist_id} ({playlist_name})")
            if args.sync_command in {"discover", "run"}:
                discover = _execute_discover(
                    open_session,
                    playlist_id,
                    dry_run=args.dry_run,
                    poll_interval_sec=poll_interval_sec,
                )
                _print_run(discover)
                if discover.status != RunStatus.SUCCEEDED:
                    failed = True
                    continue
                stats = discover.stats_json or {}
                if stats.get("empty_result"):
                    print("WARNING: discover結果が空のため、Item差分とdownloadをスキップしました")
                    continue
                if args.dry_run or args.sync_command == "discover":
                    continue
                # --all で先行Playlistのdownload Taskが後続discoverをFIFO待ちに
                # しないよう、runは全discover完了後にdownloadフェーズへ進む。
                download_playlists.append((playlist_id, playlist_name))
                continue
            if args.sync_command == "download":
                with open_session() as session:
                    download = execute_download_run(session, playlist_id)
                _print_run(download)
                failed = failed or download.status != RunStatus.SUCCEEDED
        for playlist_id, playlist_name in download_playlists:
            print(f"Playlist {playlist_id} ({playlist_name}) download")
            with open_session() as session:
                download = execute_download_run(session, playlist_id)
            _print_run(download)
            failed = failed or download.status != RunStatus.SUCCEEDED
    except KeyboardInterrupt:
        print("\n中断しました。実行中Taskへキャンセルを要求します", file=sys.stderr)
        return 130
    except (LookupError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 1 if failed else 0


def _resolve_playlists(
    open_session: OpenSession, identifier: str | None, all_playlists: bool
) -> list[tuple[int, str]]:
    with open_session() as session:
        repo = PlaylistRepository(session)
        rows: list[Playlist]
        if all_playlists:
            rows = repo.list_runnable()
        elif identifier is not None:
            rows = [repo.resolve_runnable(identifier)]
        else:
            raise ValueError("--playlistまたは--allを指定してください")
        return [(playlist.id, playlist.name) for playlist in rows]


def _execute_discover(
    open_session: OpenSession,
    playlist_id: int,
    *,
    dry_run: bool,
    poll_interval_sec: float,
) -> Run:
    with open_session() as session:
        run, task = enqueue_discover_run(session, playlist_id, dry_run=dry_run)
        run_id = run.id
        task_id = task.id
    try:
        task = _wait_for_task(open_session, task_id, poll_interval_sec=poll_interval_sec)
    except KeyboardInterrupt:
        with open_session() as session:
            TaskRepository(session).request_cancel(task_id)
            RunRepository(session).finish(run_id, RunStatus.CANCELLED, {})
        raise
    with open_session() as session:
        loaded_run = session.get(Run, run_id)
        if loaded_run is None:
            raise LookupError(f"Run {run_id} が見つかりません")
        if task.status != TaskStatus.SUCCEEDED and loaded_run.status == RunStatus.RUNNING:
            status = (
                RunStatus.CANCELLED if task.status == TaskStatus.CANCELLED else RunStatus.FAILED
            )
            RunRepository(session).finish(loaded_run.id, status, loaded_run.stats_json or {})
            session.refresh(loaded_run)
        session.expunge(loaded_run)
        return loaded_run


def _wait_for_task(
    open_session: OpenSession,
    task_id: int,
    *,
    poll_interval_sec: float,
) -> Task:
    while True:
        with open_session() as session:
            task = session.get(Task, task_id)
            if task is None:
                raise LookupError(f"Task {task_id} が見つかりません")
            if task.status in _DISCOVER_TERMINAL:
                session.expunge(task)
                return task
        time.sleep(poll_interval_sec)


def _print_run(run: Run) -> None:
    print(f"Run {run.id}: kind={run.kind} status={run.status.value}")
    stats = run.stats_json or {}
    for key in _STAT_KEYS:
        print(f"  {key}: {stats.get(key, 0 if key != 'empty_result' else False)}")


__all__ = ["configure_parser", "dispatch"]
