"""認証セッションとロック状態を追加

Revision ID: f6a7b8c9d0e1
Revises: e4a1f7b9c203
Create Date: 2026-08-13 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sluicery.db.models import UTCDateTime

revision: str = "f6a7b8c9d0e1"
down_revision: str | Sequence[str] | None = "e4a1f7b9c203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "failed_login_attempts",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch_op.add_column(sa.Column("locked_until", UTCDateTime(), nullable=True))

    op.create_table(
        "auth_session",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=True),
        sa.Column("expires_at", UTCDateTime(), nullable=False),
        sa.Column("flash_json", sa.JSON(), nullable=True),
        sa.Column("created_at", UTCDateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["user.id"],
            name=op.f("fk_auth_session_user_id_user"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_auth_session")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_auth_session_token_hash")),
    )
    op.create_index(
        op.f("ix_auth_session_expires_at"),
        "auth_session",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_auth_session_expires_at"), table_name="auth_session")
    op.drop_table("auth_session")
    with op.batch_alter_table("user", schema=None) as batch_op:
        batch_op.drop_column("locked_until")
        batch_op.drop_column("failed_login_attempts")
