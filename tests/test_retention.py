from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select

from sluicery.core.retention import (
    RetentionConfirmationError,
    RetentionConfirmationSigner,
    RetentionExecutionError,
    RetentionPolicy,
    RetentionSafetyError,
    assert_no_unfinished_retention_intents,
    build_retention_plan,
    execute_retention,
)
from sluicery.db.models import (
    Artifact,
    ArtifactRole,
    Item,
    LayoutStrategy,
    Playlist,
    PlaylistKindHint,
    PlaylistProfile,
    Profile,
    ProfileKind,
    Run,
    RunStatus,
    RunTrigger,
    Storage,
    StorageKind,
    Target,
    TargetStatus,
)
from sluicery.storage.local import LocalStorageAdapter


class _Hook:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def emit(self, event_type: str, payload: dict) -> None:
        self.events.append((event_type, payload))


def _graph(db_session, *, count: int = 5):
    storage = Storage(name="media", kind=StorageKind.LOCAL, config_json={"path": "library"})
    profile = Profile(
        name="video", kind=ProfileKind.VIDEO, layout_strategy=LayoutStrategy.FLAT
    )
    playlist = Playlist(
        name="list",
        folder_name="list",
        url="https://example.com/list",
        kind_hint=PlaylistKindHint.VIDEO,
    )
    db_session.add_all([storage, profile, playlist])
    db_session.flush()
    assignment = PlaylistProfile(
        playlist_id=playlist.id,
        profile_id=profile.id,
        storage_id=storage.id,
    )
    db_session.add(assignment)
    db_session.flush()
    artifacts = []
    targets = []
    for index in range(count):
        item = Item(
            playlist_id=playlist.id,
            source_id=f"source-{index}",
            source_url=f"https://example.com/source-{index}",
            upload_date=f"20260{index + 1}01",
            playlist_index=count - index,
        )
        db_session.add(item)
        db_session.flush()
        target = Target(
            item_id=item.id,
            playlist_profile_id=assignment.id,
            status=TargetStatus.DOWNLOADED,
        )
        db_session.add(target)
        db_session.flush()
        artifact = Artifact(
            target_id=target.id,
            role=ArtifactRole.SOURCE,
            storage_id=storage.id,
            relative_path=f"list/source-{index}.mkv",
            filesize=100 + index,
        )
        db_session.add(artifact)
        artifacts.append(artifact)
        targets.append(target)
    db_session.commit()
    return playlist, storage, targets, artifacts


def test_retention_is_disabled_by_default_and_dry_run_has_no_side_effect(db_session) -> None:
    playlist, _storage, targets, artifacts = _graph(db_session)

    plan = build_retention_plan(
        db_session,
        playlist.id,
        RetentionPolicy.from_json(playlist.retention_policy_json),
        max_delete_per_run=20,
    )

    assert plan.policy.enabled is False
    assert plan.candidates == ()
    assert all(target.status == TargetStatus.DOWNLOADED for target in targets)
    assert len(artifacts) == 5


def test_keep_latest_and_age_policy_build_union_without_deleting(db_session) -> None:
    playlist, _storage, _targets, artifacts = _graph(db_session)
    policy = RetentionPolicy(enabled=True, keep_latest=3, max_age_days=120)

    plan = build_retention_plan(
        db_session,
        playlist.id,
        policy,
        max_delete_per_run=20,
        today=date(2026, 8, 16),
    )

    assert [row.artifact_id for row in plan.candidates] == [a.id for a in artifacts[:4]]
    assert all(db_session.get(Artifact, artifact.id) is not None for artifact in artifacts)


def test_count_and_majority_guards_refuse_plan(db_session) -> None:
    playlist, _storage, _targets, _artifacts = _graph(db_session, count=5)

    plan = build_retention_plan(
        db_session,
        playlist.id,
        RetentionPolicy(enabled=True, keep_latest=1),
        max_delete_per_run=2,
    )

    assert len(plan.blocked_reasons) == 2
    assert "上限2件" in plan.blocked_reasons[0]
    assert "過半数" in plan.blocked_reasons[1]
    assert not plan.deletable


def test_confirmation_expires_and_rejects_changed_snapshot(
    db_session, secret_key
) -> None:
    playlist, _storage, _targets, artifacts = _graph(db_session)
    now = datetime(2026, 8, 16, tzinfo=UTC)
    current = [now]
    signer = RetentionConfirmationSigner(secret_key, clock=lambda: current[0])
    policy = RetentionPolicy(enabled=True, keep_latest=4)
    plan = build_retention_plan(
        db_session, playlist.id, policy, max_delete_per_run=20
    )
    token = signer.issue(plan, purpose="execute")

    confirmation = signer.load(token, purpose="execute", ttl_sec=300)
    artifacts[0].relative_path = "changed/path.mkv"
    db_session.commit()
    changed = build_retention_plan(
        db_session, playlist.id, policy, max_delete_per_run=20
    )
    with pytest.raises(RetentionConfirmationError, match="対象が変化"):
        signer.verify_plan(confirmation, changed)

    current[0] = now + timedelta(seconds=301)
    with pytest.raises(RetentionConfirmationError, match="有効期限"):
        signer.load(token, purpose="execute", ttl_sec=300)


def test_execute_requires_safe_plan_and_logs_each_synthetic_deletion(
    db_session, session_factory, tmp_path, secret_key
) -> None:
    playlist, storage, targets, artifacts = _graph(db_session, count=3)
    media_root = tmp_path / "media"
    target_file = media_root / "library" / artifacts[0].relative_path
    target_file.parent.mkdir(parents=True)
    target_file.write_bytes(b"a" * 100)
    adapter_factory = lambda row: LocalStorageAdapter(  # noqa: E731
        str(row.config_json["path"]), media_root=media_root
    )
    plan = build_retention_plan(
        db_session,
        playlist.id,
        RetentionPolicy(enabled=True, keep_latest=2),
        max_delete_per_run=20,
        adapter_factory=adapter_factory,
    )
    signer = RetentionConfirmationSigner(secret_key)
    token = signer.issue(plan, purpose="execute")
    hook = _Hook()

    result = execute_retention(
        session_factory,
        plan,
        data_dir=tmp_path / "data",
        adapter_factory=adapter_factory,
        confirmation_token=token,
        confirmation_signer=signer,
        dryrun_ttl_sec=300,
        hook=hook,
    )

    assert result.deleted_count == 1
    assert not target_file.exists()
    audit = result.log_path.read_text(encoding="utf-8")
    assert artifacts[0].relative_path in audit
    assert '"size": 100' in audit and '"deleted_at":' in audit
    assert '"event": "delete_intent"' in audit
    assert '"event": "deleted"' in audit
    assert '"quarantine_path": "list/.sluicery-retention-' in audit
    with session_factory() as session:
        assert session.get(Artifact, artifacts[0].id) is None
        assert session.get(Target, targets[0].id).status == TargetStatus.IGNORED
        run = session.get(Run, result.run_id)
        assert run is not None and run.status == RunStatus.SUCCEEDED
    assert [event_type for event_type, _payload in hook.events] == [
        "run_started",
        "run_finished",
    ]


def test_execute_refuses_plan_with_no_candidates(
    db_session, session_factory, tmp_path, secret_key
) -> None:
    playlist, _storage, _targets, _artifacts = _graph(db_session)
    plan = build_retention_plan(
        db_session,
        playlist.id,
        RetentionPolicy(enabled=True, keep_latest=10),
        max_delete_per_run=20,
    )
    signer = RetentionConfirmationSigner(secret_key)
    token = signer.issue(plan, purpose="execute")

    with pytest.raises(RetentionSafetyError, match="削除候補"):
        execute_retention(
            session_factory,
            plan,
            data_dir=tmp_path,
            adapter_factory=lambda _row: pytest.fail("adapter must not be created"),
            confirmation_token=token,
            confirmation_signer=signer,
            dryrun_ttl_sec=300,
        )


def test_execute_refuses_while_playlist_operation_is_active(
    db_session, session_factory, tmp_path, secret_key
) -> None:
    playlist, _storage, _targets, artifacts = _graph(db_session, count=3)
    media_root = tmp_path / "media"
    target_file = media_root / "library" / artifacts[0].relative_path
    target_file.parent.mkdir(parents=True)
    target_file.write_bytes(b"a" * 100)
    adapter_factory = lambda row: LocalStorageAdapter(  # noqa: E731
        str(row.config_json["path"]), media_root=media_root
    )
    plan = build_retention_plan(
        db_session,
        playlist.id,
        RetentionPolicy(enabled=True, keep_latest=2),
        max_delete_per_run=20,
        adapter_factory=adapter_factory,
    )
    active = Run(
        trigger=RunTrigger.MANUAL,
        kind="discover",
        playlist_id=playlist.id,
        status=RunStatus.RUNNING,
    )
    db_session.add(active)
    db_session.commit()
    signer = RetentionConfirmationSigner(secret_key)
    token = signer.issue(plan, purpose="execute")

    with pytest.raises(RetentionSafetyError, match="同期が実行中"):
        execute_retention(
            session_factory,
            plan,
            data_dir=tmp_path,
            adapter_factory=adapter_factory,
            confirmation_token=token,
            confirmation_signer=signer,
            dryrun_ttl_sec=300,
        )

    with session_factory() as session:
        runs = list(session.scalars(select(Run).where(Run.playlist_id == playlist.id)))
        assert runs == [session.get(Run, active.id)]


def test_execute_refuses_replaced_file_and_keeps_replacement(
    db_session, session_factory, tmp_path, secret_key
) -> None:
    playlist, _storage, _targets, artifacts = _graph(db_session, count=3)
    media_root = tmp_path / "media"
    target_file = media_root / "library" / artifacts[0].relative_path
    target_file.parent.mkdir(parents=True)
    target_file.write_bytes(b"a" * 100)
    adapter_factory = lambda row: LocalStorageAdapter(  # noqa: E731
        str(row.config_json["path"]), media_root=media_root
    )
    plan = build_retention_plan(
        db_session,
        playlist.id,
        RetentionPolicy(enabled=True, keep_latest=2),
        max_delete_per_run=20,
        adapter_factory=adapter_factory,
    )
    signer = RetentionConfirmationSigner(secret_key)
    token = signer.issue(plan, purpose="execute")
    target_file.unlink()
    target_file.write_bytes(b"b" * 100)

    with pytest.raises(RetentionExecutionError, match="途中で失敗"):
        execute_retention(
            session_factory,
            plan,
            data_dir=tmp_path / "data",
            adapter_factory=adapter_factory,
            confirmation_token=token,
            confirmation_signer=signer,
            dryrun_ttl_sec=300,
            hook=_Hook(),
        )

    assert target_file.read_bytes() == b"b" * 100


def test_next_preview_blocks_on_unfinished_quarantine(db_session, tmp_path) -> None:
    playlist, _storage, _targets, artifacts = _graph(db_session, count=3)
    media_root = tmp_path / "media"
    target_file = media_root / "library" / artifacts[0].relative_path
    target_file.parent.mkdir(parents=True)
    target_file.write_bytes(b"a" * 100)
    adapter_factory = lambda row: LocalStorageAdapter(  # noqa: E731
        str(row.config_json["path"]), media_root=media_root
    )
    plan = build_retention_plan(
        db_session,
        playlist.id,
        RetentionPolicy(enabled=True, keep_latest=2),
        max_delete_per_run=20,
        adapter_factory=adapter_factory,
    )
    candidate = plan.candidates[0]
    assert candidate.file_identity is not None
    quarantine_rel = "list/.sluicery-retention-crash"
    quarantine_file = media_root / "library" / quarantine_rel
    target_file.rename(quarantine_file)
    log_dir = tmp_path / "data" / "logs"
    log_dir.mkdir(parents=True)
    audit_path = log_dir / "retention-crash.log"
    audit_path.write_text(
        json.dumps(
            {
                "event": "delete_intent",
                "artifact_id": candidate.artifact_id,
                "storage_id": candidate.storage_id,
                "path": candidate.relative_path,
                "quarantine_path": quarantine_rel,
                "file_identity": {
                    "relative_path": candidate.file_identity.relative_path,
                    "size": candidate.file_identity.size,
                    "modified_at": candidate.file_identity.modified_at,
                    "hashes": candidate.file_identity.hashes,
                    "file_id": candidate.file_identity.file_id,
                },
                "storage_config_fingerprint": candidate.storage_config_fingerprint,
            }
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(RetentionSafetyError, match="自動移動せず隔離物を保持"):
        assert_no_unfinished_retention_intents(tmp_path / "data")

    assert not target_file.exists()
    assert quarantine_file.read_bytes() == b"a" * 100
    assert audit_path.read_text(encoding="utf-8").count("\n") == 1
