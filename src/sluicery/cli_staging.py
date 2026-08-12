from __future__ import annotations

import argparse

from sluicery.core.staging import find_orphans


def configure_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("staging", help="Staging領域を点検する")
    commands = parser.add_subparsers(dest="staging_command", required=True)
    commands.add_parser("orphans", help="対応Taskがないファイルを一覧表示する（削除しない）")


def dispatch(args: argparse.Namespace, *, open_session, load_settings) -> int | None:
    if args.command != "staging":
        return None
    settings = load_settings()
    assert settings.STAGING_DIR is not None
    session = open_session()
    try:
        orphans = find_orphans(session, settings.STAGING_DIR)
    finally:
        session.close()
    for orphan in orphans:
        print(f"{orphan.relative_path}\t{orphan.size}")
    print(f"孤立ファイル: {len(orphans)}件（自動削除していません）")
    return 0


__all__ = ["configure_parser", "dispatch"]
