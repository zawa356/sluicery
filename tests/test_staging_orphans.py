from pathlib import Path

from sluicery.core.staging import find_orphans
from sluicery.db.models import Task, TaskStatus, TaskType, WorkerClass


def test_orphans_are_reported_without_deletion(db_session, tmp_path: Path) -> None:
    staging = tmp_path / "staging"
    tracked = staging / "work-1" / "tracked.mkv"
    tracked.parent.mkdir(parents=True)
    tracked.write_bytes(b"tracked")
    orphan = staging / "trailer_1080p.mov"
    orphan.write_bytes(b"orphan")
    db_session.add(
        Task(
            type=TaskType.DOWNLOAD,
            target_ref_type="target",
            target_ref_id=1,
            payload_json={"work_id": "work-1"},
            worker_class=WorkerClass.NETWORK,
            status=TaskStatus.QUEUED,
        )
    )
    db_session.commit()

    result = find_orphans(db_session, staging)

    assert [item.relative_path for item in result] == ["trailer_1080p.mov"]
    assert orphan.exists()
    assert tracked.exists()
