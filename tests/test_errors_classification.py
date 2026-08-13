from __future__ import annotations

from sluicery.downloader.errors import Classification, classify, classify_stderr


def test_ok_on_returncode_zero() -> None:
    result = classify(0, "")
    assert result.classification == Classification.OK


def test_video_unavailable_classified_unavailable() -> None:
    result = classify_stderr("ERROR: [youtube] xxxx: Video unavailable")
    assert result.classification == Classification.UNAVAILABLE


def test_private_video_classified_unavailable() -> None:
    result = classify_stderr("ERROR: Private video. Sign in if you've been granted access")
    assert result.classification == Classification.UNAVAILABLE


def test_members_only_classified_unavailable() -> None:
    result = classify_stderr("Join this channel to get access to members-only content")
    assert result.classification == Classification.UNAVAILABLE


def test_requested_format_unavailable_does_not_retry() -> None:
    result = classify_stderr("ERROR: Requested format is not available")
    assert result.classification == Classification.UNAVAILABLE
    assert result.reason_code == "format_unavailable"


def test_rate_limited_classified_blocked() -> None:
    result = classify_stderr(
        "ERROR: unable to download video data: HTTP Error 429: Too Many Requests"
    )
    assert result.classification == Classification.BLOCKED
    assert result.reason_code == "rate_limited"


def test_network_unreachable_classified_blocked() -> None:
    result = classify_stderr("urlopen error [Errno -3] Temporary failure in name resolution")
    assert result.classification == Classification.BLOCKED


def test_bot_check_classified_blocked() -> None:
    result = classify_stderr("Sign in to confirm you're not a bot")
    assert result.classification == Classification.BLOCKED
    assert result.reason_code == "bot_check"


def test_http_403_classified_blocked() -> None:
    result = classify_stderr("ERROR: unable to download video data: HTTP Error 403: Forbidden")
    assert result.classification == Classification.BLOCKED
    assert result.reason_code == "http_403"


def test_unknown_message_falls_back_to_failed() -> None:
    result = classify_stderr("something completely unexpected happened")
    assert result.classification == Classification.FAILED
    assert result.reason_code == "unknown_error"


def test_empty_stderr_falls_back_to_failed() -> None:
    result = classify_stderr("")
    assert result.classification == Classification.FAILED
