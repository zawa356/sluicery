"""sluicery のエントリポイント。compose の各サービスから呼び出される。"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from alembic.config import Config as AlembicConfig

    from sluicery.config import Settings

REPO_ROOT = Path(__file__).resolve().parents[2]


def _alembic_config() -> AlembicConfig:
    from alembic.config import Config as AlembicConfig

    return AlembicConfig(str(REPO_ROOT / "alembic.ini"))


def _current_and_head_revision(db_path: Path | None) -> tuple[str | None, str | None]:
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from sluicery.db.session import create_engine_for

    assert db_path is not None  # Settings のモデル検証で必ず補完される

    script = ScriptDirectory.from_config(_alembic_config())
    head = script.get_current_head()

    engine = create_engine_for(db_path)
    with engine.connect() as conn:
        context = MigrationContext.configure(conn)
        current = context.get_current_revision()
    engine.dispose()
    return current, head


def _ensure_migrations_for_web(settings: Settings) -> None:
    from alembic import command

    if settings.AUTO_MIGRATE:
        command.upgrade(_alembic_config(), "head")
        return

    # AUTO_MIGRATE=false: 適用はしない。未適用があれば警告を出して起動は継続する（§5.3）。
    current, head = _current_and_head_revision(settings.DB_PATH)
    if current != head:
        print(
            f"WARNING: DB が最新のマイグレーション（head={head}）に追従していません"
            f"（現在: {current}）。AUTO_MIGRATE=false のため自動適用しません。"
            "`sluicery db upgrade` を実行してください。",
            file=sys.stderr,
        )


def _wait_for_migrations_for_worker(settings: Settings, *, timeout_seconds: float = 60.0) -> None:
    """worker はマイグレーションを実行しない。head と一致するまで待機し、
    タイムアウトしたらエラー終了する（§5.3）。
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        current, head = _current_and_head_revision(settings.DB_PATH)
        if current == head:
            return
        if time.monotonic() >= deadline:
            print(
                f"ERROR: DB のマイグレーションが head と一致しません"
                f"（現在: {current}, head: {head}）。タイムアウトしました。",
                file=sys.stderr,
            )
            sys.exit(1)
        time.sleep(1)


def _open_session():
    from sluicery.config import load_settings
    from sluicery.db import crypto
    from sluicery.db.session import create_engine_for, create_session_factory

    settings = load_settings()
    crypto.set_encryption_key(settings.SECRET_KEY)
    engine = create_engine_for(settings.DB_PATH)
    return create_session_factory(engine)()


def _maybe_auto_install_ytdlp(settings: Settings) -> None:
    """`ytdlp.auto_install` が true なら、起動後に非同期で yt-dlp の導入を試みる。

    未実装処理を待つ worker と異なり、app は yt-dlp が無くても Web UI 自体は
    提供できるため、このスレッドはベストエフォートで実行し、失敗してもアプリ
    本体（uvicorn）には一切影響させない（degraded 起動。§2.5）。
    """
    from sluicery.core import settings as core_settings
    from sluicery.db.models import YtdlpReleaseSource
    from sluicery.db.session import create_engine_for, create_session_factory
    from sluicery.downloader.version import InstallStatus, get_status, install, ytdlp_root

    root = ytdlp_root(settings.DATA_DIR)
    assert settings.DB_PATH is not None  # Settings のモデル検証で必ず補完される
    engine = create_engine_for(settings.DB_PATH)
    try:
        session = create_session_factory(engine)()
        try:
            if not core_settings.OperationalSettings(session).ytdlp_auto_install:
                return
            if get_status(root).status == InstallStatus.READY:
                return
            print("[sluicery] yt-dlp を自動導入します...", flush=True)
            release = install(root, session, source=YtdlpReleaseSource.AUTO)
            print(f"[sluicery] yt-dlp {release.version} の自動導入が完了しました", flush=True)
        finally:
            session.close()
    except Exception as exc:  # noqa: BLE001 - ベストエフォートであり app は落とさない
        print(f"[sluicery] yt-dlp の自動導入に失敗しました: {exc}", file=sys.stderr, flush=True)
    finally:
        engine.dispose()


def _run_web() -> int:
    import threading

    import uvicorn

    from sluicery.config import load_settings
    from sluicery.web.app import create_app

    settings = load_settings()
    _ensure_migrations_for_web(settings)

    threading.Thread(
        target=_maybe_auto_install_ytdlp, args=(settings,), daemon=True, name="ytdlp-auto-install"
    ).start()

    port = int(os.environ.get("HTTP_PORT", str(settings.HTTP_PORT)))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info")
    return 0


def _wait_for_ytdlp_ready(root: Path, *, poll_interval_sec: float = 10.0) -> None:
    """`current` が有効（`ready`）になるまで待機する。

    ログは初回と状態変化時のみ出力し、毎回は出力しない（§2.5）。導入・切替・
    削除は `app` サービスのみが行うため、ここでは読み取り専用でポーリングする
    （§2.4）。
    """
    from sluicery.downloader.version import InstallStatus, get_status

    last_status: InstallStatus | None = None
    while True:
        result = get_status(root)
        if result.status != last_status:
            if result.status == InstallStatus.READY:
                msg = f"[sluicery] yt-dlp 準備完了（バージョン: {result.current_version}）"
            else:
                msg = f"[sluicery] yt-dlp 待機中（状態: {result.status.value}）"
            print(msg, flush=True)
            last_status = result.status
        if result.status == InstallStatus.READY:
            return
        time.sleep(poll_interval_sec)


def _run_worker(worker_class: str) -> int:
    from sluicery.config import load_settings
    from sluicery.downloader.version import ytdlp_root

    settings = load_settings()
    _wait_for_migrations_for_worker(settings)
    _wait_for_ytdlp_ready(ytdlp_root(settings.DATA_DIR))

    # Task キュー・ワーカーの実装は実装順序 #6 で追加する。未実装の処理を持つ
    # worker はここで終了せず待機ループへ入る。終了して restart: unless-stopped
    # に任せると、Docker の restart backoff が効き始め、Phase 6 で実際の異常が
    # 起きたときに区別できなくなる（docs/phase3_指示書.md §0.3）。
    print(
        f"[sluicery] worker-{worker_class}: 実装準備中（実装順序 #6 で追加）。以降は待機します",
        flush=True,
    )
    while True:
        time.sleep(3600)


def _cmd_config_check() -> int:
    from sluicery.config import check_config

    all_ok = True
    for result in check_config():
        status = "OK" if result.ok else "NG"
        line = f"[{status}] {result.name} = {result.display_value}"
        if result.message:
            line += f"  -- {result.message}"
        print(line)
        all_ok = all_ok and result.ok
    return 0 if all_ok else 1


def _cmd_db_upgrade() -> int:
    from alembic import command

    command.upgrade(_alembic_config(), "head")
    return 0


def _cmd_db_current() -> int:
    from sluicery.config import load_settings

    settings = load_settings()
    current, head = _current_and_head_revision(settings.DB_PATH)
    print(f"current: {current}")
    print(f"head:    {head}")
    print("状態: 最新です" if current == head else "状態: 未適用のマイグレーションがあります")
    return 0 if current == head else 1


def _cmd_db_revision(message: str) -> int:
    from alembic import command

    command.revision(_alembic_config(), message=message, autogenerate=True)
    return 0


def _cmd_settings_list() -> int:
    from sluicery.core import settings as core_settings

    session = _open_session()
    try:
        for entry in core_settings.list_all(session):
            marker = "*" if entry.is_override else " "
            print(f"{marker} {entry.key} = {entry.value}")
    finally:
        session.close()
    return 0


def _cmd_settings_get(key: str) -> int:
    from sluicery.core import settings as core_settings

    session = _open_session()
    try:
        print(core_settings.get(session, key))
        return 0
    except core_settings.UnknownSettingKeyError:
        print(f"ERROR: 未知の設定キーです: {key}", file=sys.stderr)
        return 1
    finally:
        session.close()


def _cmd_settings_set(key: str, value: str) -> int:
    from sluicery.core import settings as core_settings

    session = _open_session()
    try:
        core_settings.set_override(session, key, value)
        print(f"{key} = {core_settings.get(session, key)}")
        return 0
    except core_settings.UnknownSettingKeyError:
        print(f"ERROR: 未知の設定キーです: {key}", file=sys.stderr)
        return 1
    except (TypeError, ValueError) as exc:
        print(f"ERROR: 値が不正です: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()


def _cmd_settings_unset(key: str) -> int:
    from sluicery.core import settings as core_settings

    session = _open_session()
    try:
        core_settings.unset_override(session, key)
        print(f"{key} = {core_settings.get(session, key)}（既定値）")
        return 0
    except core_settings.UnknownSettingKeyError:
        print(f"ERROR: 未知の設定キーです: {key}", file=sys.stderr)
        return 1
    finally:
        session.close()


def _ytdlp_root_from_settings():
    from sluicery.config import load_settings
    from sluicery.downloader.version import ytdlp_root

    settings = load_settings()
    return settings, ytdlp_root(settings.DATA_DIR)


def _cmd_ytdlp_status() -> int:
    from sluicery.downloader.version import get_status

    _settings, root = _ytdlp_root_from_settings()
    result = get_status(root)
    print(f"status: {result.status.value}")
    print(f"current_version: {result.current_version or '(なし)'}")
    if result.version_output:
        print(f"version_output: {result.version_output}")
    return 0


def _cmd_ytdlp_list() -> int:
    from sluicery.downloader.version import list_versions, read_current_version

    _settings, root = _ytdlp_root_from_settings()
    current = read_current_version(root)
    session = _open_session()
    try:
        releases = list_versions(session)
        if not releases:
            print("(導入済みのバージョンはありません)")
            return 0
        for release in releases:
            marker = "*" if release.version == current else " "
            print(
                f"{marker} {release.version}  status={release.status.value}"
                f"  installed_at={release.installed_at.isoformat()}"
            )
        return 0
    finally:
        session.close()


def _cmd_ytdlp_install(version: str | None, force: bool) -> int:
    from sluicery.db.models import YtdlpReleaseSource
    from sluicery.downloader.version import YtdlpInstallError, install

    _settings, root = _ytdlp_root_from_settings()
    session = _open_session()
    try:
        try:
            release = install(
                root, session, version=version, source=YtdlpReleaseSource.MANUAL, force=force
            )
        except YtdlpInstallError as exc:
            print(f"ERROR: yt-dlp の導入に失敗しました: {exc}", file=sys.stderr)
            return 1
        print(f"導入しました: {release.version}（status={release.status.value}）")
        return 0
    finally:
        session.close()


def _cmd_ytdlp_use(version: str) -> int:
    from sluicery.downloader.version import UnknownVersionError, use

    _settings, root = _ytdlp_root_from_settings()
    session = _open_session()
    try:
        try:
            release = use(root, session, version)
        except UnknownVersionError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"切り替えました: {release.version}")
        return 0
    finally:
        session.close()


def _cmd_ytdlp_remove(version: str) -> int:
    from sluicery.downloader.version import CurrentVersionRemovalError, UnknownVersionError, remove

    _settings, root = _ytdlp_root_from_settings()
    session = _open_session()
    try:
        try:
            remove(root, session, version)
        except (CurrentVersionRemovalError, UnknownVersionError) as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 1
        print(f"削除しました: {version}")
        return 0
    finally:
        session.close()


def _ytdlp_timeout_and_bin():
    """`ytdlp exec` / `probe` / `fetch` で共通の準備（導入確認・タイムアウト設定の取得）。"""
    from sluicery.core import settings as core_settings
    from sluicery.downloader.version import InstallStatus, current_ytdlp_bin, get_status
    from sluicery.downloader.ytdlp import TimeoutPolicy

    settings, root = _ytdlp_root_from_settings()
    status = get_status(root)
    if status.status != InstallStatus.READY:
        print(f"ERROR: yt-dlp が利用できません（状態: {status.status.value}）", file=sys.stderr)
        return None

    session = _open_session()
    try:
        ops = core_settings.OperationalSettings(session)
        timeout = TimeoutPolicy(
            idle_sec=ops.ytdlp_idle_timeout_sec,
            absolute_sec=ops.ytdlp_absolute_timeout_sec,
            term_grace_sec=ops.ytdlp_term_grace_sec,
        )
        stderr_tail_kb = ops.ytdlp_stderr_tail_kb
    finally:
        session.close()

    return settings, current_ytdlp_bin(root), timeout, stderr_tail_kb


def _cmd_ytdlp_exec(args: list[str]) -> int:
    from sluicery.downloader.ytdlp import YtdlpRunner, mask_command_line

    prepared = _ytdlp_timeout_and_bin()
    if prepared is None:
        return 1
    settings, bin_path, timeout, stderr_tail_kb = prepared

    print(f"$ yt-dlp {' '.join(mask_command_line(args))}")
    runner = YtdlpRunner(
        bin_path, stderr_tail_kb=stderr_tail_kb, log_dir=settings.DATA_DIR / "logs"
    )
    result = runner.run(args, timeout=timeout)

    for line in result.stdout_lines:
        print(line)
    if result.stderr_tail:
        print(result.stderr_tail, file=sys.stderr)
    print(
        f"returncode={result.returncode} classification={result.classification.value}"
        f" terminated_by={result.terminated_by}"
    )
    return result.returncode


def _cmd_ytdlp_probe(url: str) -> int:
    from sluicery.downloader.protocol import PRINT_PREFIX
    from sluicery.downloader.ytdlp import YtdlpRunner

    prepared = _ytdlp_timeout_and_bin()
    if prepared is None:
        return 1
    settings, bin_path, timeout, stderr_tail_kb = prepared

    args = [url, "--simulate", "--print", f"{PRINT_PREFIX}%()j"]
    runner = YtdlpRunner(
        bin_path, stderr_tail_kb=stderr_tail_kb, log_dir=settings.DATA_DIR / "logs"
    )
    result = runner.run(args, timeout=timeout)

    if result.returncode != 0 or not result.stdout_lines:
        print(
            f"ERROR: 取得に失敗しました（classification={result.classification.value}）",
            file=sys.stderr,
        )
        if result.stderr_tail:
            print(result.stderr_tail, file=sys.stderr)
        return 1

    import json as _json

    for line in result.stdout_lines:
        try:
            info = _json.loads(line)
        except ValueError:
            print(line)
            continue
        print(f"id: {info.get('id')}")
        print(f"title: {info.get('title')}")
        print(f"uploader: {info.get('uploader')}")
        print(f"duration: {info.get('duration')}")
        formats = info.get("formats") or []
        print(f"formats: {len(formats)}件")
        for fmt in formats:
            print(
                f"  - {fmt.get('format_id')}: {fmt.get('ext')} "
                f"{fmt.get('resolution') or fmt.get('vcodec')} "
                f"{fmt.get('filesize') or fmt.get('filesize_approx') or ''}"
            )
    return 0


def _cmd_ytdlp_fetch(url: str, dest: str | None) -> int:
    from sluicery.downloader.errors import Classification
    from sluicery.downloader.progress import ProgressEvent
    from sluicery.downloader.protocol import PRINT_PREFIX, PROGRESS_PREFIX
    from sluicery.downloader.ytdlp import YtdlpRunner

    prepared = _ytdlp_timeout_and_bin()
    if prepared is None:
        return 1
    settings, bin_path, timeout, stderr_tail_kb = prepared

    dest_dir = Path(dest) if dest else settings.STAGING_DIR
    assert dest_dir is not None
    dest_dir.mkdir(parents=True, exist_ok=True)

    args = [
        url,
        "--newline",
        "--paths",
        str(dest_dir),
        "-o",
        "%(id)s.%(ext)s",
        "--progress-template",
        f"download:{PROGRESS_PREFIX}%(progress)j",
        "--print",
        f"after_move:{PRINT_PREFIX}%(filepath)s",
    ]

    def on_progress(event: ProgressEvent) -> None:
        if event.total_bytes and event.downloaded_bytes is not None:
            pct = f"{event.downloaded_bytes / event.total_bytes * 100:5.1f}%"
        else:
            pct = "  ?  "
        print(
            f"\r[{pct}] {event.status} {event.downloaded_bytes or 0}/{event.total_bytes or '?'}",
            end="",
            flush=True,
        )

    runner = YtdlpRunner(
        bin_path, stderr_tail_kb=stderr_tail_kb, log_dir=settings.DATA_DIR / "logs"
    )
    result = runner.run(args, timeout=timeout, on_progress=on_progress)
    print()

    for line in result.stdout_lines:
        print(f"保存先: {line}")
    print(
        f"returncode={result.returncode} classification={result.classification.value}"
        f" terminated_by={result.terminated_by}"
    )
    if result.classification != Classification.OK:
        if result.stderr_tail:
            print(result.stderr_tail, file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sluicery")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("web", help="Web UI + REST API + スケジューラを起動する")

    worker_parser = sub.add_parser("worker", help="Task ワーカーを起動する")
    worker_parser.add_argument(
        "--class", dest="worker_class", choices=["network", "compute"], required=True
    )

    config_parser = sub.add_parser("config", help="設定を検証する")
    config_sub = config_parser.add_subparsers(dest="config_command", required=True)
    config_sub.add_parser("check", help=".env の全項目を検証して一覧表示する")

    db_parser = sub.add_parser("db", help="DB マイグレーションを操作する")
    db_sub = db_parser.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("upgrade", help="alembic upgrade head を実行する")
    db_sub.add_parser("current", help="現在のリビジョンと head の一致を表示する")
    db_revision_parser = db_sub.add_parser("revision", help="autogenerate でリビジョンを生成する")
    db_revision_parser.add_argument("-m", "--message", required=True)

    settings_parser = sub.add_parser("settings", help="運用パラメータを操作する")
    settings_sub = settings_parser.add_subparsers(dest="settings_command", required=True)
    settings_sub.add_parser("list", help="全運用パラメータを一覧表示する")
    settings_get_parser = settings_sub.add_parser("get", help="1件取得する")
    settings_get_parser.add_argument("key")
    settings_set_parser = settings_sub.add_parser("set", help="上書き値を保存する")
    settings_set_parser.add_argument("key")
    settings_set_parser.add_argument("value")
    settings_unset_parser = settings_sub.add_parser("unset", help="上書きを削除し既定値に戻す")
    settings_unset_parser.add_argument("key")

    ytdlp_parser = sub.add_parser("ytdlp", help="yt-dlp の venv を管理する")
    ytdlp_sub = ytdlp_parser.add_subparsers(dest="ytdlp_command", required=True)
    ytdlp_sub.add_parser("status", help="導入状態（ready/not_installed/broken）を表示する")
    ytdlp_sub.add_parser("list", help="導入済みバージョン一覧を表示する")
    ytdlp_install_parser = ytdlp_sub.add_parser(
        "install", help="導入する（未指定なら最新。既に導入済みなら何もしない）"
    )
    ytdlp_install_parser.add_argument("--version", dest="version", default=None)
    ytdlp_install_parser.add_argument("--force", action="store_true")
    ytdlp_use_parser = ytdlp_sub.add_parser("use", help="導入済みバージョンへ切り替える")
    ytdlp_use_parser.add_argument("version")
    ytdlp_remove_parser = ytdlp_sub.add_parser("remove", help="導入済みバージョンを削除する")
    ytdlp_remove_parser.add_argument("version")
    ytdlp_exec_parser = ytdlp_sub.add_parser(
        "exec", help="予約引数を注入せず生実行する（デバッグ用）"
    )
    ytdlp_exec_parser.add_argument("ytdlp_args", nargs=argparse.REMAINDER)
    ytdlp_probe_parser = ytdlp_sub.add_parser(
        "probe", help="個別試験用。メタデータとフォーマット一覧を表示する"
    )
    ytdlp_probe_parser.add_argument("url")
    ytdlp_fetch_parser = ytdlp_sub.add_parser(
        "fetch", help="個別試験用。Staging へ実際にダウンロードする"
    )
    ytdlp_fetch_parser.add_argument("url")
    ytdlp_fetch_parser.add_argument("--dest", default=None)

    args = parser.parse_args(argv)

    if args.command == "web":
        return _run_web()
    if args.command == "worker":
        return _run_worker(args.worker_class)
    if args.command == "config" and args.config_command == "check":
        return _cmd_config_check()
    if args.command == "db":
        if args.db_command == "upgrade":
            return _cmd_db_upgrade()
        if args.db_command == "current":
            return _cmd_db_current()
        if args.db_command == "revision":
            return _cmd_db_revision(args.message)
    if args.command == "settings":
        if args.settings_command == "list":
            return _cmd_settings_list()
        if args.settings_command == "get":
            return _cmd_settings_get(args.key)
        if args.settings_command == "set":
            return _cmd_settings_set(args.key, args.value)
        if args.settings_command == "unset":
            return _cmd_settings_unset(args.key)
    if args.command == "ytdlp":
        if args.ytdlp_command == "status":
            return _cmd_ytdlp_status()
        if args.ytdlp_command == "list":
            return _cmd_ytdlp_list()
        if args.ytdlp_command == "install":
            return _cmd_ytdlp_install(args.version, args.force)
        if args.ytdlp_command == "use":
            return _cmd_ytdlp_use(args.version)
        if args.ytdlp_command == "remove":
            return _cmd_ytdlp_remove(args.version)
        if args.ytdlp_command == "exec":
            passthrough = args.ytdlp_args
            if passthrough and passthrough[0] == "--":
                passthrough = passthrough[1:]
            return _cmd_ytdlp_exec(passthrough)
        if args.ytdlp_command == "probe":
            return _cmd_ytdlp_probe(args.url)
        if args.ytdlp_command == "fetch":
            return _cmd_ytdlp_fetch(args.url, args.dest)

    parser.error(f"未知のコマンド: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
