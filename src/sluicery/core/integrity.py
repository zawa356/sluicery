"""Storageを変更しない整合性チェックとrelink。"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.orm import Session

from sluicery.db.models import (
    Artifact,
    Item,
    MissingPolicy,
    Playlist,
    Storage,
    Target,
    TargetStatus,
)
from sluicery.storage.base import (
    RemoteFile,
    StorageAdapter,
    StorageOperationError,
    validate_relative_path,
)

AdapterFactory = Callable[[Storage], StorageAdapter]


@dataclass(frozen=True)
class IntegrityIssue:
    artifact_id: int
    target_id: int
    storage_id: int
    kind: str
    candidates: tuple[str, ...] = ()


@dataclass
class IntegrityReport:
    checked: int = 0
    present: int = 0
    relinked: int = 0
    missing: int = 0
    restored: int = 0
    rescanned_storages: int = 0
    issues: list[IntegrityIssue] = field(default_factory=list)


@dataclass(frozen=True)
class _ArtifactSnapshot:
    artifact_id: int
    target_id: int
    storage_id: int
    relative_path: str
    source_id: str
    artifact_updated_at: datetime
    target_status: TargetStatus
    target_updated_at: datetime


@dataclass(frozen=True)
class _Decision:
    kind: str
    relative_path: str | None = None
    issue_kind: str | None = None
    candidates: tuple[str, ...] = ()


def _matches_source_id(relative_path: str, source_id: str) -> bool:
    """拡張子直前の末尾IDだけを照合し、部分一致を避ける。"""
    name = PurePosixPath(relative_path).name
    return bool(re.search(rf"\[{re.escape(source_id)}\](?:\.[^./]+)+$", name))


def _list_with_timeout(
    adapter: StorageAdapter, timeout_sec: int
) -> tuple[list[RemoteFile] | None, str | None]:
    """Adapterの期限付き走査を使い、失敗をmissingと分離する。"""
    try:
        return list(adapter.list_recursive("", timeout_sec=timeout_sec)), None
    except StorageOperationError as exc:
        return None, "scan_timeout" if exc.reason_code == "timeout" else "scan_error"
    except Exception:  # noqa: BLE001 - Storageエラーをmissingへ誤変換しない境界
        return None, "scan_error"


def check_integrity(
    session: Session,
    adapter_factory: AdapterFactory,
    *,
    storage_id: int | None = None,
    playlist_id: int | None = None,
    rescan_timeout_sec: int = 600,
    max_candidates_per_source_id: int = 5,
    now: datetime | None = None,
) -> IntegrityReport:
    """Artifactを確認し、DBだけをrelink/missing更新する。

    Storageへの書込み・移動・削除APIは呼ばない。存在確認または走査に失敗した
    Storageでは、未確認Artifactをmissingへ変更しない。
    """
    if rescan_timeout_sec <= 0:
        raise ValueError("rescan_timeout_secは1以上にしてください")
    if max_candidates_per_source_id <= 0:
        raise ValueError("max_candidates_per_source_idは1以上にしてください")
    checked_at = now or datetime.now(UTC)
    stmt = (
        select(Artifact, Target, Item, Playlist, Storage)
        .join(Target, Target.id == Artifact.target_id)
        .join(Item, Item.id == Target.item_id)
        .join(Playlist, Playlist.id == Item.playlist_id)
        .join(Storage, Storage.id == Artifact.storage_id)
        .order_by(Storage.id, Artifact.id)
    )
    if storage_id is not None:
        stmt = stmt.where(Storage.id == storage_id)
    if playlist_id is not None:
        stmt = stmt.where(Playlist.id == playlist_id)
    grouped: dict[int, list[_ArtifactSnapshot]] = defaultdict(list)
    storages: dict[int, Storage] = {}
    for artifact, target, item, _playlist, storage in session.execute(stmt).tuples():
        grouped[storage.id].append(
            _ArtifactSnapshot(
                artifact.id,
                target.id,
                storage.id,
                artifact.relative_path,
                item.source_id,
                artifact.updated_at,
                target.status,
                target.updated_at,
            )
        )
        storages[storage.id] = storage

    report = IntegrityReport()
    tracked_by_storage: dict[int, set[str]] = defaultdict(set)
    if grouped:
        tracked_stmt = select(Artifact.storage_id, Artifact.relative_path).where(
            Artifact.storage_id.in_(grouped)
        )
        for tracked_storage_id, relative_path in session.execute(tracked_stmt).tuples():
            tracked_by_storage[tracked_storage_id].add(relative_path)

    adapters: dict[int, StorageAdapter] = {}
    for current_storage_id, rows in grouped.items():
        try:
            adapters[current_storage_id] = adapter_factory(storages[current_storage_id])
        except Exception:  # noqa: BLE001 - Adapter構築失敗もmissingへ誤変換しない
            for row in rows:
                report.issues.append(
                    IntegrityIssue(
                        row.artifact_id,
                        row.target_id,
                        current_storage_id,
                        "storage_error",
                    )
                )
    # Storage I/Oの前にread transactionを閉じる。結果適用時は現在値を再確認する。
    session.commit()

    snapshots = {
        row.artifact_id: row for rows in grouped.values() for row in rows
    }
    decisions: dict[int, _Decision] = {}
    for current_storage_id, rows in grouped.items():
        adapter = adapters.get(current_storage_id)
        if adapter is None:
            continue
        absent: list[_ArtifactSnapshot] = []
        present: list[_ArtifactSnapshot] = []
        storage_error = False
        for row in rows:
            report.checked += 1
            try:
                exists = adapter.exists(row.relative_path)
            except Exception:  # noqa: BLE001 - 到達不能をmissingと判定しない
                storage_error = True
                break
            (present if exists else absent).append(row)
        if storage_error:
            for row in rows:
                report.issues.append(
                    IntegrityIssue(
                        row.artifact_id,
                        row.target_id,
                        current_storage_id,
                        "storage_error",
                    )
                )
            continue

        scanned_files: list[RemoteFile] | None = None
        scan_error: str | None = None
        if absent:
            report.rescanned_storages += 1
            scanned_files, scan_error = _list_with_timeout(adapter, rescan_timeout_sec)
        if scan_error is not None:
            # 一部Artifactが存在しても、同一Storageに未確認が残る間は
            # missing解除やTarget復帰を行わない。
            for row in absent:
                report.issues.append(
                    IntegrityIssue(
                        row.artifact_id,
                        row.target_id,
                        current_storage_id,
                        scan_error,
                    )
                )
        elif scanned_files is not None:
            tracked_paths = tracked_by_storage[current_storage_id]
            untracked = [
                entry for entry in scanned_files if entry.relative_path not in tracked_paths
            ]
            candidates_by_artifact = {
                row.artifact_id: [
                    entry.relative_path
                    for entry in untracked
                    if _matches_source_id(entry.relative_path, row.source_id)
                ]
                for row in absent
            }
            claimants: dict[str, set[int]] = defaultdict(set)
            for artifact_id, candidates in candidates_by_artifact.items():
                for candidate in candidates:
                    claimants[candidate].add(artifact_id)
            for row in absent:
                candidates = candidates_by_artifact[row.artifact_id]
                if len(candidates) == 1 and len(claimants[candidates[0]]) == 1:
                    decisions[row.artifact_id] = _Decision("relink", candidates[0])
                else:
                    if len(candidates) == 1:
                        kind = "shared_candidate"
                    else:
                        kind = "multiple_candidates" if candidates else "missing"
                    decisions[row.artifact_id] = _Decision(
                        "missing",
                        issue_kind=kind,
                        candidates=tuple(candidates[:max_candidates_per_source_id]),
                    )
            for row in present:
                decisions[row.artifact_id] = _Decision("present")
        else:
            for row in present:
                decisions[row.artifact_id] = _Decision("present")

    revalidated_decisions: dict[int, _Decision] = {}
    for artifact_id, decision in decisions.items():
        snapshot = snapshots[artifact_id]
        adapter = adapters[snapshot.storage_id]
        try:
            original_exists = adapter.exists(snapshot.relative_path)
            if original_exists:
                revalidated_decisions[artifact_id] = _Decision("present")
            elif decision.kind == "missing":
                revalidated_decisions[artifact_id] = decision
            elif decision.kind == "relink":
                assert decision.relative_path is not None
                if adapter.exists(decision.relative_path):
                    revalidated_decisions[artifact_id] = decision
                else:
                    report.issues.append(
                        IntegrityIssue(
                            artifact_id,
                            snapshot.target_id,
                            snapshot.storage_id,
                            "storage_changed",
                        )
                    )
            else:
                report.issues.append(
                    IntegrityIssue(
                        artifact_id,
                        snapshot.target_id,
                        snapshot.storage_id,
                        "storage_changed",
                    )
                )
        except Exception:  # noqa: BLE001 - 再確認失敗時もDBに古い判定を適用しない
            report.issues.append(
                IntegrityIssue(
                    artifact_id,
                    snapshot.target_id,
                    snapshot.storage_id,
                    "storage_error",
                )
            )

    session.expire_all()
    affected_target_ids: set[int] = set()
    for artifact_id, decision in revalidated_decisions.items():
        snapshot = snapshots[artifact_id]
        loaded_artifact = session.get(Artifact, artifact_id)
        loaded_target = session.get(Target, snapshot.target_id)
        if (
            loaded_artifact is None
            or loaded_target is None
            or loaded_artifact.relative_path != snapshot.relative_path
            or loaded_artifact.updated_at != snapshot.artifact_updated_at
            or loaded_target.status != snapshot.target_status
            or loaded_target.updated_at != snapshot.target_updated_at
        ):
            continue
        if decision.kind == "relink":
            assert decision.relative_path is not None
            already_tracked = session.scalar(
                select(Artifact.id).where(
                    Artifact.storage_id == snapshot.storage_id,
                    Artifact.relative_path == decision.relative_path,
                    Artifact.id != artifact_id,
                )
            )
            if already_tracked is not None:
                continue
            loaded_artifact.relative_path = decision.relative_path
            loaded_artifact.absolute_path_cache = None
            loaded_artifact.missing_since = None
            report.relinked += 1
        elif decision.kind == "missing":
            loaded_artifact.missing_since = loaded_artifact.missing_since or checked_at
            report.missing += 1
            assert decision.issue_kind is not None
            report.issues.append(
                IntegrityIssue(
                    artifact_id,
                    snapshot.target_id,
                    snapshot.storage_id,
                    decision.issue_kind,
                    decision.candidates,
                )
            )
        else:
            loaded_artifact.missing_since = None
            report.present += 1
        affected_target_ids.add(snapshot.target_id)

    session.flush()
    for target_id in affected_target_ids:
        loaded_target = session.get(Target, target_id)
        assert loaded_target is not None
        artifacts = list(
            session.scalars(select(Artifact).where(Artifact.target_id == target_id))
        )
        has_missing = any(artifact.missing_since is not None for artifact in artifacts)
        if has_missing and loaded_target.status == TargetStatus.DOWNLOADED:
            loaded_playlist = session.scalar(
                select(Playlist)
                .join(Item, Item.playlist_id == Playlist.id)
                .where(Item.id == loaded_target.item_id)
            )
            assert loaded_playlist is not None
            if loaded_playlist.missing_policy == MissingPolicy.REDOWNLOAD:
                loaded_target.status = TargetStatus.PENDING
            elif loaded_playlist.missing_policy == MissingPolicy.IGNORE:
                loaded_target.status = TargetStatus.IGNORED
            else:
                loaded_target.status = TargetStatus.MISSING
        elif not has_missing and loaded_target.status == TargetStatus.MISSING:
            loaded_target.status = TargetStatus.DOWNLOADED
            report.restored += 1
    session.commit()
    return report


def list_orphan_files(
    session: Session,
    storage: Storage,
    adapter: StorageAdapter,
    *,
    timeout_sec: int = 600,
) -> tuple[list[RemoteFile], str | None]:
    """Storage内のArtifact未追跡ファイルを一覧する。ファイル操作は行わない。"""
    tracked = set(
        session.scalars(
            select(Artifact.relative_path).where(Artifact.storage_id == storage.id)
        )
    )
    session.commit()
    files, error = _list_with_timeout(adapter, timeout_sec)
    if files is None:
        return [], error
    return [entry for entry in files if entry.relative_path not in tracked], None


def set_missing_action(session: Session, target_id: int, action: MissingPolicy) -> TargetStatus:
    """missing実体に対する明示操作。ファイルには触れない。"""
    target = session.get(Target, target_id)
    if target is None:
        raise LookupError(f"Target {target_id} が見つかりません")
    has_missing = session.scalar(
        select(Artifact.id).where(
            Artifact.target_id == target_id,
            Artifact.missing_since.is_not(None),
        )
    )
    if has_missing is None:
        raise ValueError("実体不在のArtifactがありません")
    destination = {
        MissingPolicy.LEAVE: TargetStatus.MISSING,
        MissingPolicy.REDOWNLOAD: TargetStatus.PENDING,
        MissingPolicy.IGNORE: TargetStatus.IGNORED,
    }[action]
    target.status = destination
    target.last_error = None
    target.blocked_reason = None
    session.commit()
    return destination


def manual_link(
    session: Session,
    artifact_id: int,
    candidate_path: str,
    adapter: StorageAdapter,
    *,
    now: datetime | None = None,
) -> None:
    """孤立ファイルへDBパスだけを付け替え、Targetをdownloadedへ戻す。"""
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise LookupError(f"Artifact {artifact_id} が見つかりません")
    if artifact.missing_since is None:
        raise ValueError("missingではないArtifactは手動リンクできません")
    normalized = validate_relative_path(candidate_path)
    tracked = session.scalar(
        select(Artifact.id).where(
            Artifact.storage_id == artifact.storage_id,
            Artifact.relative_path == normalized,
            Artifact.id != artifact.id,
        )
    )
    if tracked is not None:
        raise ValueError("選択したファイルは別のArtifactが追跡中です")
    if not adapter.exists(normalized):
        raise ValueError("選択したファイルがStorageに存在しません")
    artifact.manual_link_previous_path = artifact.relative_path
    artifact.manual_linked_at = now or datetime.now(UTC)
    artifact.relative_path = normalized
    artifact.absolute_path_cache = None
    artifact.missing_since = None
    target = session.get(Target, artifact.target_id)
    assert target is not None
    remaining = session.scalar(
        select(Artifact.id).where(
            Artifact.target_id == target.id,
            Artifact.id != artifact.id,
            Artifact.missing_since.is_not(None),
        )
    )
    if remaining is None:
        target.status = TargetStatus.DOWNLOADED
    session.commit()


def undo_manual_link(
    session: Session, artifact_id: int, *, now: datetime | None = None
) -> None:
    """直前のDBパスへ戻し、Targetをmissingへ戻す。ファイルは動かさない。"""
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise LookupError(f"Artifact {artifact_id} が見つかりません")
    if artifact.manual_link_previous_path is None:
        raise ValueError("取り消せる手動リンクがありません")
    artifact.relative_path = artifact.manual_link_previous_path
    artifact.absolute_path_cache = None
    artifact.manual_link_previous_path = None
    artifact.manual_linked_at = None
    artifact.missing_since = now or datetime.now(UTC)
    target = session.get(Target, artifact.target_id)
    assert target is not None
    target.status = TargetStatus.MISSING
    session.commit()


__all__ = [
    "AdapterFactory",
    "IntegrityIssue",
    "IntegrityReport",
    "check_integrity",
    "list_orphan_files",
    "manual_link",
    "set_missing_action",
    "undo_manual_link",
]
