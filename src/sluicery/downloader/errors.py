"""yt-dlp 実行結果のエラー分類（要件定義 §5, docs/phase3_指示書.md §5）。

ルールは stderr の英語メッセージに依存しており壊れやすい。ルールを一箇所の
テーブル（`ERROR_RULES`）に集約し、分類できなかった stderr は `failed` に
落とす（安全側に倒す。§5.2）。`Target` の状態遷移はここでは行わない
（Phase 8 の `core/` の責務。§5.4）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class Classification(StrEnum):
    OK = "ok"
    FAILED = "failed"
    UNAVAILABLE = "unavailable"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ClassificationResult:
    classification: Classification
    reason_code: str


# (パターン, 分類, 理由コード)。上から順に最初にマッチしたものを採用する。
# 正確な文言は yt-dlp のバージョンで変わりうる。実機で確認したパターンに
# 追記・修正すること（docs/phase3_指示書.md §5.3）。
ERROR_RULES: list[tuple[re.Pattern[str], Classification, str]] = [
    # ---- unavailable: 回復不能。自動リトライしない ----
    (
        re.compile(r"requested format is not available", re.I),
        Classification.UNAVAILABLE,
        "format_unavailable",
    ),
    (re.compile(r"video (is )?unavailable", re.I), Classification.UNAVAILABLE, "video_unavailable"),
    (re.compile(r"has been removed", re.I), Classification.UNAVAILABLE, "video_removed"),
    (
        re.compile(r"account associated with this video has been terminated", re.I),
        Classification.UNAVAILABLE,
        "account_terminated",
    ),
    (re.compile(r"private video", re.I), Classification.UNAVAILABLE, "private_video"),
    (
        re.compile(r"(members-only|join this channel to get access)", re.I),
        Classification.UNAVAILABLE,
        "members_only",
    ),
    (
        re.compile(
            r"(not available in your country|"
            r"blocked it (on copyright grounds )?in your country)",
            re.I,
        ),
        Classification.UNAVAILABLE,
        "geo_restricted",
    ),
    (
        re.compile(r"(premieres in|this live event will begin in|scheduled for)", re.I),
        Classification.UNAVAILABLE,
        "not_yet_available",
    ),
    # ---- blocked: 外的要因で保留。retry_count を消費しない ----
    (re.compile(r"HTTP Error 429|Too Many Requests", re.I), Classification.BLOCKED, "rate_limited"),
    (
        re.compile(
            r"(Temporary failure in name resolution|Name or service not known|"
            r"Failed to resolve|Connection refused|Network is unreachable|"
            r"Network is down)",
            re.I,
        ),
        Classification.BLOCKED,
        "network_unreachable",
    ),
    (
        re.compile(r"(sign in to confirm you.?re not a bot|confirm you.?re not a bot)", re.I),
        Classification.BLOCKED,
        "bot_check",
    ),
    (
        re.compile(r"(HTTP Error 403|403 Forbidden|HTTP status(?: code)? 403)", re.I),
        Classification.BLOCKED,
        "http_403",
    ),
]


def classify_stderr(stderr: str) -> ClassificationResult:
    """stderr を分類する。未知のメッセージは `failed`（§5.2）。"""
    for pattern, classification, reason in ERROR_RULES:
        if pattern.search(stderr):
            return ClassificationResult(classification, reason)
    return ClassificationResult(Classification.FAILED, "unknown_error")


def classify(returncode: int, stderr: str) -> ClassificationResult:
    if returncode == 0:
        return ClassificationResult(Classification.OK, "ok")
    return classify_stderr(stderr)


__all__ = ["ERROR_RULES", "Classification", "ClassificationResult", "classify", "classify_stderr"]
