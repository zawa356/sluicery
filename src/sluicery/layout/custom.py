"""Profile の output_template を使うカスタムレイアウト。"""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

from sluicery.layout.base import LayoutContext, ValidationError

REQUIRED_SUFFIX = "[%(id)s].%(ext)s"


class CustomLayout:
    name = "custom"

    def output_template(self, ctx: LayoutContext) -> str:
        return ctx.custom_output_template or ""

    def validate(self, ctx: LayoutContext) -> list[ValidationError]:
        template = ctx.custom_output_template
        if not template:
            return [ValidationError("output_template", "custom では output_template が必須です")]

        errors: list[ValidationError] = []
        if PurePosixPath(template).is_absolute() or PureWindowsPath(template).is_absolute():
            errors.append(
                ValidationError("output_template", "絶対パスは指定できません")
            )
        normalized = template.replace("\\", "/")
        if any(part == ".." for part in normalized.split("/")):
            errors.append(
                ValidationError("output_template", "パス traversal（..）は指定できません")
            )
        if not normalized.endswith(REQUIRED_SUFFIX):
            errors.append(
                ValidationError(
                    "output_template",
                    "末尾（拡張子の直前）に `[%(id)s]` を含めてください",
                )
            )
        return errors

    def warnings(self, ctx: LayoutContext) -> tuple[str, ...]:
        template = ctx.custom_output_template or ""
        if "/" in template or "\\" in template:
            return (
                "custom output_template のパス区切りは --paths home からの"
                "相対パスとして解釈されます",
            )
        return ()


__all__ = ["CustomLayout", "REQUIRED_SUFFIX"]
