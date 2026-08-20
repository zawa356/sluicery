"""Playlist folder_name の明示変更とArtifact移動の安全境界。"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import json
import logging
import os
import stat
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select, text
from sqlalchemy.orm import Session, sessionmaker

from sluicery.core.naming import NamingValidationError, sanitize_component
from sluicery.core.settings import OperationalSettings
from sluicery.core.sync import playlist_sync_is_active
from sluicery.db.models import (
    Artifact,
    Item,
    Playlist,
    PlaylistProfile,
    Profile,
    Run,
    RunStatus,
    RunTrigger,
    Storage,
    StorageKind,
    Target,
)
from sluicery.hooks import EventLogHook, Hook, emit_safely
from sluicery.layout import LayoutContext, LayoutValidationError, resolve_subpath
from sluicery.storage.base import RemoteFile, StorageAdapter, StorageOperationError

logger = logging.getLogger(__name__)


class FolderMovePlanError(ValueError):
    """入力または現在の配置から安全な移動計画を作れない。"""


class FolderMoveConfirmationError(RuntimeError):
    """確認tokenが不正、期限切れ、または現在状態と一致しない。"""


class FolderMoveExecutionError(RuntimeError):
    """実ファイル移動または移動後のDB反映に失敗した。"""


@dataclass(frozen=True)
class FolderMoveCandidate:
    artifact_id: int
    target_id: int
    assignment_id: int
    profile_id: int
    storage_id: int
    source_path: str
    destination_path: str
    artifact_updated_at: str
    target_updated_at: str
    assignment_updated_at: str
    profile_updated_at: str
    storage_updated_at: str
    storage_config_fingerprint: str
    file_identity: RemoteFile | None
    remote_or_mount: bool
    already_moved: bool = False


@dataclass(frozen=True)
class FolderMovePlan:
    playlist_id: int
    old_folder_name: str
    new_folder_name: str
    playlist_updated_at: str
    candidates: tuple[FolderMoveCandidate, ...]
    unaffected_count: int
    blocked_reasons: tuple[str, ...]
    recovery_run_ids: tuple[int, ...] = ()

    @property
    def move_count(self) -> int:
        return len(self.candidates)

    @property
    def remote_count(self) -> int:
        return sum(candidate.remote_or_mount for candidate in self.candidates)

    @property
    def movable(self) -> bool:
        return self.old_folder_name != self.new_folder_name and not self.blocked_reasons


@dataclass(frozen=True)
class FolderMoveConfirmation:
    playlist_id: int
    old_folder_name: str
    new_folder_name: str
    fingerprint: str
    issued_at: datetime


@dataclass(frozen=True)
class FolderMoveExecutionResult:
    run_id: int
    moved_count: int
    total_count: int


def _storage_fingerprint(storage: Storage) -> str:
    encoded = json.dumps(
        {"kind": storage.kind.value, "config": storage.config_json},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _plan_fingerprint(plan: FolderMovePlan) -> str:
    encoded = json.dumps(
        {
            "playlist_id": plan.playlist_id,
            "old_folder_name": plan.old_folder_name,
            "new_folder_name": plan.new_folder_name,
            "playlist_updated_at": plan.playlist_updated_at,
            "candidates": [asdict(candidate) for candidate in plan.candidates],
            "unaffected_count": plan.unaffected_count,
            "recovery_run_ids": plan.recovery_run_ids,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _replace_prefix(path: str, old_prefix: str, new_prefix: str) -> str | None:
    current = PurePosixPath(path)
    old = PurePosixPath(old_prefix)
    try:
        suffix = current.relative_to(old)
    except ValueError:
        return None
    return (PurePosixPath(new_prefix) / suffix).as_posix()


def _identity_matches(current: RemoteFile, expected: RemoteFile) -> bool:
    return (
        current.size == expected.size
        and current.modified_at == expected.modified_at
        and current.hashes == expected.hashes
        and current.file_id == expected.file_id
    )


@dataclass(frozen=True)
class _PlanRowSnapshot:
    artifact_id: int
    target_id: int
    assignment_id: int
    profile_id: int
    storage_id: int
    artifact_path: str
    artifact_filesize: int | None
    artifact_updated_at: str
    target_updated_at: str
    assignment_subpath: str
    assignment_storage_id: int
    assignment_updated_at: str
    profile_name: str
    profile_kind: str
    profile_updated_at: str
    storage: Storage
    storage_updated_at: str
    storage_fingerprint: str


def _remote_file_from_json(value: object) -> RemoteFile | None:
    if not isinstance(value, dict):
        return None
    relative_path = value.get("relative_path")
    size = value.get("size")
    modified_at = value.get("modified_at")
    hashes = value.get("hashes")
    file_id = value.get("file_id")
    if (
        not isinstance(relative_path, str)
        or (size is not None and (isinstance(size, bool) or not isinstance(size, int)))
        or (modified_at is not None and not isinstance(modified_at, str))
        or not isinstance(hashes, dict)
        or not all(isinstance(key, str) and isinstance(item, str) for key, item in hashes.items())
        or (file_id is not None and not isinstance(file_id, str))
    ):
        return None
    return RemoteFile(relative_path, size, modified_at, dict(hashes), file_id)


def _pending_intent_for_row(
    intents: dict[int, tuple[int, dict]],
    row: _PlanRowSnapshot,
    destination: str,
) -> tuple[int, RemoteFile] | None:
    pending = intents.get(row.artifact_id)
    if pending is None:
        return None
    run_id, intent = pending
    if (
        intent.get("artifact_id") != row.artifact_id
        or intent.get("storage_id") != row.storage_id
        or intent.get("source_path") != row.artifact_path
        or intent.get("destination_path") != destination
        or intent.get("artifact_updated_at") != row.artifact_updated_at
        or intent.get("storage_updated_at") != row.storage_updated_at
        or intent.get("storage_config_fingerprint") != row.storage_fingerprint
    ):
        return None
    identity = _remote_file_from_json(intent.get("file_identity"))
    if identity is None:
        return None
    return run_id, identity


def build_folder_move_plan(
    session: Session,
    playlist_id: int,
    new_folder_name: str,
    *,
    adapter_factory: Callable[[Storage], StorageAdapter] | None = None,
) -> FolderMovePlan:
    """DB snapshotを閉じてから実体を読み、移動元・移動先の確認計画を作る。"""
    playlist = session.get(Playlist, playlist_id)
    if playlist is None:
        raise LookupError("Playlistが見つかりません")
    try:
        normalized_new = sanitize_component(new_folder_name)
    except NamingValidationError as exc:
        raise FolderMovePlanError(str(exc)) from exc
    if normalized_new != new_folder_name:
        raise FolderMovePlanError(
            f"フォルダ名は安全化後の値 `{normalized_new}` で再入力してください"
        )
    if normalized_new == playlist.folder_name:
        raise FolderMovePlanError("現在と異なるフォルダ名を入力してください")

    playlist_name = playlist.name
    old_folder_name = playlist.folder_name
    playlist_updated_at = playlist.updated_at.isoformat()
    orm_rows = list(
        session.execute(
            select(Artifact, Target, PlaylistProfile, Profile, Storage)
            .join(Target, Artifact.target_id == Target.id)
            .join(Item, Target.item_id == Item.id)
            .join(
                PlaylistProfile,
                Target.playlist_profile_id == PlaylistProfile.id,
            )
            .join(Profile, PlaylistProfile.profile_id == Profile.id)
            .join(Storage, Artifact.storage_id == Storage.id)
            .where(Item.playlist_id == playlist_id)
            .order_by(Artifact.id)
        )
    )
    snapshots: list[_PlanRowSnapshot] = []
    storage_snapshots: dict[int, Storage] = {}
    for artifact, target, assignment, profile, storage in orm_rows:
        detached_storage = storage_snapshots.get(storage.id)
        if detached_storage is None:
            detached_storage = Storage(
                id=storage.id,
                name=storage.name,
                kind=storage.kind,
                enabled=storage.enabled,
                config_json=dict(storage.config_json or {}),
                credentials_encrypted=storage.credentials_encrypted,
                updated_at=storage.updated_at,
            )
            storage_snapshots[storage.id] = detached_storage
        snapshots.append(
            _PlanRowSnapshot(
                artifact.id,
                target.id,
                assignment.id,
                profile.id,
                storage.id,
                artifact.relative_path,
                artifact.filesize,
                artifact.updated_at.isoformat(),
                target.updated_at.isoformat(),
                assignment.subpath,
                assignment.storage_id,
                assignment.updated_at.isoformat(),
                profile.name,
                profile.kind.value,
                profile.updated_at.isoformat(),
                detached_storage,
                storage.updated_at.isoformat(),
                _storage_fingerprint(storage),
            )
        )
    pending_runs = list(
        session.scalars(
            select(Run).where(
                Run.playlist_id == playlist_id,
                Run.kind == "folder_move",
                Run.status.in_((RunStatus.RUNNING, RunStatus.FAILED)),
            )
        )
    )
    pending_intents: dict[int, tuple[int, dict]] = {}
    recovery_run_ids: set[int] = set()
    for pending_run in pending_runs:
        stats = pending_run.stats_json if isinstance(pending_run.stats_json, dict) else {}
        if (
            stats.get("old_folder_name") != old_folder_name
            or stats.get("new_folder_name") != normalized_new
        ):
            continue
        if pending_run.status == RunStatus.RUNNING:
            recovery_run_ids.add(pending_run.id)
        intent = stats.get("current_intent")
        if isinstance(intent, dict) and isinstance(intent.get("artifact_id"), int):
            pending_intents[int(intent["artifact_id"])] = (pending_run.id, intent)

    # remote hash取得中にSQLite read transactionを保持しない。
    session.rollback()
    adapters: dict[int, StorageAdapter] = {}
    candidates: list[FolderMoveCandidate] = []
    blocked: list[str] = []
    unaffected = 0
    destinations: set[tuple[int, str]] = set()

    for row in snapshots:
        storage = row.storage
        if row.assignment_storage_id != row.storage_id:
            blocked.append(
                f"Artifact {row.artifact_id} は現在のProfile割当とStorageが一致しません"
            )
            continue
        try:
            old_prefix = resolve_subpath(
                row.assignment_subpath,
                LayoutContext(
                    playlist_name=playlist_name,
                    playlist_folder_name=old_folder_name,
                    profile_name=row.profile_name,
                    profile_kind=row.profile_kind,
                    subpath=row.assignment_subpath,
                ),
            )
            new_prefix = resolve_subpath(
                row.assignment_subpath,
                LayoutContext(
                    playlist_name=playlist_name,
                    playlist_folder_name=normalized_new,
                    profile_name=row.profile_name,
                    profile_kind=row.profile_kind,
                    subpath=row.assignment_subpath,
                ),
            )
        except LayoutValidationError as exc:
            blocked.append(f"Profile割当 {row.assignment_id} のsubpathを解決できません: {exc}")
            continue
        if old_prefix == new_prefix:
            unaffected += 1
            continue
        destination = _replace_prefix(row.artifact_path, old_prefix, new_prefix)
        if destination is None:
            # 手動relink等でテンプレート外にあるArtifactは勝手に移動しない。
            unaffected += 1
            continue
        destination_key = (row.storage_id, destination)
        if destination_key in destinations:
            blocked.append(f"移動先が重複しています: Storage {storage.id} / {destination}")
            continue
        destinations.add(destination_key)

        identity: RemoteFile | None = None
        already_moved = False
        if adapter_factory is not None:
            try:
                adapter = adapters.get(row.storage_id)
                if adapter is None:
                    adapter = adapter_factory(storage)
                    adapters[row.storage_id] = adapter
                pending = _pending_intent_for_row(pending_intents, row, destination)
                if adapter.exists(row.artifact_path):
                    identity = adapter.inspect_file(row.artifact_path)
                    if adapter.exists(destination):
                        raise StorageOperationError(
                            "移動元と移動先の両方が存在します",
                            reason_code="ambiguous_move_state",
                        )
                elif pending is not None and adapter.exists(destination):
                    _pending_run_id, expected = pending
                    moved_identity = adapter.inspect_file(destination)
                    if not _identity_matches(moved_identity, expected):
                        raise StorageOperationError(
                            "移動先の識別情報が永続intentと一致しません",
                            reason_code="identity_mismatch",
                        )
                    identity = expected
                    already_moved = True
                else:
                    raise StorageOperationError(
                        "移動元が存在せず、安全に回収できるintentもありません",
                        reason_code="source_missing",
                    )
                if row.artifact_filesize is not None and identity.size != row.artifact_filesize:
                    raise StorageOperationError(
                        "DB記録と実ファイルのsizeが一致しません",
                        reason_code="size_mismatch",
                    )
            except (OSError, StorageOperationError, ValueError) as exc:
                blocked.append(f"Artifact {row.artifact_id} を安全に確認できません: {exc}")

        candidates.append(
            FolderMoveCandidate(
                artifact_id=row.artifact_id,
                target_id=row.target_id,
                assignment_id=row.assignment_id,
                profile_id=row.profile_id,
                storage_id=row.storage_id,
                source_path=row.artifact_path,
                destination_path=destination,
                artifact_updated_at=row.artifact_updated_at,
                target_updated_at=row.target_updated_at,
                assignment_updated_at=row.assignment_updated_at,
                profile_updated_at=row.profile_updated_at,
                storage_updated_at=row.storage_updated_at,
                storage_config_fingerprint=row.storage_fingerprint,
                file_identity=identity,
                remote_or_mount=storage.kind in {StorageKind.REMOTE, StorageKind.MOUNT},
                already_moved=already_moved,
            )
        )

    return FolderMovePlan(
        playlist_id=playlist_id,
        old_folder_name=old_folder_name,
        new_folder_name=normalized_new,
        playlist_updated_at=playlist_updated_at,
        candidates=tuple(candidates),
        unaffected_count=unaffected,
        blocked_reasons=tuple(blocked),
        recovery_run_ids=tuple(sorted(recovery_run_ids)),
    )


class FolderMoveConfirmationSigner:
    def __init__(
        self,
        secret_key: str,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._serializer = URLSafeSerializer(secret_key, salt="sluicery-folder-move-v1")
        self._clock = clock

    def issue(self, plan: FolderMovePlan) -> str:
        if not plan.movable:
            raise FolderMovePlanError("安全に実行できない計画へ確認tokenは発行できません")
        return self._serializer.dumps(
            {
                "playlist_id": plan.playlist_id,
                "old_folder_name": plan.old_folder_name,
                "new_folder_name": plan.new_folder_name,
                "fingerprint": _plan_fingerprint(plan),
                "issued_at": self._clock().timestamp(),
            }
        )

    def load(self, token: str, *, ttl_sec: int) -> FolderMoveConfirmation:
        if ttl_sec <= 0:
            raise ValueError("確認token TTLは1以上にしてください")
        try:
            payload = self._serializer.loads(token)
            if not isinstance(payload, dict):
                raise FolderMoveConfirmationError("フォルダ移動の確認tokenが不正です")
            issued_at = datetime.fromtimestamp(float(payload["issued_at"]), UTC)
            confirmation = FolderMoveConfirmation(
                playlist_id=int(payload["playlist_id"]),
                old_folder_name=str(payload["old_folder_name"]),
                new_folder_name=str(payload["new_folder_name"]),
                fingerprint=str(payload["fingerprint"]),
                issued_at=issued_at,
            )
        except (BadSignature, KeyError, TypeError, ValueError) as exc:
            raise FolderMoveConfirmationError(
                "フォルダ移動の確認tokenが不正です"
            ) from exc
        age = (self._clock() - confirmation.issued_at).total_seconds()
        if age < 0 or age > ttl_sec:
            raise FolderMoveConfirmationError(
                "フォルダ移動の確認期限が切れました。再確認してください"
            )
        return confirmation

    @staticmethod
    def verify_plan(
        confirmation: FolderMoveConfirmation,
        plan: FolderMovePlan,
    ) -> None:
        if (
            confirmation.playlist_id != plan.playlist_id
            or confirmation.old_folder_name != plan.old_folder_name
            or confirmation.new_folder_name != plan.new_folder_name
            or confirmation.fingerprint != _plan_fingerprint(plan)
        ):
            raise FolderMoveConfirmationError(
                "確認後に移動対象が変化しました。再確認してください"
            )


def _candidate_rows_match(session: Session, candidate: FolderMoveCandidate) -> bool:
    artifact = session.get(Artifact, candidate.artifact_id)
    target = session.get(Target, candidate.target_id)
    assignment = session.get(PlaylistProfile, candidate.assignment_id)
    profile = session.get(Profile, candidate.profile_id)
    storage = session.get(Storage, candidate.storage_id)
    return bool(
        artifact is not None
        and artifact.target_id == candidate.target_id
        and artifact.storage_id == candidate.storage_id
        and artifact.relative_path == candidate.source_path
        and artifact.updated_at.isoformat() == candidate.artifact_updated_at
        and target is not None
        and target.playlist_profile_id == candidate.assignment_id
        and target.updated_at.isoformat() == candidate.target_updated_at
        and assignment is not None
        and assignment.profile_id == candidate.profile_id
        and assignment.storage_id == candidate.storage_id
        and assignment.updated_at.isoformat() == candidate.assignment_updated_at
        and profile is not None
        and profile.updated_at.isoformat() == candidate.profile_updated_at
        and storage is not None
        and storage.updated_at.isoformat() == candidate.storage_updated_at
        and _storage_fingerprint(storage) == candidate.storage_config_fingerprint
    )


@contextmanager
def _playlist_move_lock(data_dir: Path, playlist_id: int):
    lock_dir = data_dir / "locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    if lock_dir.is_symlink() or not lock_dir.is_dir():
        raise FolderMoveExecutionError("フォルダ移動lock directoryが安全ではありません")
    lock_path = lock_dir / f"folder-move-{playlist_id}.lock"
    flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise FolderMoveExecutionError("フォルダ移動lockを作成できません") from exc
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise FolderMoveExecutionError(
                    "同じPlaylistのフォルダ移動が現在実行中です"
                ) from exc
            raise
        yield
    finally:
        os.close(descriptor)


def _write_move_journal(log_path: Path, event: str, values: dict) -> None:
    payload = {
        "at": datetime.now(UTC).isoformat(),
        "event": event,
        **values,
    }
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(log_path, flags, 0o600)
    try:
        os.write(descriptor, encoded)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_move_journal_after_commit(log_path: Path, event: str, values: dict) -> None:
    """durable DB成功後の補助log失敗で、成功状態を巻き戻したりFAILEDへ変えない。"""
    try:
        _write_move_journal(log_path, event, values)
    except OSError:
        logger.warning("Folder move post-commit journal write failed", exc_info=True)


def _run_stats(
    plan: FolderMovePlan,
    moved_count: int,
    *,
    current_intent: dict | None = None,
    reason_code: str | None = None,
) -> dict:
    stats: dict = {
        "moved_count": moved_count,
        "total_count": plan.move_count,
        "unaffected_count": plan.unaffected_count,
        "old_folder_name": plan.old_folder_name,
        "new_folder_name": plan.new_folder_name,
    }
    if current_intent is not None:
        stats["current_intent"] = current_intent
    if reason_code is not None:
        stats["reason_code"] = reason_code
    return stats


def execute_folder_move(
    session_factory: sessionmaker[Session],
    plan: FolderMovePlan,
    *,
    adapter_factory: Callable[[Storage, OperationalSettings], StorageAdapter],
    confirmation_token: str,
    confirmation_signer: FolderMoveConfirmationSigner,
    confirmation_ttl_sec: int,
    data_dir: Path,
    hook: Hook | None = None,
) -> FolderMoveExecutionResult:
    """永続intentを先に記録し、確認済みの実体とDBを1件ずつ進める。"""
    confirmation = confirmation_signer.load(
        confirmation_token, ttl_sec=confirmation_ttl_sec
    )
    confirmation_signer.verify_plan(confirmation, plan)
    if not plan.movable:
        raise FolderMovePlanError("安全に実行できないフォルダ移動計画です")
    if any(candidate.file_identity is None for candidate in plan.candidates):
        raise FolderMovePlanError("実ファイル確認を含むプレビューを再実行してください")
    event_hook = hook or EventLogHook(session_factory)
    # Run作成前にjournal directory自体の安全性を確定する。
    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    if log_dir.is_symlink() or not log_dir.is_dir():
        raise FolderMoveExecutionError("Run log directoryが安全ではありません")
    with _playlist_move_lock(data_dir, plan.playlist_id):
        interrupted_run_ids: list[int] = []
        with session_factory() as session:
            if session.in_transaction():
                session.rollback()
            session.execute(text("BEGIN IMMEDIATE"))
            for recovery_run_id in plan.recovery_run_ids:
                recovery_run = session.get(Run, recovery_run_id)
                stats = (
                    recovery_run.stats_json
                    if recovery_run is not None and isinstance(recovery_run.stats_json, dict)
                    else {}
                )
                if (
                    recovery_run is None
                    or recovery_run.kind != "folder_move"
                    or recovery_run.playlist_id != plan.playlist_id
                    or recovery_run.status != RunStatus.RUNNING
                    or stats.get("old_folder_name") != plan.old_folder_name
                    or stats.get("new_folder_name") != plan.new_folder_name
                ):
                    session.rollback()
                    raise FolderMoveConfirmationError(
                        "回収対象のフォルダ移動Runが変化しました。再確認してください"
                    )
                recovery_run.status = RunStatus.FAILED
                recovery_run.finished_at = datetime.now(UTC)
                recovery_run.stats_json = {**stats, "reason_code": "process_interrupted"}
                interrupted_run_ids.append(recovery_run.id)
            session.flush()
            if playlist_sync_is_active(session, plan.playlist_id):
                session.rollback()
                raise FolderMoveExecutionError(
                    "Playlistの同期またはフォルダ移動が実行中です"
                )
            playlist = session.get(Playlist, plan.playlist_id)
            if (
                playlist is None
                or playlist.folder_name != plan.old_folder_name
                or playlist.updated_at.isoformat() != plan.playlist_updated_at
                or any(not _candidate_rows_match(session, row) for row in plan.candidates)
            ):
                session.rollback()
                raise FolderMoveConfirmationError(
                    "確認後にPlaylistまたは移動対象が変化しました。再確認してください"
                )
            storages: dict[int, Storage] = {}
            for storage_id in {row.storage_id for row in plan.candidates}:
                stored = session.get(Storage, storage_id)
                if stored is None:
                    session.rollback()
                    raise FolderMoveExecutionError("移動対象のStorageが見つかりません")
                storages[storage_id] = Storage(
                    id=stored.id,
                    name=stored.name,
                    kind=stored.kind,
                    enabled=stored.enabled,
                    config_json=dict(stored.config_json or {}),
                    credentials_encrypted=stored.credentials_encrypted,
                    updated_at=stored.updated_at,
                )
            run = Run(
                trigger=RunTrigger.MANUAL,
                kind="folder_move",
                playlist_id=plan.playlist_id,
                status=RunStatus.RUNNING,
                stats_json=_run_stats(plan, 0),
            )
            session.add(run)
            session.commit()
            run_id = run.id

        for interrupted_run_id in interrupted_run_ids:
            emit_safely(
                event_hook,
                "run_failed",
                {
                    "run_id": interrupted_run_id,
                    "playlist_id": plan.playlist_id,
                    "kind": "folder_move",
                    "reason_code": "process_interrupted",
                },
            )

        log_path = log_dir / f"run-{run_id}.log"
        emit_safely(
            event_hook,
            "run_started",
            {
                "run_id": run_id,
                "playlist_id": plan.playlist_id,
                "kind": "folder_move",
                "trigger": RunTrigger.MANUAL.value,
            },
        )
        try:
            _write_move_journal(
                log_path, "run_started", {"playlist_id": plan.playlist_id}
            )
            directory_fd = os.open(log_dir, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
            with session_factory() as session:
                stored_run = session.get(Run, run_id)
                assert stored_run is not None
                stored_run.log_path = str(log_path.resolve())
                session.commit()
        except Exception as exc:  # noqa: BLE001 - RUNNINGを残さない初期化境界
            durable_log_path: str | None = None
            try:
                if stat.S_ISREG(os.lstat(log_path).st_mode):
                    durable_log_path = str(log_path.absolute())
            except OSError:
                pass
            with session_factory() as session:
                failed_run = session.get(Run, run_id)
                if failed_run is not None:
                    failed_run.status = RunStatus.FAILED
                    failed_run.finished_at = datetime.now(UTC)
                    failed_run.stats_json = _run_stats(
                        plan, 0, reason_code="journal_initialization_failed"
                    )
                    failed_run.log_path = durable_log_path
                    session.commit()
            emit_safely(
                event_hook,
                "run_failed",
                {
                    "run_id": run_id,
                    "playlist_id": plan.playlist_id,
                    "kind": "folder_move",
                    "reason_code": "journal_initialization_failed",
                },
            )
            raise FolderMoveExecutionError(
                "フォルダ移動の永続記録を初期化できませんでした"
            ) from exc

        adapters: dict[int, StorageAdapter] = {}
        moved_count = 0
        current_intent: dict | None = None
        try:
            for candidate in plan.candidates:
                with session_factory() as session:
                    playlist = session.get(Playlist, plan.playlist_id)
                    if (
                        playlist is None
                        or playlist.folder_name != plan.old_folder_name
                        or playlist.updated_at.isoformat() != plan.playlist_updated_at
                        or not _candidate_rows_match(session, candidate)
                    ):
                        raise FolderMoveConfirmationError(
                            "移動直前に対象が変化しました。再確認してください"
                        )
                    current_run = session.get(Run, run_id)
                    assert current_run is not None
                    current_intent = asdict(candidate)
                    current_run.stats_json = _run_stats(
                        plan, moved_count, current_intent=current_intent
                    )
                    session.commit()
                _write_move_journal(log_path, "move_intent", current_intent)

                storage = storages[candidate.storage_id]
                adapter = adapters.get(candidate.storage_id)
                if adapter is None:
                    with session_factory() as settings_session:
                        adapter = adapter_factory(
                            storage,
                            OperationalSettings(settings_session),
                        )
                    adapters[candidate.storage_id] = adapter
                expected = candidate.file_identity
                assert expected is not None
                if candidate.already_moved:
                    if adapter.exists(candidate.source_path):
                        raise FolderMoveExecutionError(
                            "回収対象の移動元が再作成されたため自動処理を停止しました"
                        )
                    moved = adapter.inspect_file(candidate.destination_path)
                else:
                    current = adapter.inspect_file(candidate.source_path)
                    if not _identity_matches(current, expected):
                        raise FolderMoveConfirmationError(
                            "確認後に移動元ファイルが変化しました。再確認してください"
                        )
                    if adapter.exists(candidate.destination_path):
                        raise FolderMoveExecutionError(
                            "移動先が作成されたため上書きせず停止しました"
                        )
                    try:
                        adapter.move(candidate.source_path, candidate.destination_path)
                    except Exception:
                        # remote timeout等で応答だけ失われても、強いidentityで完了を回収する。
                        if adapter.exists(candidate.source_path) or not adapter.exists(
                            candidate.destination_path
                        ):
                            raise
                        ambiguous = adapter.inspect_file(candidate.destination_path)
                        if not _identity_matches(ambiguous, expected):
                            raise
                    moved = adapter.inspect_file(candidate.destination_path)
                if not _identity_matches(moved, expected):
                    raise FolderMoveExecutionError(
                        "移動後のファイル識別情報が一致しないため自動処理を停止しました"
                    )
                _write_move_journal(log_path, "physical_move_completed", current_intent)
                # commitは成功したが応答だけ例外、という状態を呼出側から判別できない。
                # commit試行後は物理fileを戻さず、失敗時はintentを保持して次回照合する。
                with session_factory() as session:
                    if not _candidate_rows_match(session, candidate):
                        raise FolderMoveConfirmationError(
                            "ファイル移動中にDB上の対象が変化しました"
                        )
                    artifact = session.get(Artifact, candidate.artifact_id)
                    current_run = session.get(Run, run_id)
                    assert artifact is not None and current_run is not None
                    artifact.relative_path = candidate.destination_path
                    artifact.absolute_path_cache = None
                    next_moved_count = moved_count + 1
                    current_run.stats_json = _run_stats(plan, next_moved_count)
                    session.commit()
                moved_count = next_moved_count
                current_intent = None
                _write_move_journal_after_commit(
                    log_path,
                    "database_move_committed",
                    {"artifact_id": candidate.artifact_id},
                )

            with session_factory() as session:
                playlist = session.get(Playlist, plan.playlist_id)
                final_run = session.get(Run, run_id)
                if (
                    playlist is None
                    or final_run is None
                    or playlist.folder_name != plan.old_folder_name
                    or playlist.updated_at.isoformat() != plan.playlist_updated_at
                ):
                    raise FolderMoveConfirmationError(
                        "完了直前にPlaylistが変化したためフォルダ名を更新できません"
                    )
                playlist.folder_name = plan.new_folder_name
                final_run.status = RunStatus.SUCCEEDED
                final_run.finished_at = datetime.now(UTC)
                final_run.stats_json = _run_stats(plan, moved_count)
                session.commit()
            _write_move_journal_after_commit(
                log_path, "run_finished", {"moved_count": moved_count}
            )
        except Exception as exc:  # noqa: BLE001 - 外部I/O境界でRunを必ず終端する
            with session_factory() as session:
                failed_run = session.get(Run, run_id)
                if failed_run is not None:
                    failed_run.status = RunStatus.FAILED
                    failed_run.finished_at = datetime.now(UTC)
                    failed_run.stats_json = _run_stats(
                        plan,
                        moved_count,
                        current_intent=current_intent,
                        reason_code="folder_move_failed",
                    )
                    session.commit()
            _write_move_journal_after_commit(
                log_path, "run_failed", {"moved_count": moved_count}
            )
            emit_safely(
                event_hook,
                "run_failed",
                {
                    "run_id": run_id,
                    "playlist_id": plan.playlist_id,
                    "kind": "folder_move",
                    "reason_code": "folder_move_failed",
                },
            )
            if isinstance(exc, FolderMoveConfirmationError):
                raise
            raise FolderMoveExecutionError(
                "フォルダ移動の途中で失敗しました。永続記録から再実行できます"
            ) from exc

        emit_safely(
            event_hook,
            "run_finished",
            {
                "run_id": run_id,
                "playlist_id": plan.playlist_id,
                "kind": "folder_move",
                "status": RunStatus.SUCCEEDED.value,
            },
        )
        return FolderMoveExecutionResult(run_id, moved_count, plan.move_count)


__all__ = [
    "FolderMoveCandidate",
    "FolderMoveConfirmation",
    "FolderMoveConfirmationError",
    "FolderMoveConfirmationSigner",
    "FolderMoveExecutionError",
    "FolderMoveExecutionResult",
    "FolderMovePlan",
    "FolderMovePlanError",
    "build_folder_move_plan",
    "execute_folder_move",
]
