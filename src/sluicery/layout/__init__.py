"""レイアウト戦略の選択と subpath テンプレート解決。"""

from __future__ import annotations

from dataclasses import dataclass

from sluicery.core.naming import sanitize_component, sanitize_relative_path
from sluicery.layout.base import LayoutContext, LayoutStrategy, ResolvedLayout
from sluicery.layout.custom import CustomLayout
from sluicery.layout.flat import FlatLayout


class LayoutValidationError(ValueError):
    """レイアウトまたは subpath テンプレートが不正。"""


@dataclass(frozen=True)
class _PlaylistVariables:
    name: str
    folder_name: str


@dataclass(frozen=True)
class _ProfileVariables:
    name: str
    kind: str


def resolve_subpath(template: str, ctx: LayoutContext) -> str:
    """`{playlist.*}` / `{profile.*}` を展開し、安全な相対パスにする。

    この構文は yt-dlp の `%(...)s` 出力テンプレートとは別のアプリ独自構文。
    """
    variables = {
        "playlist": _PlaylistVariables(
            name=ctx.playlist_name,
            folder_name=sanitize_component(ctx.playlist_folder_name),
        ),
        "profile": _ProfileVariables(name=ctx.profile_name, kind=ctx.profile_kind),
    }
    try:
        expanded = template.format_map(variables)
    except (AttributeError, KeyError, ValueError) as exc:
        raise LayoutValidationError(f"subpath テンプレートを展開できません: {exc}") from exc
    try:
        return sanitize_relative_path(expanded)
    except ValueError as exc:
        raise LayoutValidationError(str(exc)) from exc


def resolve_layout(strategy_name: str, ctx: LayoutContext) -> ResolvedLayout:
    strategies: dict[str, LayoutStrategy] = {"flat": FlatLayout(), "custom": CustomLayout()}
    strategy = strategies.get(strategy_name)
    if strategy is None:
        raise LayoutValidationError(f"未対応の layout_strategy です: {strategy_name}")

    errors = strategy.validate(ctx)
    if errors:
        message = "; ".join(f"{error.field}: {error.message}" for error in errors)
        raise LayoutValidationError(message)
    subpath = resolve_subpath(ctx.subpath, ctx)
    warnings = strategy.warnings(ctx) if isinstance(strategy, CustomLayout) else ()
    return ResolvedLayout(
        subpath=subpath,
        output_template=strategy.output_template(ctx),
        warnings=warnings,
    )


__all__ = [
    "LayoutContext",
    "LayoutValidationError",
    "ResolvedLayout",
    "resolve_layout",
    "resolve_subpath",
]
