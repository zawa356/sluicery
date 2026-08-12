"""実パイプラインハンドラをworker registryへ組み立てる。"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from sluicery.config import Settings
from sluicery.core.settings import OperationalSettings
from sluicery.downloader.version import current_ytdlp_bin, ytdlp_root
from sluicery.downloader.ytdlp import YtdlpRunner
from sluicery.runner.ffprobe import FFprobeRunner
from sluicery.tasks.handlers.download import DownloadHandler
from sluicery.tasks.handlers.dummy import TaskHandler
from sluicery.tasks.handlers.index import IndexHandler
from sluicery.tasks.handlers.postprocess import PostprocessHandler
from sluicery.tasks.handlers.publish import PublishHandler
from sluicery.tasks.handlers.verify import VerifyHandler


def build_pipeline_handler_factories(
    session_factory: sessionmaker[Session], settings: Settings
) -> dict[str, Callable[[], TaskHandler]]:
    assert settings.STAGING_DIR is not None
    staging_dir = settings.STAGING_DIR
    with session_factory() as session:
        ops = OperationalSettings(session)
        stderr_tail_kb = ops.ytdlp_stderr_tail_kb
        verify_timeout = ops.pipeline_verify_timeout_sec
        delete_staging = ops.sync_delete_staging_after_index
    ytdlp_bin = current_ytdlp_bin(ytdlp_root(settings.DATA_DIR))
    log_dir = settings.DATA_DIR / "logs"
    return {
        "download": lambda: DownloadHandler(
            session_factory,
            staging_dir=staging_dir,
            runner=YtdlpRunner(
                ytdlp_bin,
                stderr_tail_kb=stderr_tail_kb,
                log_dir=log_dir,
            ),
            env_allow_exec=settings.ALLOW_EXEC,
        ),
        "verify": lambda: VerifyHandler(
            session_factory,
            runner=FFprobeRunner(),
            timeout_sec=verify_timeout,
        ),
        "postprocess": lambda: PostprocessHandler(session_factory),
        "publish": lambda: PublishHandler(
            session_factory,
            staging_dir=staging_dir,
        ),
        "index": lambda: IndexHandler(
            session_factory,
            staging_dir=staging_dir,
            delete_staging=delete_staging,
        ),
    }


__all__ = ["build_pipeline_handler_factories"]
