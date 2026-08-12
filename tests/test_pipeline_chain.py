from sluicery.db.models import TaskStatus, TaskType, WorkerClass
from sluicery.tasks.pipeline import dependency_payload, enqueue_pipeline


def test_enqueue_pipeline_creates_five_linked_tasks(session_factory) -> None:
    with session_factory() as session:
        chain = enqueue_pipeline(session, 42, run_id=None, work_id="work-42")

        assert [task.type for task in chain.all] == [
            TaskType.DOWNLOAD,
            TaskType.VERIFY,
            TaskType.POSTPROCESS,
            TaskType.PUBLISH,
            TaskType.INDEX,
        ]
        assert [task.worker_class for task in chain.all] == [
            WorkerClass.NETWORK,
            WorkerClass.COMPUTE,
            WorkerClass.COMPUTE,
            WorkerClass.NETWORK,
            WorkerClass.NETWORK,
        ]
        assert [task.depends_on_task_id for task in chain.all] == [
            None,
            chain.download.id,
            chain.verify.id,
            chain.postprocess.id,
            chain.publish.id,
        ]
        assert all(task.status == TaskStatus.QUEUED for task in chain.all)
        assert all(
            task.payload_json == {"work_id": "work-42", "target_id": 42} for task in chain.all
        )


def test_dependency_payload_reads_completed_predecessor(session_factory) -> None:
    with session_factory() as session:
        chain = enqueue_pipeline(session, 7, work_id="work-7")
        chain.download.status = TaskStatus.SUCCEEDED
        chain.download.payload_json = {
            "work_id": "work-7",
            "target_id": 7,
            "file_path": "/data/staging/work-7/example.mkv",
        }
        session.commit()

        payload = dependency_payload(session, chain.verify.id)

    assert payload["file_path"].endswith("example.mkv")
