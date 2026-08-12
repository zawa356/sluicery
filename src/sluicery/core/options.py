"""yt-dlp オプションの合成・ガード（要件定義 §9、Phase 4）。

自由入力は必ず :func:`guard_freeform` でトークン化してから扱う。yt-dlp の
全オプションを再実装せず、アプリの出力プロトコルやファイル追跡に影響する
予約引数と警告対象だけを認識する。
"""

from __future__ import annotations

import shlex
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from sluicery.db.models import Playlist, PlaylistProfile, Profile, Target

CommandKind = Literal["discover", "download"]

# 値は canonical 名。長形式・短形式・`--opt=value` を同じものとして扱う。
OPTION_ALIASES: dict[str, str] = {
    "--paths": "--paths",
    "-P": "--paths",
    "--output": "--output",
    "-o": "--output",
    "--print": "--print",
    "-O": "--print",
    "--print-to-file": "--print-to-file",
    "--progress-template": "--progress-template",
    "--download-archive": "--download-archive",
    "--load-info-json": "--load-info-json",
    "--dump-json": "--dump-json",
    "-j": "--dump-json",
    "--dump-single-json": "--dump-single-json",
    "--exec": "--exec",
    "--exec-before-download": "--exec-before-download",
    "--quiet": "--quiet",
    "-q": "--quiet",
    "--no-progress": "--no-progress",
    "--simulate": "--simulate",
    "-s": "--simulate",
    "--restrict-filenames": "--restrict-filenames",
    "--no-windows-filenames": "--no-windows-filenames",
    "--no-newline": "--no-newline",
}

RESERVED_OPTIONS = frozenset(
    {
        "--paths",
        "--output",
        "--print",
        "--print-to-file",
        "--progress-template",
        "--download-archive",
        "--load-info-json",
        "--dump-json",
        "--dump-single-json",
        "--exec",
        "--exec-before-download",
    }
)

EXEC_OPTIONS = frozenset({"--exec", "--exec-before-download"})

WARNING_REASONS: dict[str, str] = {
    "--quiet": "進捗出力を抑制し、進捗・パスの取得を妨げる可能性があります",
    "--no-progress": "進捗出力を抑制します",
    "--simulate": "download ではファイルが生成されません",
    "--restrict-filenames": "非ASCII文字を置換し、日本語のファイル名方針と衝突します",
    "--no-windows-filenames": "Windows / SMB 向けのファイル名方針と衝突します",
    "--no-newline": "標準出力の行単位パースを壊す可能性があります",
}

# `-ovalue` のような短縮形を認識する対象。フラグ型の `-q` / `-s` / `-j` は
# 後続文字を値として取らないため含めない。
SHORT_VALUE_ALIASES = {"-P": "--paths", "-o": "--output", "-O": "--print"}


class OptionValidationError(ValueError):
    """自由入力の構文または予約引数ガード違反。"""


def validate_source_url(value: str) -> str:
    """yt-dlp に渡す取得元を完全な HTTP(S) URL に限定する。"""
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        _ = parsed.port
    except ValueError as exc:
        raise OptionValidationError("取得元 URL の形式が不正です") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or any(character.isspace() for character in value)
    ):
        raise OptionValidationError(
            "取得元 URL は http:// または https:// の完全な形式で指定してください"
        )
    return value


def guard_raw_exec_args(tokens: list[str]) -> tuple[str, ...]:
    """Profile を持たない debug exec から予約引数を実行させない。"""
    for occurrence in parse_managed_options(tokens):
        if occurrence.canonical in RESERVED_OPTIONS:
            raise OptionValidationError(
                f"`{occurrence.canonical}` は `ytdlp exec` では使用できません。"
                "Profile を通る probe / fetch 経路を使用してください"
            )
    return tuple(tokens)


@dataclass(frozen=True)
class ParsedOption:
    canonical: str
    token: str
    index: int
    value: str | None


@dataclass(frozen=True)
class GuardedTokens:
    tokens: tuple[str, ...]
    warnings: tuple[str, ...]
    reserved_overrides: frozenset[str]


@dataclass(frozen=True)
class ArgumentOrigin:
    arguments: tuple[str, ...]
    layer: str
    source: str
    field: str | None = None


@dataclass(frozen=True)
class TimeoutSpec:
    idle_sec: int | None
    absolute_sec: int | None
    term_grace_sec: int


@dataclass(frozen=True)
class BuiltCommand:
    args: tuple[str, ...]
    origins: tuple[ArgumentOrigin, ...]
    warnings: tuple[str, ...]
    resolved_output_path: Path | None
    timeout: TimeoutSpec


@dataclass(frozen=True)
class OptionOverrides:
    structured: Mapping[str, Any] = field(default_factory=dict)
    ytdlp_args: str | None = None


@dataclass(frozen=True)
class _ResolvedField:
    value: Any
    layer: str
    source: str
    field: str


@dataclass(frozen=True)
class FieldArgSpec:
    render: Callable[[Any], list[str]]


def parse_managed_options(tokens: list[str]) -> list[ParsedOption]:
    """管理対象だけを抽出し、別名を canonical 名へ解決する。

    入力トークン自体は変更しない。ガード後にそのまま yt-dlp へ渡すことで、
    アプリが知らない新しいオプションも利用できる自由度を維持する。
    """
    found: list[ParsedOption] = []
    for index, token in enumerate(tokens):
        option_token, separator, inline_value = token.partition("=")
        canonical = OPTION_ALIASES.get(option_token)
        value: str | None = inline_value if separator else None

        if canonical is None:
            for short, short_canonical in SHORT_VALUE_ALIASES.items():
                if token.startswith(short) and len(token) > len(short):
                    canonical = short_canonical
                    value = token[len(short) :]
                    break

        if canonical is None:
            continue
        if value is None and index + 1 < len(tokens):
            value = tokens[index + 1]
        found.append(
            ParsedOption(canonical=canonical, token=token, index=index, value=value)
        )
    return found


def guard_freeform(
    value: str | None,
    *,
    source_label: str,
    expert_mode: bool = False,
    env_allow_exec: bool = False,
    profile_allow_exec: bool = False,
    command_kind: CommandKind = "download",
) -> GuardedTokens:
    """自由入力をトークン化し、予約引数を検証する。

    `--download-archive` は expert_mode でも許可しない。Item / Target が唯一の
    状態管理源であり、別の archive ファイルを併用すると状態が分岐するためである。
    """
    if value is None or not value.strip():
        return GuardedTokens(tokens=(), warnings=(), reserved_overrides=frozenset())
    try:
        tokens = shlex.split(value)
    except ValueError as exc:
        raise OptionValidationError(f"{source_label} の引用符が不正です: {exc}") from exc

    warnings: list[str] = []
    overridden: set[str] = set()
    warned: set[str] = set()
    for occurrence in parse_managed_options(tokens):
        option = occurrence.canonical
        if option == "--output":
            raise OptionValidationError(
                "`--output` / `-o` は自由入力では指定できません。"
                "layout_strategy=custom と profile.output_template を使用してください"
            )
        if option == "--download-archive":
            raise OptionValidationError(
                "`--download-archive` は使用できません。Item / Target が唯一の状態管理源です"
            )
        if option in EXEC_OPTIONS:
            if not (env_allow_exec and profile_allow_exec):
                raise OptionValidationError(
                    f"`{option}` は ALLOW_EXEC=true と Profile.allow_exec=true の"
                    "両方が必要です"
                )
            overridden.add(option)
            message = f"{source_label}: `{option}` による任意コマンド実行が有効です"
            if message not in warned:
                warnings.append(message)
                warned.add(message)
            continue
        if option in RESERVED_OPTIONS:
            if not expert_mode:
                raise OptionValidationError(
                    f"{source_label}: 予約引数 `{option}` は expert_mode=false では指定できません"
                )
            overridden.add(option)
            message = (
                f"{source_label}: expert_mode により予約引数 `{option}` を許可しました。"
                "ファイル追跡や出力の取得が壊れる可能性があります"
            )
            if message not in warned:
                warnings.append(message)
                warned.add(message)

        reason = WARNING_REASONS.get(option)
        if reason is not None and not (option == "--simulate" and command_kind == "discover"):
            message = f"{source_label}: `{option}` — {reason}"
            if message not in warned:
                warnings.append(message)
                warned.add(message)

    return GuardedTokens(
        tokens=tuple(tokens),
        warnings=tuple(warnings),
        reserved_overrides=frozenset(overridden),
    )


def _value_arg(option: str) -> Callable[[Any], list[str]]:
    return lambda value: [option, str(value)]


def _bool_arg(enabled: str, disabled: str) -> Callable[[Any], list[str]]:
    return lambda value: [enabled if bool(value) else disabled]


def _audio_extract_args(value: Any) -> list[str]:
    # yt-dlp 2026.07.04 には --no-extract-audio がない。構造化フィールドは
    # 最終解決値だけを出力するため、false は -x を出さないことで明示的に無効化できる。
    return ["--extract-audio"] if bool(value) else []


def _container_args(value: Any) -> list[str]:
    return ["--merge-output-format", str(value), "--remux-video", str(value)]


def _subtitle_lang_args(value: Any) -> list[str]:
    if not str(value).strip():
        return ["--no-write-subs"]
    return ["--write-subs", "--sub-langs", str(value)]


def _parse_metadata_args(value: Any) -> list[str]:
    rendered: list[str] = []
    for expression in value:
        rendered.extend(["--parse-metadata", str(expression)])
    return rendered


# 構造化フィールドから yt-dlp 引数への変換はここだけに集約する。
FIELD_TO_ARGS: dict[str, FieldArgSpec] = {
    "format_selector": FieldArgSpec(_value_arg("--format")),
    "container": FieldArgSpec(_container_args),
    "audio_extract": FieldArgSpec(_audio_extract_args),
    "audio_format": FieldArgSpec(_value_arg("--audio-format")),
    "audio_quality": FieldArgSpec(_value_arg("--audio-quality")),
    "embed_metadata": FieldArgSpec(
        _bool_arg("--embed-metadata", "--no-embed-metadata")
    ),
    "embed_thumbnail": FieldArgSpec(
        _bool_arg("--embed-thumbnail", "--no-embed-thumbnail")
    ),
    "embed_chapters": FieldArgSpec(
        _bool_arg("--embed-chapters", "--no-embed-chapters")
    ),
    "subtitle_langs": FieldArgSpec(_subtitle_lang_args),
    "subtitle_auto": FieldArgSpec(
        _bool_arg("--write-auto-subs", "--no-write-auto-subs")
    ),
    "subtitle_embed": FieldArgSpec(_bool_arg("--embed-subs", "--no-embed-subs")),
    "concurrent_fragments": FieldArgSpec(_value_arg("--concurrent-fragments")),
    "parse_metadata": FieldArgSpec(_parse_metadata_args),
}

FIELD_ORDER = tuple(FIELD_TO_ARGS)
PROFILE_FIELDS = tuple(field_name for field_name in FIELD_ORDER if field_name != "parse_metadata")


class _CommandAccumulator:
    def __init__(self) -> None:
        self.args: list[str] = []
        self.origins: list[ArgumentOrigin] = []

    def add(
        self,
        arguments: list[str] | tuple[str, ...],
        *,
        layer: str,
        source: str,
        field_name: str | None = None,
    ) -> None:
        if not arguments:
            return
        group = tuple(arguments)
        self.args.extend(group)
        self.origins.append(
            ArgumentOrigin(
                arguments=group,
                layer=layer,
                source=source,
                field=field_name,
            )
        )


def _common_rate_args(session: Session, accumulator: _CommandAccumulator) -> None:
    from sluicery.core import settings as core_settings

    mappings = (
        ("download.sleep_requests", "--sleep-requests"),
        ("download.sleep_interval", "--sleep-interval"),
        ("download.max_sleep_interval", "--max-sleep-interval"),
        ("download.limit_rate", "--limit-rate"),
        ("download.retries", "--retries"),
        ("download.fragment_retries", "--fragment-retries"),
    )
    for key, option in mappings:
        accumulator.add(
            [option, str(core_settings.get(session, key))],
            layer="L2",
            source="グローバル既定",
            field_name=key,
        )


def _kind_values(session: Session, kind: str) -> dict[str, Any]:
    from sluicery.core import settings as core_settings

    prefix = f"defaults.{kind}."
    values: dict[str, Any] = {}
    for field_name in FIELD_ORDER:
        key = f"{prefix}{field_name}"
        if key in core_settings.CODE_DEFAULTS:
            values[field_name] = core_settings.get(session, key)
    return values


def _resolve_structured_fields(
    session: Session,
    *,
    kind: str,
    profile: Profile | None,
    overrides: OptionOverrides,
) -> dict[str, _ResolvedField]:
    from sluicery.core import settings as core_settings

    resolved: dict[str, _ResolvedField] = {}
    l2 = {"concurrent_fragments": core_settings.get(session, "download.concurrent_fragments")}
    for field_name, value in l2.items():
        if value is not None:
            resolved[field_name] = _ResolvedField(
                value, "L2", "グローバル既定", f"download.{field_name}"
            )

    for field_name, value in _kind_values(session, kind).items():
        if value is not None:
            resolved[field_name] = _ResolvedField(
                value, "L3", f"種別既定 {kind}", f"defaults.{kind}.{field_name}"
            )

    if profile is not None:
        for field_name in PROFILE_FIELDS:
            value = getattr(profile, field_name)
            if value is not None:
                resolved[field_name] = _ResolvedField(
                    value, "L4", f"Profile {profile.name}", field_name
                )

    unknown = set(overrides.structured) - set(FIELD_TO_ARGS)
    if unknown:
        raise OptionValidationError(
            f"未対応の一時上書きフィールドです: {', '.join(sorted(unknown))}"
        )
    for field_name, value in overrides.structured.items():
        if value is not None:
            resolved[field_name] = _ResolvedField(
                value, "L6", "一時上書き", field_name
            )
    return resolved


def _render_structured(
    resolved: Mapping[str, _ResolvedField], accumulator: _CommandAccumulator
) -> None:
    for field_name in FIELD_ORDER:
        field = resolved.get(field_name)
        if field is None:
            continue
        arguments = FIELD_TO_ARGS[field_name].render(field.value)
        accumulator.add(
            arguments,
            layer=field.layer,
            source=field.source,
            field_name=field.field,
        )


def _guard_layers(
    layers: list[tuple[str, str, str | None]],
    *,
    profile: Profile | None,
    env_allow_exec: bool,
    command_kind: CommandKind,
) -> tuple[list[tuple[str, str, GuardedTokens]], list[str], set[str]]:
    guarded_layers: list[tuple[str, str, GuardedTokens]] = []
    warnings: list[str] = []
    overridden: set[str] = set()
    for layer, source, value in layers:
        guarded = guard_freeform(
            value,
            source_label=source,
            expert_mode=bool(profile and profile.expert_mode),
            env_allow_exec=env_allow_exec,
            profile_allow_exec=bool(profile and profile.allow_exec),
            command_kind=command_kind,
        )
        guarded_layers.append((layer, source, guarded))
        warnings.extend(guarded.warnings)
        overridden.update(guarded.reserved_overrides)
    return guarded_layers, warnings, overridden


def _add_guarded_layers(
    layers: list[tuple[str, str, GuardedTokens]], accumulator: _CommandAccumulator
) -> None:
    for layer, source, guarded in layers:
        accumulator.add(
            guarded.tokens,
            layer=layer,
            source=source,
            field_name="ytdlp_args",
        )


def _timeout(session: Session, command_kind: CommandKind) -> TimeoutSpec:
    from sluicery.core import settings as core_settings

    term_grace = core_settings.get(session, "ytdlp.term_grace_sec")
    if command_kind == "discover":
        discover = core_settings.get(session, "ytdlp.discover_timeout_sec")
        return TimeoutSpec(discover, discover, term_grace)
    return TimeoutSpec(
        core_settings.get(session, "ytdlp.idle_timeout_sec"),
        core_settings.get(session, "ytdlp.absolute_timeout_sec"),
        term_grace,
    )


def _reserved_warning(option: str, command_kind: CommandKind) -> str:
    if option in {"--print", "--progress-template"}:
        return (
            f"expert_mode の `{option}` を優先したため L1 の同引数を注入しません。"
            "進捗・生成パスを取得できない可能性があります"
        )
    if option == "--paths":
        return (
            "expert_mode の `--paths` を優先したため L1 の出力先を注入しません。"
            f"{command_kind} のファイル追跡が壊れる可能性があります"
        )
    return f"expert_mode の `{option}` が L1 の出力プロトコルへ影響する可能性があります"


def build_discover_args(
    playlist: Playlist,
    *,
    session: Session,
    profile: Profile | None = None,
    overrides: OptionOverrides | None = None,
    env_allow_exec: bool = False,
) -> BuiltCommand:
    """Playlist の構成だけを取得する discover コマンドを組み立てる。"""
    from sluicery.downloader.protocol import PRINT_PREFIX

    overrides = overrides or OptionOverrides()
    accumulator = _CommandAccumulator()
    _common_rate_args(session, accumulator)

    guarded_layers, warnings, reserved = _guard_layers(
        [
            ("L5", f"Playlist {playlist.name}", playlist.ytdlp_args),
            ("L6", "一時上書き", overrides.ytdlp_args),
        ],
        profile=profile,
        env_allow_exec=env_allow_exec,
        command_kind="discover",
    )
    _add_guarded_layers(guarded_layers, accumulator)

    accumulator.add(["--flat-playlist"], layer="L1", source="アプリ予約引数")
    accumulator.add(["--simulate"], layer="L1", source="アプリ予約引数")
    if "--print" not in reserved:
        accumulator.add(
            ["--print", f"{PRINT_PREFIX}%()j"],
            layer="L1",
            source="アプリ予約引数",
        )
    for option in sorted(reserved):
        warnings.append(_reserved_warning(option, "discover"))
    source_url = validate_source_url(playlist.url)
    accumulator.add(
        ["--", source_url],
        layer="L1",
        source=f"Playlist {playlist.name}",
        field_name="url",
    )
    return BuiltCommand(
        args=tuple(accumulator.args),
        origins=tuple(accumulator.origins),
        warnings=tuple(dict.fromkeys(warnings)),
        resolved_output_path=None,
        timeout=_timeout(session, "discover"),
    )


def build_download_args(
    target: Target | None,
    *,
    source_url: str,
    session: Session,
    staging_dir: Path,
    work_id: str,
    playlist: Playlist | None = None,
    profile: Profile | None = None,
    playlist_profile: PlaylistProfile | None = None,
    kind: str | None = None,
    overrides: OptionOverrides | None = None,
    env_allow_exec: bool = False,
) -> BuiltCommand:
    """単一 source_url を Staging に取得する download コマンドを組み立てる。"""
    from sluicery.core import settings as core_settings
    from sluicery.downloader.protocol import PRINT_PREFIX, PROGRESS_PREFIX
    from sluicery.layout import LayoutContext, resolve_layout

    overrides = overrides or OptionOverrides()
    source_url = validate_source_url(source_url)
    selected_kind = kind or (profile.kind.value if profile is not None else "video")
    if selected_kind not in {"video", "music", "other"}:
        raise OptionValidationError(f"未対応の Profile kind です: {selected_kind}")

    playlist_name = playlist.name if playlist is not None else "fetch"
    playlist_folder = playlist.folder_name if playlist is not None else "fetch"
    profile_name = profile.name if profile is not None else selected_kind
    strategy_name = profile.layout_strategy.value if profile is not None else "flat"
    subpath = playlist_profile.subpath if playlist_profile is not None else "{playlist.folder_name}"
    layout = resolve_layout(
        strategy_name,
        LayoutContext(
            playlist_name=playlist_name,
            playlist_folder_name=playlist_folder,
            profile_name=profile_name,
            profile_kind=selected_kind,
            subpath=subpath,
            custom_output_template=profile.output_template if profile is not None else None,
        ),
    )

    accumulator = _CommandAccumulator()
    _common_rate_args(session, accumulator)
    structured = _resolve_structured_fields(
        session,
        kind=selected_kind,
        profile=profile,
        overrides=overrides,
    )
    _render_structured(structured, accumulator)

    # ファイル名ポリシーは自由文字列より前に置き、明示的な衝突指定は警告付きで
    # 後勝ちにできる。custom は利用者の長さ指定を尊重し、末尾 ID を一括 trim で
    # 切り落とさないため --trim-filenames を注入しない（D-018）。
    accumulator.add(["--windows-filenames"], layer="L2", source="ファイル名既定")
    if strategy_name == "flat":
        accumulator.add(
            ["--trim-filenames", str(core_settings.get(session, "download.trim_filenames"))],
            layer="L2",
            source="ファイル名既定",
            field_name="download.trim_filenames",
        )

    guarded_layers, warnings, reserved = _guard_layers(
        [
            ("L4", f"Profile {profile.name}", profile.ytdlp_args)
            if profile is not None
            else ("L4", "Profileなし", None),
            ("L5", f"Playlist {playlist.name}", playlist.ytdlp_args)
            if playlist is not None
            else ("L5", "Playlistなし", None),
            ("L6", "一時上書き", overrides.ytdlp_args),
        ],
        profile=profile,
        env_allow_exec=env_allow_exec,
        command_kind="download",
    )
    _add_guarded_layers(guarded_layers, accumulator)
    warnings.extend(layout.warnings)

    work_root = staging_dir / work_id
    temp_root = work_root / ".tmp"
    relative_output = layout.relative_output_template
    if "--paths" not in reserved:
        accumulator.add(
            ["--paths", f"home:{work_root}"], layer="L1", source="アプリ予約引数"
        )
        accumulator.add(
            ["--paths", f"temp:{temp_root}"], layer="L1", source="アプリ予約引数"
        )
    # --output は自由文字列から常に拒否するため、正規の layout 経路だけを注入する。
    accumulator.add(
        ["--output", relative_output],
        layer="L1",
        source=f"layout {strategy_name}",
        field_name="output_template",
    )
    accumulator.add(
        ["--continue", "--newline", "--progress"],
        layer="L1",
        source="アプリ予約引数",
    )
    if "--progress-template" not in reserved:
        accumulator.add(
            ["--progress-template", f"download:{PROGRESS_PREFIX}%(progress)j"],
            layer="L1",
            source="アプリ予約引数",
        )
    if "--print" not in reserved:
        accumulator.add(
            ["--print", f"after_move:{PRINT_PREFIX}%(filepath)s"],
            layer="L1",
            source="アプリ予約引数",
        )
    for option in sorted(reserved):
        warnings.append(_reserved_warning(option, "download"))
    source_label = f"Target {target.id}" if target is not None and target.id else "source_url"
    accumulator.add(
        ["--", source_url], layer="L1", source=source_label, field_name="source_url"
    )

    return BuiltCommand(
        args=tuple(accumulator.args),
        origins=tuple(accumulator.origins),
        warnings=tuple(dict.fromkeys(warnings)),
        resolved_output_path=work_root / relative_output,
        timeout=_timeout(session, "download"),
    )


__all__ = [
    "ArgumentOrigin",
    "BuiltCommand",
    "EXEC_OPTIONS",
    "FIELD_TO_ARGS",
    "FieldArgSpec",
    "GuardedTokens",
    "OPTION_ALIASES",
    "OptionOverrides",
    "OptionValidationError",
    "ParsedOption",
    "RESERVED_OPTIONS",
    "WARNING_REASONS",
    "build_discover_args",
    "build_download_args",
    "guard_freeform",
    "guard_raw_exec_args",
    "parse_managed_options",
    "validate_source_url",
]
