"""yt-dlp オプションの合成・ガード（要件定義 §9、Phase 4）。

自由入力は必ず :func:`guard_freeform` でトークン化してから扱う。yt-dlp の
全オプションを再実装せず、アプリの出力プロトコルやファイル追跡に影響する
予約引数と警告対象だけを認識する。
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from typing import Literal

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


__all__ = [
    "EXEC_OPTIONS",
    "GuardedTokens",
    "OPTION_ALIASES",
    "OptionValidationError",
    "ParsedOption",
    "RESERVED_OPTIONS",
    "WARNING_REASONS",
    "guard_freeform",
    "parse_managed_options",
]
