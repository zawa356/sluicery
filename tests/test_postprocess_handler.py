from __future__ import annotations

from sluicery.db.models import (
    Item,
    LayoutStrategy,
    Playlist,
    PlaylistKindHint,
    PlaylistProfile,
    Profile,
    ProfileKind,
    Storage,
    StorageKind,
    Target,
    Task,
    TaskStatus,
    TaskType,
    WorkerClass,
)
from sluicery.tasks.handlers.postprocess import PostprocessHandler
from sluicery.tasks.queue import TaskOutcome


def test_empty_postprocess_chain_passes_dependency_payload(session_factory) -> None:
    with session_factory() as session:
        storage = Storage(name="s", kind=StorageKind.LOCAL, config_json={"path": "out"})
        profile = Profile(
            name="p",
            kind=ProfileKind.VIDEO,
            layout_strategy=LayoutStrategy.FLAT,
            postprocess_chain_json=[],
        )
        playlist = Playlist(
            name="p", folder_name="p", url="https://example.com", kind_hint=PlaylistKindHint.VIDEO
        )
        session.add_all([storage, profile, playlist])
        session.flush()
        assignment = PlaylistProfile(
            playlist_id=playlist.id, profile_id=profile.id, storage_id=storage.id
        )
        item = Item(playlist_id=playlist.id, source_id="x", source_url="https://example.com/x")
        session.add_all([assignment, item])
        session.flush()
        target = Target(item_id=item.id, playlist_profile_id=assignment.id)
        session.add(target)
        session.flush()
        verify = Task(
            type=TaskType.VERIFY,
            target_ref_type="target",
            target_ref_id=target.id,
            payload_json={"file_path": "/tmp/a", "duration": 10},
            worker_class=WorkerClass.COMPUTE,
            status=TaskStatus.SUCCEEDED,
        )
        session.add(verify)
        session.flush()
        postprocess = Task(
            type=TaskType.POSTPROCESS,
            target_ref_type="target",
            target_ref_id=target.id,
            payload_json={"target_id": target.id},
            worker_class=WorkerClass.COMPUTE,
            status=TaskStatus.RUNNING,
            depends_on_task_id=verify.id,
        )
        session.add(postprocess)
        session.commit()
        target_id = target.id
        task_id = postprocess.id

    result = PostprocessHandler(session_factory).run(
        {"target_id": target_id, "_execution": {"task_id": task_id}}, lambda _: None
    )

    assert result.outcome == TaskOutcome.SUCCEEDED
    assert result.payload_update == {"file_path": "/tmp/a", "duration": 10}
