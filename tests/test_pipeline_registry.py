from sluicery.config import load_settings
from sluicery.tasks.handlers.runtime import build_pipeline_handler_factories


def test_pipeline_registry_contains_five_handlers(
    session_factory, base_env, tmp_path, monkeypatch
) -> None:
    from sluicery.tasks.handlers import runtime

    executable = tmp_path / "yt-dlp"
    executable.write_text("#!/bin/sh\n")
    monkeypatch.setattr(runtime, "current_ytdlp_bin", lambda root: executable)

    factories = build_pipeline_handler_factories(session_factory, load_settings())

    assert set(factories) == {"download", "verify", "postprocess", "publish", "index"}
    assert all(callable(factory) for factory in factories.values())
