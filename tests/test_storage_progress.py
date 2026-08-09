from __future__ import annotations

import json

from sluicery.storage.progress import MAX_LINE_LEN, parse_rclone_log_line


def test_parse_rclone_stats_json() -> None:
    event = parse_rclone_log_line(
        json.dumps(
            {
                "level": "info",
                "stats": {
                    "bytes": 5,
                    "totalBytes": 10,
                    "speed": 2.5,
                    "eta": 2,
                    "transfers": 1,
                    "totalTransfers": 2,
                    "errors": 0,
                    "elapsedTime": 0.5,
                },
            }
        )
    )
    assert event is not None
    assert event.bytes_transferred == 5
    assert event.total_bytes == 10
    assert event.speed == 2.5
    assert event.total_transfers == 2


def test_parse_rclone_log_ignores_non_stats_and_broken_lines() -> None:
    assert parse_rclone_log_line("") is None
    assert parse_rclone_log_line("not-json") is None
    assert parse_rclone_log_line("[]") is None
    assert parse_rclone_log_line('{"level":"info"}') is None


def test_parse_rclone_log_rejects_extremely_long_line() -> None:
    assert parse_rclone_log_line("{" + "x" * MAX_LINE_LEN + "}") is None
