"""Playlist保持ポリシーのdry-run、確認、削除実行に対する安全境界。"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from sluicery.core.sync import (
    SyncAlreadyRunningError,
    lock_and_validate_playlist_operation_start,
)
from sluicery.db.models import (
    Artifact,
    Item,
    Playlist,
    Run,
    RunStatus,
    RunTrigger,
    Storage,
    Target,
    TargetStatus,
)
from sluicery.hooks import EventLogHook, Hook, emit_safely
from sluicery.storage.base import (
    RemoteFile,
    StorageAdapter,
    StorageOperationError,
)

logger = logging.getLogger(__name__)


class RetentionPolicyError(ValueError):
    """保持ポリシーが不正。"""


class RetentionSafetyError(RuntimeError):
    """件数・割合guardまたは状態変化により実行できない。"""


class RetentionConfirmationError(RuntimeError):
    """dry-run確認tokenが不正、期限切れ、または現在状態と不一致。"""


class RetentionExecutionError(RuntimeError):
    """Storage上の削除または削除後DB反映に失敗した。"""


@dataclass(frozen=True)
class RetentionPolicy:
    enabled: bool = False
    keep_latest: int | None = None
    max_age_days: int | None = None

    @classmethod
    def from_json(cls, value: object) -> RetentionPolicy:
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise RetentionPolicyError("retention policyはobjectで指定してください")
        allowed = {"enabled", "keep_latest", "max_age_days"}
        if set(value) - allowed:
            raise RetentionPolicyError("retention policyに未知の項目があります")
        enabled = value.get("enabled", False)
        if not isinstance(enabled, bool):
            raise RetentionPolicyError("enabledは真偽値で指定してください")
        keep_latest = _optional_positive_int(value.get("keep_latest"), "keep_latest")
        max_age_days = _optional_positive_int(value.get("max_age_days"), "max_age_days")
        if enabled and keep_latest is None and max_age_days is None:
            raise RetentionPolicyError("有効化時は件数または期間を1つ以上指定してください")
        return cls(enabled, keep_latest, max_age_days)

    def to_json(self) -> dict[str, bool | int | None]:
        return asdict(self)


def _optional_positive_int(value: object, field: str) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        raise RetentionPolicyError(f"{field}は1以上の整数にしてください")
    if not isinstance(value, str | int):
        raise RetentionPolicyError(f"{field}は1以上の整数にしてください")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise RetentionPolicyError(f"{field}は1以上の整数にしてください") from exc
    if result <= 0 or str(result) != str(value).strip():
        raise RetentionPolicyError(f"{field}は1以上の整数にしてください")
    return result


@dataclass(frozen=True)
class RetentionCandidate:
    artifact_id: int
    target_id: int
    storage_id: int
    relative_path: str
    filesize: int | None
    item_date: str | None
    artifact_updated_at: str
    target_updated_at: str
    storage_updated_at: str
    storage_config_fingerprint: str
    file_identity: RemoteFile | None = None


@dataclass(frozen=True)
class RetentionPlan:
    playlist_id: int
    policy: RetentionPolicy
    candidates: tuple[RetentionCandidate, ...]
    total_artifacts: int
    blocked_reasons: tuple[str, ...]

    @property
    def delete_count(self) -> int:
        return len(self.candidates)

    @property
    def deletable(self) -> bool:
        return self.policy.enabled and not self.blocked_reasons


@dataclass(frozen=True)
class RetentionConfirmation:
    playlist_id: int
    purpose: str
    policy: RetentionPolicy
    fingerprint: str
    issued_at: datetime


@dataclass(frozen=True)
class RetentionExecutionResult:
    run_id: int
    deleted_count: int
    deleted_bytes: int
    log_path: Path


def _parse_upload_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        return None


def build_retention_plan(
    session: Session,
    playlist_id: int,
    policy: RetentionPolicy,
    *,
    max_delete_per_run: int,
    today: date | None = None,
    adapter_factory: Callable[[Storage], StorageAdapter] | None = None,
) -> RetentionPlan:
    """候補snapshotを作る。execute用dry-runでは実ファイル識別情報も固定する。"""
    if session.get(Playlist, playlist_id) is None:
        raise LookupError("Playlistが見つかりません")
    if max_delete_per_run <= 0:
        raise ValueError("max_delete_per_runは1以上にしてください")
    rows = list(
        session.execute(
            select(Artifact, Target, Item)
            .join(Target, Artifact.target_id == Target.id)
            .join(Item, Target.item_id == Item.id)
            .where(Item.playlist_id == playlist_id)
            .order_by(Artifact.id)
        )
    )
    total = len(rows)
    if not policy.enabled:
        return RetentionPlan(playlist_id, policy, (), total, ())

    items = {item.id: item for _artifact, _target, item in rows}
    selected_item_ids: set[int] = set()
    if policy.keep_latest is not None:
        ordered = sorted(
            items.values(),
            key=lambda item: (
                item.upload_date or "99999999",
                -(item.playlist_index or 0),
                item.id,
            ),
            reverse=True,
        )
        kept = {item.id for item in ordered[: policy.keep_latest]}
        selected_item_ids.update(item.id for item in ordered if item.id not in kept)

    if policy.max_age_days is not None:
        cutoff = (today or datetime.now(UTC).date()) - timedelta(days=policy.max_age_days)
        selected_item_ids.update(
            item.id
            for item in items.values()
            if (uploaded := _parse_upload_date(item.upload_date)) is not None
            and uploaded < cutoff
        )

    candidate_rows = [
        (artifact, target, item)
        for artifact, target, item in rows
        if item.id in selected_item_ids and target.status == TargetStatus.DOWNLOADED
    ]
    blocked: list[str] = []
    adapters: dict[int, StorageAdapter] = {}
    candidates_list: list[RetentionCandidate] = []
    for artifact, target, item in candidate_rows:
        storage = session.get(Storage, artifact.storage_id)
        if storage is None:
            blocked.append(f"Artifact {artifact.id} のStorageが見つかりません")
            continue
        identity: RemoteFile | None = None
        if adapter_factory is not None:
            try:
                adapter = adapters.get(storage.id)
                if adapter is None:
                    adapter = adapter_factory(storage)
                    adapters[storage.id] = adapter
                identity = adapter.inspect_file(artifact.relative_path)
                if not identity.hashes and identity.file_id is None:
                    raise StorageOperationError(
                        "削除対象の強い識別情報を取得できません",
                        reason_code="identity_unavailable",
                    )
                if artifact.filesize is not None and identity.size != artifact.filesize:
                    raise StorageOperationError(
                        "DB記録と実ファイルのsizeが一致しません",
                        reason_code="size_mismatch",
                    )
            except (OSError, StorageOperationError, ValueError) as exc:
                blocked.append(
                    f"Artifact {artifact.id} の実体を安全に確認できません: {exc}"
                )
        candidates_list.append(
            RetentionCandidate(
            artifact_id=artifact.id,
            target_id=target.id,
            storage_id=artifact.storage_id,
            relative_path=artifact.relative_path,
            filesize=artifact.filesize,
            item_date=item.upload_date,
            artifact_updated_at=artifact.updated_at.isoformat(),
            target_updated_at=target.updated_at.isoformat(),
            storage_updated_at=storage.updated_at.isoformat(),
            storage_config_fingerprint=_storage_config_fingerprint(storage),
            file_identity=identity,
        )
        )
    candidates = tuple(candidates_list)
    if len(candidates) > max_delete_per_run:
        blocked.append(
            f"削除候補{len(candidates)}件が1回の上限{max_delete_per_run}件を超えています"
        )
    if total and len(candidates) * 2 > total:
        blocked.append("PlaylistのArtifactの過半数が削除候補になるため拒否しました")
    return RetentionPlan(playlist_id, policy, candidates, total, tuple(blocked))


def _storage_config_fingerprint(storage: Storage) -> str:
    portable = {
        "kind": storage.kind.value,
        "config": storage.config_json,
    }
    encoded = json.dumps(portable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _plan_fingerprint(plan: RetentionPlan) -> str:
    payload = {
        "playlist_id": plan.playlist_id,
        "policy": plan.policy.to_json(),
        "candidates": [asdict(candidate) for candidate in plan.candidates],
        "total_artifacts": plan.total_artifacts,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


class RetentionConfirmationSigner:
    """dry-run snapshotを改ざん不可・期限付きの確認tokenへする。"""

    def __init__(
        self,
        secret_key: str,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._serializer = URLSafeSerializer(secret_key, salt="sluicery-retention-v1")
        self._clock = clock

    def issue(self, plan: RetentionPlan, *, purpose: str) -> str:
        if purpose not in {"enable", "execute"}:
            raise ValueError("retention confirmation purposeが不正です")
        return self._serializer.dumps(
            {
                "playlist_id": plan.playlist_id,
                "purpose": purpose,
                "policy": plan.policy.to_json(),
                "fingerprint": _plan_fingerprint(plan),
                "issued_at": self._clock().timestamp(),
            }
        )

    def load(self, token: str, *, purpose: str, ttl_sec: int) -> RetentionConfirmation:
        if ttl_sec <= 0:
            raise ValueError("dry-run TTLは1以上にしてください")
        try:
            payload = self._serializer.loads(token)
            if not isinstance(payload, dict):
                raise RetentionConfirmationError("dry-run確認tokenが不正です")
            issued_at = datetime.fromtimestamp(float(payload["issued_at"]), UTC)
            playlist_id = int(payload["playlist_id"])
            token_purpose = str(payload["purpose"])
            fingerprint = str(payload["fingerprint"])
            policy = RetentionPolicy.from_json(payload["policy"])
        except (BadSignature, KeyError, TypeError, ValueError) as exc:
            raise RetentionConfirmationError("dry-run確認tokenが不正です") from exc
        age = (self._clock() - issued_at).total_seconds()
        if age < 0 or age > ttl_sec:
            raise RetentionConfirmationError(
                "dry-run結果の有効期限が切れました。再確認してください"
            )
        if token_purpose != purpose:
            raise RetentionConfirmationError("確認目的が一致しません。再確認してください")
        return RetentionConfirmation(
            playlist_id, token_purpose, policy, fingerprint, issued_at
        )

    @staticmethod
    def verify_plan(confirmation: RetentionConfirmation, plan: RetentionPlan) -> None:
        if (
            confirmation.playlist_id != plan.playlist_id
            or confirmation.policy != plan.policy
            or confirmation.fingerprint != _plan_fingerprint(plan)
        ):
            raise RetentionConfirmationError(
                "dry-run後に対象が変化しました。再確認してください"
            )


def save_retention_policy(
    session: Session,
    playlist_id: int,
    policy: RetentionPolicy,
) -> None:
    playlist = session.get(Playlist, playlist_id)
    if playlist is None:
        raise LookupError("Playlistが見つかりません")
    playlist.retention_policy_json = policy.to_json() if policy.enabled else None
    session.commit()


def _candidate_matches(artifact: Artifact, candidate: RetentionCandidate) -> bool:
    return (
        artifact.id == candidate.artifact_id
        and artifact.target_id == candidate.target_id
        and artifact.storage_id == candidate.storage_id
        and artifact.relative_path == candidate.relative_path
        and artifact.filesize == candidate.filesize
        and artifact.updated_at.isoformat() == candidate.artifact_updated_at
    )


def _related_rows_match(
    target: Target | None,
    storage: Storage | None,
    candidate: RetentionCandidate,
) -> bool:
    return (
        target is not None
        and target.status == TargetStatus.DOWNLOADED
        and target.updated_at.isoformat() == candidate.target_updated_at
        and storage is not None
        and storage.updated_at.isoformat() == candidate.storage_updated_at
        and _storage_config_fingerprint(storage) == candidate.storage_config_fingerprint
    )


def _write_audit(audit, payload: dict[str, object]) -> None:
    audit.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    audit.flush()
    os.fsync(audit.fileno())


def _quarantine_path(relative_path: str) -> str:
    path = Path(relative_path)
    return (path.parent / f".sluicery-retention-{uuid4().hex}").as_posix()


def assert_no_unfinished_retention_intents(data_dir: Path) -> None:
    """二相auditの未完了intentを読み取り検出し、手動確認まで次回実行を止める。"""
    log_dir = data_dir / "logs"
    if not log_dir.is_dir():
        return
    for log_path in sorted(log_dir.glob("retention-*.log")):
        try:
            if log_path.stat().st_size > 1024 * 1024:
                raise RetentionSafetyError(
                    f"retention監査ログが上限を超えています。手動確認してください: {log_path.name}"
                )
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except RetentionSafetyError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Unreadable retention audit log", extra={"path": str(log_path)})
            raise RetentionSafetyError(
                f"retention監査ログを確認できません。手動確認してください: {log_path.name}"
            ) from exc
        completed = {
            (row.get("artifact_id"), row.get("storage_id"), row.get("path"))
            for row in records
            if isinstance(row, dict) and row.get("event") == "deleted"
        }
        for intent in records:
            if not isinstance(intent, dict) or intent.get("event") != "delete_intent":
                continue
            key = (
                intent.get("artifact_id"),
                intent.get("storage_id"),
                intent.get("path"),
            )
            if key in completed:
                continue
            quarantine = intent.get("quarantine_path")
            detail = quarantine if isinstance(quarantine, str) else log_path.name
            raise RetentionSafetyError(
                "未完了のretention削除意図があります。"
                f"自動移動せず隔離物を保持しました。監査ログを手動確認してください: {detail}"
            )


def execute_retention(
    session_factory: sessionmaker[Session],
    plan: RetentionPlan,
    *,
    data_dir: Path,
    adapter_factory: Callable[[Storage], StorageAdapter],
    confirmation_token: str,
    confirmation_signer: RetentionConfirmationSigner,
    dryrun_ttl_sec: int,
    hook: Hook | None = None,
) -> RetentionExecutionResult:
    """確認済みsnapshotを1件ずつ削除し、成功分を即時DBと監査logへ反映する。"""
    confirmation = confirmation_signer.load(
        confirmation_token, purpose="execute", ttl_sec=dryrun_ttl_sec
    )
    confirmation_signer.verify_plan(confirmation, plan)
    if not plan.deletable:
        reason = " / ".join(plan.blocked_reasons) or "retentionが無効です"
        raise RetentionSafetyError(reason)
    if not plan.candidates:
        raise RetentionSafetyError("削除候補がありません")
    if any(candidate.file_identity is None for candidate in plan.candidates):
        raise RetentionSafetyError(
            "削除対象の実ファイル識別情報がありません。execute用dry-runを再実行してください"
        )

    log_dir = data_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / f"retention-{uuid4().hex}.log"
    with session_factory() as session:
        try:
            lock_and_validate_playlist_operation_start(session, plan.playlist_id)
        except SyncAlreadyRunningError as exc:
            raise RetentionSafetyError(
                "Playlistの同期が実行中のためretentionを開始できません"
            ) from exc
        for candidate in plan.candidates:
            artifact = session.get(Artifact, candidate.artifact_id)
            target = session.get(Target, candidate.target_id)
            storage = session.get(Storage, candidate.storage_id)
            if (
                artifact is None
                or not _candidate_matches(artifact, candidate)
                or not _related_rows_match(target, storage, candidate)
            ):
                session.rollback()
                raise RetentionSafetyError(
                    "dry-run後に削除対象が変化しました。再確認してください"
                )
        storages = {
            storage_id: session.get(Storage, storage_id)
            for storage_id in {candidate.storage_id for candidate in plan.candidates}
        }
        if any(storage is None for storage in storages.values()):
            session.rollback()
            raise RetentionSafetyError("削除対象のStorageが見つかりません")
        run = Run(
            trigger=RunTrigger.MANUAL,
            kind="retention",
            playlist_id=plan.playlist_id,
            status=RunStatus.RUNNING,
            log_path=str(log_path),
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
            "kind": "retention",
            "trigger": RunTrigger.MANUAL.value,
        },
    )

    adapters: dict[int, StorageAdapter] = {}
    deleted_count = 0
    deleted_bytes = 0
    try:
        with log_path.open("x", encoding="utf-8", buffering=1) as audit:
            for candidate in plan.candidates:
                storage = storages[candidate.storage_id]
                assert storage is not None
                with session_factory() as session:
                    current_artifact = session.get(Artifact, candidate.artifact_id)
                    current_target = session.get(Target, candidate.target_id)
                    current_storage = session.get(Storage, candidate.storage_id)
                    if (
                        current_artifact is None
                        or not _candidate_matches(current_artifact, candidate)
                        or not _related_rows_match(
                            current_target, current_storage, candidate
                        )
                    ):
                        raise RetentionExecutionError(
                            "削除直前に対象が変化しました。dry-runを再実行してください"
                        )
                adapter = adapters.get(candidate.storage_id)
                if adapter is None:
                    adapter = adapter_factory(storage)
                    adapters[candidate.storage_id] = adapter
                quarantine_path = _quarantine_path(candidate.relative_path)
                intent_at = datetime.now(UTC).isoformat()
                assert candidate.file_identity is not None
                _write_audit(
                    audit,
                    {
                        "event": "delete_intent",
                        "artifact_id": candidate.artifact_id,
                        "storage_id": candidate.storage_id,
                        "path": candidate.relative_path,
                        "quarantine_path": quarantine_path,
                        "size": candidate.filesize,
                        "file_identity": asdict(candidate.file_identity),
                        "storage_config_fingerprint": candidate.storage_config_fingerprint,
                        "recorded_at": intent_at,
                    },
                )
                adapter.delete_file(
                    candidate.relative_path,
                    expected=candidate.file_identity,
                    quarantine_rel=quarantine_path,
                )
                deleted_at = datetime.now(UTC).isoformat()
                _write_audit(
                    audit,
                    {
                        "event": "deleted",
                        "artifact_id": candidate.artifact_id,
                        "storage_id": candidate.storage_id,
                        "path": candidate.relative_path,
                        "size": candidate.filesize,
                        "deleted_at": deleted_at,
                    },
                )
                with session_factory() as session:
                    artifact = session.get(Artifact, candidate.artifact_id)
                    target = session.get(Target, candidate.target_id)
                    current_storage = session.get(Storage, candidate.storage_id)
                    if (
                        artifact is None
                        or not _candidate_matches(artifact, candidate)
                        or not _related_rows_match(target, current_storage, candidate)
                    ):
                        raise RetentionExecutionError(
                            "ファイル削除後のDB反映前に対象が変化しました。監査ログを確認してください"
                        )
                    assert target is not None
                    target.status = TargetStatus.IGNORED
                    target.last_error = None
                    session.delete(artifact)
                    session.commit()
                deleted_count += 1
                deleted_bytes += candidate.filesize or 0
    except Exception as exc:  # noqa: BLE001 - 削除境界は必ずRunを終端して報告する
        with session_factory() as session:
            failed_run = session.get(Run, run_id)
            if failed_run is not None:
                failed_run.status = RunStatus.FAILED
                failed_run.finished_at = datetime.now(UTC)
                failed_run.stats_json = {
                    "deleted_count": deleted_count,
                    "deleted_bytes": deleted_bytes,
                    "reason_code": "retention_failed",
                }
                session.commit()
        emit_safely(
            event_hook,
            "run_failed",
            {
                "run_id": run_id,
                "playlist_id": plan.playlist_id,
                "kind": "retention",
                "reason_code": "retention_failed",
            },
        )
        raise RetentionExecutionError(
            "retentionの途中で失敗しました。成功分は監査ログを確認してください"
        ) from exc

    with session_factory() as session:
        finished_run = session.get(Run, run_id)
        if finished_run is not None:
            finished_run.status = RunStatus.SUCCEEDED
            finished_run.finished_at = datetime.now(UTC)
            finished_run.stats_json = {
                "deleted_count": deleted_count,
                "deleted_bytes": deleted_bytes,
            }
            session.commit()
    emit_safely(
        event_hook,
        "run_finished",
        {
            "run_id": run_id,
            "playlist_id": plan.playlist_id,
            "kind": "retention",
            "status": RunStatus.SUCCEEDED.value,
        },
    )
    return RetentionExecutionResult(run_id, deleted_count, deleted_bytes, log_path)


__all__ = [
    "RetentionCandidate",
    "RetentionConfirmation",
    "RetentionConfirmationError",
    "RetentionConfirmationSigner",
    "RetentionExecutionError",
    "RetentionExecutionResult",
    "RetentionPlan",
    "RetentionPolicy",
    "RetentionPolicyError",
    "RetentionSafetyError",
    "build_retention_plan",
    "execute_retention",
    "assert_no_unfinished_retention_intents",
    "save_retention_policy",
]
