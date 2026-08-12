from sluicery.hooks.base import emit_safely


def test_hook_failure_does_not_escape() -> None:
    class BrokenHook:
        def emit(self, event_type: str, payload: dict) -> None:
            raise RuntimeError("broken")

    emit_safely(BrokenHook(), "target_downloaded", {"target_id": 1})
