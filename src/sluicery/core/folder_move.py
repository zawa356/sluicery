"""Playlist folder_name の明示変更とArtifact移動の安全境界。"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import PurePosixPath

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sluicery.core.naming import NamingValidationError, sanitize_component
from sluicery.core.settings import OperationalSettings
from sluicery.core.sync import (
    SyncAlreadyRunningError,
    lock_and_validate_playlist_operation_start,
)
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


@dataclass(frozen=True)
class FolderMovePlan:
    playlist_id: int
    old_folder_name: str
    new_folder_name: str
    playlist_updated_at: str
    candidates: tuple[FolderMoveCandidate, ...]
    unaffected_count: int
    blocked_reasons: tuple[str, ...]

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


def build_folder_move_plan(
    session: Session,
    playlist_id: int,
    new_folder_name: str,
    *,
    adapter_factory: Callable[[Storage], StorageAdapter] | None = None,
) -> FolderMovePlan:
    """DBと実体の読み取りだけで、移動元・移動先の確認snapshotを作る。"""
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

    rows = list(
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
    adapters: dict[int, StorageAdapter] = {}
    candidates: list[FolderMoveCandidate] = []
    blocked: list[str] = []
    unaffected = 0
    destinations: set[tuple[int, str]] = set()

    for artifact, target, assignment, profile, storage in rows:
        if assignment.storage_id != artifact.storage_id:
            blocked.append(
                f"Artifact {artifact.id} は現在のProfile割当とStorageが一致しません"
            )
            continue
        try:
            old_prefix = resolve_subpath(
                assignment.subpath,
                LayoutContext(
                    playlist_name=playlist.name,
                    playlist_folder_name=playlist.folder_name,
                    profile_name=profile.name,
                    profile_kind=profile.kind.value,
                    subpath=assignment.subpath,
                ),
            )
            new_prefix = resolve_subpath(
                assignment.subpath,
                LayoutContext(
                    playlist_name=playlist.name,
                    playlist_folder_name=normalized_new,
                    profile_name=profile.name,
                    profile_kind=profile.kind.value,
                    subpath=assignment.subpath,
                ),
            )
        except LayoutValidationError as exc:
            blocked.append(f"Profile割当 {assignment.id} のsubpathを解決できません: {exc}")
            continue
        if old_prefix == new_prefix:
            unaffected += 1
            continue
        destination = _replace_prefix(artifact.relative_path, old_prefix, new_prefix)
        if destination is None:
            # 手動relink等でテンプレート外にあるArtifactは勝手に移動しない。
            unaffected += 1
            continue
        destination_key = (storage.id, destination)
        if destination_key in destinations:
            blocked.append(f"移動先が重複しています: Storage {storage.id} / {destination}")
            continue
        destinations.add(destination_key)

        identity: RemoteFile | None = None
        if adapter_factory is not None:
            try:
                adapter = adapters.get(storage.id)
                if adapter is None:
                    adapter = adapter_factory(storage)
                    adapters[storage.id] = adapter
                identity = adapter.inspect_file(artifact.relative_path)
                if artifact.filesize is not None and identity.size != artifact.filesize:
                    raise StorageOperationError(
                        "DB記録と実ファイルのsizeが一致しません",
                        reason_code="size_mismatch",
                    )
                if adapter.exists(destination):
                    raise StorageOperationError(
                        "移動先が既に存在します",
                        reason_code="destination_exists",
                    )
            except (OSError, StorageOperationError, ValueError) as exc:
                blocked.append(f"Artifact {artifact.id} を安全に確認できません: {exc}")

        candidates.append(
            FolderMoveCandidate(
                artifact_id=artifact.id,
                target_id=target.id,
                assignment_id=assignment.id,
                profile_id=profile.id,
                storage_id=storage.id,
                source_path=artifact.relative_path,
                destination_path=destination,
                artifact_updated_at=artifact.updated_at.isoformat(),
                target_updated_at=target.updated_at.isoformat(),
                assignment_updated_at=assignment.updated_at.isoformat(),
                profile_updated_at=profile.updated_at.isoformat(),
                storage_updated_at=storage.updated_at.isoformat(),
                storage_config_fingerprint=_storage_fingerprint(storage),
                file_identity=identity,
                remote_or_mount=storage.kind in {StorageKind.REMOTE, StorageKind.MOUNT},
            )
        )

    return FolderMovePlan(
        playlist_id=playlist.id,
        old_folder_name=playlist.folder_name,
        new_folder_name=normalized_new,
        playlist_updated_at=playlist.updated_at.isoformat(),
        candidates=tuple(candidates),
        unaffected_count=unaffected,
        blocked_reasons=tuple(blocked),
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


def execute_folder_move(
    session_factory: sessionmaker[Session],
    plan: FolderMovePlan,
    *,
    adapter_factory: Callable[[Storage, OperationalSettings], StorageAdapter],
    confirmation_token: str,
    confirmation_signer: FolderMoveConfirmationSigner,
    confirmation_ttl_sec: int,
    hook: Hook | None = None,
) -> FolderMoveExecutionResult:
    """確認済みの実体を1件ずつ移動し、成功分を即時DBへ反映する。"""
    confirmation = confirmation_signer.load(
        confirmation_token, ttl_sec=confirmation_ttl_sec
    )
    confirmation_signer.verify_plan(confirmation, plan)
    if not plan.movable:
        raise FolderMovePlanError("安全に実行できないフォルダ移動計画です")
    if any(candidate.file_identity is None for candidate in plan.candidates):
        raise FolderMovePlanError("実ファイル確認を含むプレビューを再実行してください")

    with session_factory() as session:
        try:
            lock_and_validate_playlist_operation_start(session, plan.playlist_id)
        except SyncAlreadyRunningError as exc:
            raise FolderMoveExecutionError(
                "Playlistの同期が実行中のためフォルダを移動できません"
            ) from exc
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
        storages = {
            storage_id: session.get(Storage, storage_id)
            for storage_id in {row.storage_id for row in plan.candidates}
        }
        if any(storage is None for storage in storages.values()):
            session.rollback()
            raise FolderMoveExecutionError("移動対象のStorageが見つかりません")
        run = Run(
            trigger=RunTrigger.MANUAL,
            kind="folder_move",
            playlist_id=plan.playlist_id,
            status=RunStatus.RUNNING,
            stats_json={
                "moved_count": 0,
                "total_count": plan.move_count,
                "unaffected_count": plan.unaffected_count,
            },
        )
        session.add(run)
        session.commit()
        run_id = run.id

    event_hook = hook or EventLogHook(session_factory)
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
    adapters: dict[int, StorageAdapter] = {}
    moved_count = 0
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
            storage = storages[candidate.storage_id]
            assert storage is not None
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
            current = adapter.inspect_file(candidate.source_path)
            if not _identity_matches(current, expected):
                raise FolderMoveConfirmationError(
                    "確認後に移動元ファイルが変化しました。再確認してください"
                )
            if adapter.exists(candidate.destination_path):
                raise FolderMoveExecutionError(
                    "移動先が作成されたため上書きせず停止しました"
                )
            adapter.move(candidate.source_path, candidate.destination_path)
            moved = adapter.inspect_file(candidate.destination_path)
            if not _identity_matches(moved, expected):
                if not adapter.exists(candidate.source_path):
                    adapter.move(candidate.destination_path, candidate.source_path)
                raise FolderMoveExecutionError(
                    "移動後のファイル識別情報が一致しないため元へ戻しました"
                )
            try:
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
                    current_run.stats_json = {
                        "moved_count": next_moved_count,
                        "total_count": plan.move_count,
                        "unaffected_count": plan.unaffected_count,
                    }
                    session.commit()
                    moved_count = next_moved_count
            except Exception:
                # DB反映に失敗した現在の1件だけは、上書き禁止で元へ戻す。
                if not adapter.exists(candidate.source_path):
                    adapter.move(candidate.destination_path, candidate.source_path)
                raise

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
            final_run.stats_json = {
                "moved_count": moved_count,
                "total_count": plan.move_count,
                "unaffected_count": plan.unaffected_count,
            }
            session.commit()
    except Exception as exc:  # noqa: BLE001 - 外部I/O境界でRunを必ず終端する
        with session_factory() as session:
            failed_run = session.get(Run, run_id)
            if failed_run is not None:
                failed_run.status = RunStatus.FAILED
                failed_run.finished_at = datetime.now(UTC)
                failed_run.stats_json = {
                    "moved_count": moved_count,
                    "total_count": plan.move_count,
                    "unaffected_count": plan.unaffected_count,
                    "reason_code": "folder_move_failed",
                }
                session.commit()
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
            "フォルダ移動の途中で失敗しました。成功分はDBへ反映済みです。再実行できます"
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
