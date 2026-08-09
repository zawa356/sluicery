"""rclone の stderr / JSON ログに対する Storage 固有のエラー分類。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class StorageClassification(StrEnum):
    OK = "ok"
    FAILED = "failed"
    UNREACHABLE = "unreachable"
    AUTH_FAILED = "auth_failed"
    NO_SPACE = "no_space"
    PERMISSION_DENIED = "permission_denied"


@dataclass(frozen=True)
class StorageClassificationResult:
    classification: StorageClassification
    reason_code: str


ERROR_RULES: list[tuple[re.Pattern[str], StorageClassification, str]] = [
    (
        re.compile(
            r"NT_STATUS_LOGON_FAILURE|LOGON_FAILURE|user name or password is incorrect|"
            r"authentication failed|invalid credentials|authorization failed|"
            r"attempted logon is invalid|bad username or authentication information",
            re.I,
        ),
        StorageClassification.AUTH_FAILED,
        "authentication_failed",
    ),
    (
        re.compile(r"no space left on device|disk full|quota exceeded|not enough space", re.I),
        StorageClassification.NO_SPACE,
        "no_space",
    ),
    (
        re.compile(
            r"NT_STATUS_ACCESS_DENIED|permission denied|access denied|operation not permitted|"
            r"read-only file system",
            re.I,
        ),
        StorageClassification.PERMISSION_DENIED,
        "permission_denied",
    ),
    (
        re.compile(
            r"connection refused|no route to host|network is unreachable|network is down|"
            r"host is down|i/o timeout|connection timed out|temporary failure in name resolution|"
            r"no such host|failed to resolve|failed to connect|dial tcp|connection reset by peer",
            re.I,
        ),
        StorageClassification.UNREACHABLE,
        "unreachable",
    ),
]


def classify_stderr(stderr: str) -> StorageClassificationResult:
    for pattern, classification, reason_code in ERROR_RULES:
        if pattern.search(stderr):
            return StorageClassificationResult(classification, reason_code)
    return StorageClassificationResult(StorageClassification.FAILED, "unknown_error")


def classify(returncode: int, stderr: str) -> StorageClassificationResult:
    if returncode == 0:
        return StorageClassificationResult(StorageClassification.OK, "ok")
    return classify_stderr(stderr)


__all__ = [
    "ERROR_RULES",
    "StorageClassification",
    "StorageClassificationResult",
    "classify",
    "classify_stderr",
]
