"""レイアウト戦略の共通インターフェース（要件定義 §14.2）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class LayoutContext:
    playlist_name: str
    playlist_folder_name: str
    profile_name: str
    profile_kind: str
    subpath: str
    custom_output_template: str | None = None


@dataclass(frozen=True)
class ValidationError:
    field: str
    message: str


@dataclass(frozen=True)
class ResolvedLayout:
    subpath: str
    output_template: str
    warnings: tuple[str, ...] = ()

    @property
    def relative_output_template(self) -> str:
        return f"{self.subpath}/{self.output_template}"


class LayoutStrategy(Protocol):
    name: str

    def output_template(self, ctx: LayoutContext) -> str: ...

    def validate(self, ctx: LayoutContext) -> list[ValidationError]: ...


__all__ = [
    "LayoutContext",
    "LayoutStrategy",
    "ResolvedLayout",
    "ValidationError",
]
