"""Playlist Cookieを暗号化保存

Revision ID: b8c9d0e1f2a3
Revises: f6a7b8c9d0e1
Create Date: 2026-08-13 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

from sluicery.db.crypto import EncryptedJSON

revision: str = "b8c9d0e1f2a3"
down_revision: str | Sequence[str] | None = "f6a7b8c9d0e1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("playlist", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "cookie_enabled",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column("cookies_encrypted", EncryptedJSON(), nullable=True)
        )


def downgrade() -> None:
    with op.batch_alter_table("playlist", schema=None) as batch_op:
        batch_op.drop_column("cookies_encrypted")
        batch_op.drop_column("cookie_enabled")
