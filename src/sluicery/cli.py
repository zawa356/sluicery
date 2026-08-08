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


def _run_web() -> int:
    import uvicorn

    from sluicery.config import load_settings
    from sluicery.web.app import create_app

    settings = load_settings()
    _ensure_migrations_for_web(settings)

    port = int(os.environ.get("HTTP_PORT", str(settings.HTTP_PORT)))
    uvicorn.run(create_app(), host="0.0.0.0", port=port, log_level="info")
    return 0


def _run_worker(worker_class: str) -> int:
    from sluicery.config import load_settings

    settings = load_settings()
    _wait_for_migrations_for_worker(settings)

    # Task キュー・ワーカーの実装は実装順序 #6 で追加する。
    print(f"[sluicery] worker-{worker_class}: 実装準備中（実装順序 #6 で追加）", flush=True)
    return 0


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

    parser.error(f"未知のコマンド: {args.command}")
    return 2


if __name__ == "__main__":
    sys.exit(main())
