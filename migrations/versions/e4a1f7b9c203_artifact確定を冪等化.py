"""artifact確定を冪等化

Revision ID: e4a1f7b9c203
Revises: c7d94b31a6e2
Create Date: 2026-08-13 00:00:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e4a1f7b9c203"
down_revision: str | Sequence[str] | None = "c7d94b31a6e2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("artifact", schema=None) as batch_op:
        batch_op.create_unique_constraint(
            "uq_artifact_target_id_role_storage_id_relative_path",
            ["target_id", "role", "storage_id", "relative_path"],
        )


def downgrade() -> None:
    with op.batch_alter_table("artifact", schema=None) as batch_op:
        batch_op.drop_constraint(
            "uq_artifact_target_id_role_storage_id_relative_path", type_="unique"
        )
