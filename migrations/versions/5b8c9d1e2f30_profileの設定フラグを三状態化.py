"""profile の設定フラグを三状態化

Revision ID: 5b8c9d1e2f30
Revises: 01f4e2ff8faf
Create Date: 2026-08-09 20:50:00

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "5b8c9d1e2f30"
down_revision: str | Sequence[str] | None = "01f4e2ff8faf"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRISTATE_FLAGS = (
    "audio_extract",
    "embed_metadata",
    "embed_thumbnail",
    "embed_chapters",
    "subtitle_auto",
    "subtitle_embed",
)


def upgrade() -> None:
    """Profile の未設定（NULL）と明示的な false を区別できるようにする。

    既存行に保存済みの true / false は明示値としてそのまま残す。Phase 4 より前に
    Python 側の既定値で作られた行を NULL へ書き換えると、利用者が明示した値との
    区別ができず挙動を変えるためである。
    """
    # D-008 の CHECK 制約偽陽性は含めず、実際に必要な nullable 変更だけを記述する。
    with op.batch_alter_table("profile", schema=None) as batch_op:
        for column_name in TRISTATE_FLAGS:
            batch_op.alter_column(
                column_name,
                existing_type=sa.Boolean(),
                nullable=True,
            )


def downgrade() -> None:
    """NULL を Phase 3 までの既定値へ戻してから NOT NULL 制約を復元する。"""
    profile = sa.table(
        "profile",
        *(sa.column(column_name, sa.Boolean()) for column_name in TRISTATE_FLAGS),
    )
    legacy_defaults = {
        "audio_extract": False,
        "embed_metadata": True,
        "embed_thumbnail": True,
        "embed_chapters": False,
        "subtitle_auto": False,
        "subtitle_embed": False,
    }
    for column_name, default in legacy_defaults.items():
        column = getattr(profile.c, column_name)
        op.execute(profile.update().where(column.is_(None)).values({column_name: default}))

    with op.batch_alter_table("profile", schema=None) as batch_op:
        for column_name in TRISTATE_FLAGS:
            batch_op.alter_column(
                column_name,
                existing_type=sa.Boolean(),
                nullable=False,
            )
