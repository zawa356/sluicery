from __future__ import annotations

import io
from pathlib import Path

from sqlalchemy import select, text

from sluicery import cli, cli_crud
from sluicery.db.models import Storage
from sluicery.storage.base import (
    ConnectionStage,
    ConnectionStageResult,
    ConnectionTestResult,
    PublishResult,
    RemoteFile,
    StageStatus,
)
from sluicery.storage.errors import StorageClassification


def _patch_sessions(monkeypatch, session_factory) -> None:
    monkeypatch.setattr(cli, "_open_session", lambda: session_factory())


def _successful_connection() -> ConnectionTestResult:
    return ConnectionTestResult(
        tuple(
            ConnectionStageResult(stage, StageStatus.SUCCESS, f"{stage.value} ok")
            for stage in ConnectionStage
        )
    )


class FakeStorageAdapter:
    def __init__(self) -> None:
        self.published: list[tuple[Path, str, bool]] = []

    def test_connection(self) -> ConnectionTestResult:
        return _successful_connection()

    def publish(
        self, src: Path, dest_rel: str, *, overwrite: bool = False
    ) -> PublishResult:
        self.published.append((src, dest_rel, overwrite))
        return PublishResult(
            True,
            dest_rel,
            src.stat().st_size,
            StorageClassification.OK,
            "ok",
            "published",
        )

    def exists(self, rel: str) -> bool:
        return rel == "folder/file.bin"

    def list_recursive(self, rel: str):
        yield RemoteFile("folder/file.bin", 123, "2026-08-09T00:00:00Z")

    def move(self, src_rel: str, dest_rel: str) -> None:
        return None

    def free_space(self) -> int | None:
        return 987654321


def test_remote_add_and_edit_keep_credentials_write_only(
    monkeypatch,
    session_factory,
    base_env,
    capsys,
) -> None:
    _patch_sessions(monkeypatch, session_factory)
    monkeypatch.setattr(cli_crud.sys, "stdin", io.StringIO("first-unit-password\n"))
    assert (
        cli.main(
            [
                "storage",
                "add",
                "--kind",
                "remote",
                "--name",
                "remote-unit",
                "--protocol",
                "smb",
                "--host",
                "smb.example.invalid",
                "--share",
                "unit-share",
                "--path",
                "library",
                "--user",
                "unit-user",
                "--domain",
                "UNIT",
                "--password-stdin",
            ]
        )
        == 0
    )
    assert "first-unit-password" not in capsys.readouterr().out

    session = session_factory()
    try:
        storage = session.scalar(select(Storage).where(Storage.name == "remote-unit"))
        assert storage is not None
        assert storage.kind.value == "remote"
        assert storage.config_json == {
            "protocol": "smb",
            "host": "smb.example.invalid",
            "share": "unit-share",
            "path": "library",
            "port": 445,
        }
        assert storage.credentials_encrypted == {
            "user": "unit-user",
            "password": "first-unit-password",
            "domain": "UNIT",
        }
        raw = session.execute(
            text("SELECT credentials_encrypted FROM storage WHERE id = :id"),
            {"id": storage.id},
        ).scalar_one()
        assert "first-unit-password" not in raw
    finally:
        session.close()

    monkeypatch.setattr(cli_crud.sys, "stdin", io.StringIO("second-unit-password\n"))
    assert (
        cli.main(
            [
                "storage",
                "edit",
                "remote-unit",
                "--user",
                "updated-user",
                "--clear-domain",
                "--password-stdin",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert cli.main(["storage", "show", "remote-unit"]) == 0
    output = capsys.readouterr().out
    for secret in ("first-unit-password", "second-unit-password", "updated-user"):
        assert secret not in output
    assert "設定済み" in output


def test_storage_test_space_ls_and_push_commands(
    monkeypatch,
    session_factory,
    base_env,
    capsys,
    tmp_path: Path,
) -> None:
    _patch_sessions(monkeypatch, session_factory)
    assert (
        cli.main(
            [
                "storage",
                "add",
                "--kind",
                "local",
                "--name",
                "local-unit",
                "--path",
                "/mnt/media",
            ]
        )
        == 0
    )
    capsys.readouterr()
    adapter = FakeStorageAdapter()
    monkeypatch.setattr(cli_crud, "create_storage_adapter", lambda storage, settings: adapter)

    assert cli.main(["storage", "test", "local-unit"]) == 0
    test_output = capsys.readouterr().out
    for label in ("疎通", "認証", "一覧", "書き込み"):
        assert f"{label}: success" in test_output

    session = session_factory()
    try:
        storage = session.scalar(select(Storage).where(Storage.name == "local-unit"))
        assert storage is not None
        assert storage.last_check_at is not None
        assert storage.last_check_result_json is not None
        assert storage.last_check_result_json["ok"] is True
        assert len(storage.last_check_result_json["stages"]) == 4
    finally:
        session.close()

    assert cli.main(["storage", "space", "local-unit"]) == 0
    assert "987654321 bytes" in capsys.readouterr().out
    assert cli.main(["storage", "ls", "local-unit"]) == 0
    listing = capsys.readouterr().out
    assert "folder/file.bin" in listing
    assert "123" in listing

    source = tmp_path / "push-source.bin"
    source.write_bytes(b"abc")
    assert (
        cli.main(
            [
                "storage",
                "push",
                "local-unit",
                str(source),
                "folder/pushed.bin",
            ]
        )
        == 0
    )
    assert "publish 完了" in capsys.readouterr().out
    assert adapter.published == [(source, "folder/pushed.bin", False)]


def test_local_cli_rejects_media_root_escape(
    monkeypatch,
    session_factory,
    base_env,
    capsys,
) -> None:
    _patch_sessions(monkeypatch, session_factory)
    assert (
        cli.main(
            [
                "storage",
                "add",
                "--kind",
                "local",
                "--name",
                "escaped",
                "--path",
                "/tmp/outside-media-root",
            ]
        )
        == 1
    )
    assert "/mnt/media" in capsys.readouterr().err
