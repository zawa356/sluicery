"""task ワーカー制御列を追加

Revision ID: c7d94b31a6e2
Revises: 5b8c9d1e2f30
Create Date: 2026-08-12 00:00:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sluicery.db.models import UTCDateTime

revision: str = "c7d94b31a6e2"
down_revision: str | Sequence[str] | None = "5b8c9d1e2f30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LEGACY_TASK_TYPES = (
    "discover",
    "download",
    "verify",
    "postprocess",
    "publish",
    "index",
    "integrity_check",
    "retention",
    "update_ytdlp",
)
TEST_TASK_TYPES = ("noop", "sleep", "fail", "fail_unavailable", "fail_blocked", "spawn")
LEGACY_TASK_STATUSES = ("pending", "queued", "running", "succeeded", "failed", "cancelled")
PHASE6_TASK_STATUSES = (
    "pending",
    "queued",
    "running",
    "succeeded",
    "failed",
    "unavailable",
    "blocked",
    "cancelled",
)


def _task_type(values: Sequence[str]) -> sa.Enum:
    return sa.Enum(
        *values, name="task_type", native_enum=False, create_constraint=True
    )


def _task_status(values: Sequence[str]) -> sa.Enum:
    return sa.Enum(
        *values, name="task_status", native_enum=False, create_constraint=True
    )


def upgrade() -> None:
    """Phase 6 のclaim、heartbeat、cancel、backoff用の列と状態を追加する。"""
    # D-008 の偽陽性差分は含めず、task の実変更だけを batch recreate する。
    with op.batch_alter_table("task", schema=None, recreate="always") as batch_op:
        batch_op.drop_index("ix_task_status_worker_class_priority")
        batch_op.alter_column(
            "type",
            existing_type=_task_type(LEGACY_TASK_TYPES),
            type_=_task_type((*LEGACY_TASK_TYPES, *TEST_TASK_TYPES)),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "status",
            existing_type=_task_status(LEGACY_TASK_STATUSES),
            type_=_task_status(PHASE6_TASK_STATUSES),
            existing_nullable=False,
        )
        batch_op.add_column(sa.Column("available_at", UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("blocked_until", UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("blocked_reason", sa.String(length=4000), nullable=True))
        batch_op.add_column(sa.Column("heartbeat_at", UTCDateTime(), nullable=True))
        batch_op.add_column(sa.Column("worker_id", sa.String(length=255), nullable=True))
        batch_op.add_column(
            sa.Column(
                "cancel_requested",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.create_index(
            "ix_task_claim",
            [
                "status",
                "worker_class",
                "priority",
                "scheduled_at",
                "available_at",
                "blocked_until",
            ],
            unique=False,
        )


def downgrade() -> None:
    """検証専用行を除去し、blocked を pending へ戻して旧スキーマへ復元する。"""
    task = sa.table(
        "task",
        sa.column("type", sa.String()),
        sa.column("status", sa.String()),
    )
    op.execute(task.delete().where(task.c.type.in_(TEST_TASK_TYPES)))
    op.execute(task.update().where(task.c.status == "blocked").values(status="pending"))
    op.execute(task.update().where(task.c.status == "unavailable").values(status="failed"))

    with op.batch_alter_table("task", schema=None, recreate="always") as batch_op:
        batch_op.drop_index("ix_task_claim")
        batch_op.alter_column(
            "type",
            existing_type=_task_type((*LEGACY_TASK_TYPES, *TEST_TASK_TYPES)),
            type_=_task_type(LEGACY_TASK_TYPES),
            existing_nullable=False,
        )
        batch_op.alter_column(
            "status",
            existing_type=_task_status(PHASE6_TASK_STATUSES),
            type_=_task_status(LEGACY_TASK_STATUSES),
            existing_nullable=False,
        )
        batch_op.drop_column("cancel_requested")
        batch_op.drop_column("worker_id")
        batch_op.drop_column("heartbeat_at")
        batch_op.drop_column("blocked_reason")
        batch_op.drop_column("blocked_until")
        batch_op.drop_column("available_at")
        batch_op.create_index(
            "ix_task_status_worker_class_priority",
            ["status", "worker_class", "priority"],
            unique=False,
        )
