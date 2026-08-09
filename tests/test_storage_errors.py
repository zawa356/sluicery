from __future__ import annotations

import pytest

from sluicery.storage.errors import StorageClassification, classify, classify_stderr


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("dial tcp: connection refused", StorageClassification.UNREACHABLE),
        ("NT_STATUS_LOGON_FAILURE", StorageClassification.AUTH_FAILED),
        (
            "The attempted logon is invalid due to a bad username or authentication information",
            StorageClassification.AUTH_FAILED,
        ),
        ("no space left on device", StorageClassification.NO_SPACE),
        ("NT_STATUS_ACCESS_DENIED", StorageClassification.PERMISSION_DENIED),
    ],
)
def test_storage_error_classification(message: str, expected: StorageClassification) -> None:
    assert classify_stderr(message).classification == expected


def test_unknown_storage_error_is_failed() -> None:
    result = classify_stderr("unexpected backend error")
    assert result.classification == StorageClassification.FAILED


def test_zero_returncode_is_ok() -> None:
    assert classify(0, "permission denied").classification == StorageClassification.OK
