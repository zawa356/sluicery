from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_privileged_overlay_is_separate_and_explicit() -> None:
    base = yaml.safe_load((REPO_ROOT / "compose.yaml").read_text(encoding="utf-8"))
    overlay = yaml.safe_load(
        (REPO_ROOT / "compose.privileged.yaml").read_text(encoding="utf-8")
    )
    for name in ("app", "worker-network", "worker-compute"):
        base_service = base["services"][name]
        assert "cap_add" not in base_service
        assert "security_opt" not in base_service
        assert "SLUICERY_PRIVILEGED_MOUNT" not in base_service.get("environment", {})

    for name in ("app", "worker-network"):
        service = overlay["services"][name]
        assert set(service["cap_add"]) == {"SYS_ADMIN", "DAC_READ_SEARCH"}
        assert service["security_opt"] == ["apparmor:unconfined"]
        assert (
            service["environment"]["SLUICERY_PRIVILEGED_MOUNT"]
            == "enabled-by-compose-overlay"
        )
        mount_volume = service["volumes"][0]
        assert mount_volume["target"] == "/mnt/sluicery-mounts"
        assert mount_volume["bind"]["propagation"] == "rshared"

    assert "worker-compute" not in overlay["services"]


def test_runtime_contains_mount_helpers_and_gpu_example_stays_disabled() -> None:
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (REPO_ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "cifs-utils" in dockerfile
    assert "nfs-common" in dockerfile
    assert "# deploy:" in compose
    assert "#         - driver: nvidia" in compose
