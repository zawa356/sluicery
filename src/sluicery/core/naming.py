"""ユーザー入力由来のディレクトリ名を安全な相対パスへ正規化する。

yt-dlp が出力テンプレートから生成するファイル名は `--windows-filenames` と
`--trim-filenames` に委ねる。本モジュールは playlist.folder_name と subpath
だけを扱い、実ファイル名を二重にサニタイズしない。
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import PurePosixPath, PureWindowsPath

WINDOWS_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{number}" for number in range(1, 10)}
    | {f"LPT{number}" for number in range(1, 10)}
)
FORBIDDEN_COMPONENT_CHARS = re.compile(r'[\\/:*?"<>|]')
CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
WINDOWS_DRIVE = re.compile(r"^[A-Za-z]:")


class NamingValidationError(ValueError):
    """空要素・絶対パス・traversal など、安全に補正できない入力。"""


def sanitize_component(value: str, *, replacement: str = "_") -> str:
    """単一のパス要素を NFC 化し、Windows / SMB 互換にする。"""
    normalized = unicodedata.normalize("NFC", value)
    normalized = CONTROL_CHARS.sub("", normalized)
    normalized = FORBIDDEN_COMPONENT_CHARS.sub(replacement, normalized)
    normalized = normalized.rstrip(" .")
    if not normalized or normalized in {".", ".."}:
        raise NamingValidationError("空、`.`、`..` はパス要素に使用できません")

    # Windows 予約語は拡張子が付いていても予約される（例: CON.txt）。
    stem = normalized.split(".", 1)[0].upper()
    if stem in WINDOWS_RESERVED_NAMES:
        normalized = f"_{normalized}"
    return normalized


def validate_relative_path(value: str) -> None:
    """相対パスであり、`.` / `..` 要素を含まないことを検証する。"""
    normalized = unicodedata.normalize("NFC", value)
    if (
        not normalized
        or PurePosixPath(normalized).is_absolute()
        or PureWindowsPath(normalized).is_absolute()
        or WINDOWS_DRIVE.match(normalized)
    ):
        raise NamingValidationError("subpath には空文字または絶対パスを指定できません")
    parts = normalized.replace("\\", "/").split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise NamingValidationError("subpath に空要素、`.`、`..` は使用できません")


def sanitize_relative_path(value: str) -> str:
    """相対 subpath の各要素を安全化し、区切りを `/` に統一する。"""
    validate_relative_path(value)
    normalized = unicodedata.normalize("NFC", value).replace("\\", "/")
    return "/".join(sanitize_component(part) for part in normalized.split("/"))


__all__ = [
    "NamingValidationError",
    "WINDOWS_RESERVED_NAMES",
    "sanitize_component",
    "sanitize_relative_path",
    "validate_relative_path",
]
