from __future__ import annotations

import json

import pytest

from sluicery.core.format_probe import (
    FormatProbeLimiter,
    FormatProbeRateLimited,
    FormatProbeResultError,
    parse_format_probe_output,
)


def test_parse_format_probe_output_extracts_only_display_fields() -> None:
    result = parse_format_probe_output(
        json.dumps(
            {
                "webpage_url": "https://example.com/private?token=secret",
                "format_id": "137+140",
                "formats": [
                    {
                        "format_id": "137",
                        "ext": "mp4",
                        "resolution": "1920x1080",
                        "vcodec": "avc1",
                        "acodec": "none",
                        "filesize": 1000,
                        "url": "https://cdn.example/private",
                    },
                    {
                        "format_id": "140",
                        "ext": "m4a",
                        "vcodec": "none",
                        "acodec": "mp4a",
                        "filesize_approx": 250,
                    },
                ],
                "requested_formats": [
                    {"format_id": "137", "filesize": 1000},
                    {"format_id": "140", "filesize_approx": 250},
                ],
            }
        )
    )

    assert result.selected_format_ids == ("137", "140")
    assert result.estimated_size == 1250
    assert [row.format_id for row in result.formats] == ["137", "140"]
    assert not hasattr(result.formats[0], "url")


def test_parse_format_probe_output_rejects_invalid_json() -> None:
    with pytest.raises(FormatProbeResultError):
        parse_format_probe_output("not-json")


def test_partial_selected_format_sizes_are_not_reported_as_total() -> None:
    result = parse_format_probe_output(
        json.dumps(
            {
                "requested_formats": [
                    {"format_id": "video", "filesize": 1000},
                    {"format_id": "audio"},
                ]
            }
        )
    )

    assert result.estimated_size is None


def test_format_probe_limiter_applies_global_minimum_interval() -> None:
    limiter = FormatProbeLimiter()
    limiter.acquire(10, now=100)

    with pytest.raises(FormatProbeRateLimited) as exc_info:
        limiter.acquire(10, now=103)
    assert exc_info.value.retry_after_sec == 7

    limiter.acquire(10, now=110)
