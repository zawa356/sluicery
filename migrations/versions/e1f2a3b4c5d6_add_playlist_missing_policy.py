"""Playlistにmissing方針を追加

Revision ID: e1f2a3b4c5d6
Revises: d0e1f2a3b4c5
Create Date: 2026-08-16 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e1f2a3b4c5d6"
down_revision: str | Sequence[str] | None = "d0e1f2a3b4c5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

def upgrade() -> None:
    with op.batch_alter_table("playlist", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "missing_policy",
                sa.String(length=10),
                nullable=False,
                server_default="leave",
            )
        )
        batch_op.create_check_constraint(
            "playlist_missing_policy_values",
            "missing_policy IN ('leave', 'redownload', 'ignore')",
        )


def downgrade() -> None:
    with op.batch_alter_table("playlist", schema=None) as batch_op:
        batch_op.drop_constraint("playlist_missing_policy_values", type_="check")
        batch_op.drop_column("missing_policy")
