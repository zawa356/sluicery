from __future__ import annotations

from sluicery.tasks.progress import ProgressWriter


def test_progress_throttles_by_time_and_percent_but_always_writes_final() -> None:
    now = [0.0]
    writes: list[dict] = []
    writer = ProgressWriter(
        writes.append,
        interval_sec=2,
        percent_step=5,
        clock=lambda: now[0],
    )

    assert writer.emit({"percent": 0})
    now[0] = 0.5
    assert not writer.emit({"percent": 4.9})
    assert writer.emit({"percent": 5})
    now[0] = 1.0
    assert not writer.emit({"percent": 6})
    now[0] = 3.1
    assert writer.emit({"percent": 6})
    assert writer.emit({"status": "failed", "percent": None}, final=True)
    assert len(writes) == 4
