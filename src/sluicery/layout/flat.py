"""既定の Playlist 単位フラットレイアウト。"""

from __future__ import annotations

from sluicery.layout.base import LayoutContext, ValidationError

# 欠損時の fallback を明示し、既定の `NA` をファイル名へ混入させない。
VIDEO_TEMPLATE = "%(upload_date>%Y-%m-%d&{} |)s%(title).120B [%(id)s].%(ext)s"
MUSIC_TEMPLATE = "%(playlist_index|0)03d %(track,title).120B [%(id)s].%(ext)s"


class FlatLayout:
    name = "flat"

    def output_template(self, ctx: LayoutContext) -> str:
        if ctx.profile_kind == "music":
            return MUSIC_TEMPLATE
        return VIDEO_TEMPLATE

    def validate(self, ctx: LayoutContext) -> list[ValidationError]:
        if ctx.profile_kind not in {"video", "music"}:
            return [
                ValidationError(
                    field="profile.kind",
                    message="flat レイアウトは video / music のみ対応します",
                )
            ]
        return []


__all__ = ["FlatLayout", "MUSIC_TEMPLATE", "VIDEO_TEMPLATE"]
