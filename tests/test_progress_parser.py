from __future__ import annotations

from sluicery.downloader.progress import MAX_LINE_LEN, parse_progress_line


def test_parses_valid_progress_line() -> None:
    line = (
        '{"status": "downloading", "downloaded_bytes": 1024, "total_bytes": 2048,'
        ' "speed": 512.5, "eta": 2, "fragment_index": 1, "fragment_count": 4,'
        ' "filename": "foo.mp4"}'
    )
    event = parse_progress_line(line)
    assert event is not None
    assert event.status == "downloading"
    assert event.downloaded_bytes == 1024
    assert event.total_bytes == 2048
    assert event.speed == 512.5
    assert event.eta == 2
    assert event.fragment_index == 1
    assert event.fragment_count == 4
    assert event.filename == "foo.mp4"


def test_falls_back_to_total_bytes_estimate() -> None:
    line = '{"status": "downloading", "total_bytes_estimate": 500}'
    event = parse_progress_line(line)
    assert event is not None
    assert event.total_bytes == 500


def test_incomplete_json_returns_none() -> None:
    assert parse_progress_line('{"status": "downloading", "downloaded_by') is None


def test_line_without_prefix_recognizable_status_missing_returns_none() -> None:
    assert parse_progress_line('{"downloaded_bytes": 10}') is None


def test_non_dict_json_returns_none() -> None:
    assert parse_progress_line("[1, 2, 3]") is None


def test_na_and_empty_string_become_none() -> None:
    line = '{"status": "downloading", "eta": "NA", "speed": "", "downloaded_bytes": "NA"}'
    event = parse_progress_line(line)
    assert event is not None
    assert event.eta is None
    assert event.speed is None
    assert event.downloaded_bytes is None


def test_extremely_long_line_returns_none() -> None:
    line = '{"status": "downloading", "filename": "' + ("a" * (MAX_LINE_LEN + 10)) + '"}'
    assert parse_progress_line(line) is None


def test_numeric_strings_are_coerced() -> None:
    line = '{"status": "downloading", "downloaded_bytes": "1024", "speed": "12.5"}'
    event = parse_progress_line(line)
    assert event is not None
    assert event.downloaded_bytes == 1024
    assert event.speed == 12.5


def test_unparseable_garbage_never_raises() -> None:
    for garbage in ["", "not json at all", "{", "null", "true", '{"status": null}']:
        assert parse_progress_line(garbage) is None
