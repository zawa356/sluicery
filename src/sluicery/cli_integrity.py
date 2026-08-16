"""Artifact整合性チェックのCLI。"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from sqlalchemy.orm import Session

from sluicery.cli_crud import resolve_record
from sluicery.core.integrity import check_integrity
from sluicery.core.settings import OperationalSettings
from sluicery.db.models import Playlist, Storage
from sluicery.storage import create_storage_adapter

OpenSession = Callable[[], Session]


def configure_parser(sub: argparse._SubParsersAction) -> None:
    parser = sub.add_parser("integrity", help="Artifactの実体とDBパスを点検する")
    commands = parser.add_subparsers(dest="integrity_command", required=True)
    check = commands.add_parser("check", help="整合性を確認しDBだけをrelink更新する")
    check.add_argument("--storage", help="Storageの名前またはID")
    check.add_argument("--playlist", help="Playlistの名前またはID")


def dispatch(args: argparse.Namespace, *, open_session: OpenSession) -> int | None:
    if args.command != "integrity":
        return None
    session = open_session()
    try:
        storage = (
            resolve_record(session, Storage, args.storage, label="Storage")
            if args.storage
            else None
        )
        playlist = (
            resolve_record(session, Playlist, args.playlist, label="Playlist")
            if args.playlist
            else None
        )
        operational = OperationalSettings(session)

        def adapter_factory(row: Storage):
            return create_storage_adapter(row, operational)

        report = check_integrity(
            session,
            adapter_factory,
            storage_id=storage.id if storage is not None else None,
            playlist_id=playlist.id if playlist is not None else None,
            rescan_timeout_sec=operational.integrity_rescan_timeout_sec,
            max_candidates_per_source_id=(
                operational.integrity_max_candidates_per_source_id
            ),
        )
    except (LookupError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    finally:
        session.close()

    print(
        f"確認={report.checked} 存在={report.present} relink={report.relinked} "
        f"missing={report.missing} 復帰={report.restored} "
        f"再走査Storage={report.rescanned_storages}"
    )
    for issue in report.issues:
        line = (
            f"artifact={issue.artifact_id} target={issue.target_id} "
            f"storage={issue.storage_id} kind={issue.kind}"
        )
        if issue.candidates:
            line += f" candidates={','.join(issue.candidates)}"
        print(line)
    has_scan_error = any(
        issue.kind in {"storage_error", "scan_error", "scan_timeout"}
        for issue in report.issues
    )
    return 1 if has_scan_error else 0


__all__ = ["configure_parser", "dispatch"]
