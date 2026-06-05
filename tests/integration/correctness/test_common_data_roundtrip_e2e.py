"""
End-to-end round-trip for upload_to_common_data + download_from_common_data
in LOCAL mode.

Mirrors test_scratch_roundtrip_e2e.py, exercising the SHARED-namespace
contract: two projects on the same env can READ the same common_data
path; both see the same bytes. Uploads refuse to overwrite (reference
data is versioned, not silently replaced).
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from agent.skills import common_data


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _setup_local_common(tmp_path: Path) -> tuple[Path, Path]:
    """Build the cluster-simulator: a tmp_path-rooted common_data + two
    projects that both use the env. Returns (access_path, common_root)."""
    common_root = tmp_path / "FAKE_COMMON_DATA"
    common_root.mkdir()
    access = {
        "compute_envs": [{
            "name": "fake_cluster",
            "type": "local",
            "container_upload_target": None,
            "agent_common_data_target": {
                "path": str(common_root) + "/",
                "permissions": ["upload", "download", "exec"],
                "description": "fake cluster common-data zone",
            },
        }],
        "projects": [
            {"name": "proj_a", "compute_env_access": [
                {"compute_env": "fake_cluster", "directories": []}]},
            {"name": "proj_b", "compute_env_access": [
                {"compute_env": "fake_cluster", "directories": []}]},
        ],
    }
    ap = tmp_path / "projects_access.yaml"
    ap.write_text(yaml.safe_dump(access))
    return ap, common_root


@pytest.mark.integration
def test_upload_then_download_roundtrip(tmp_path):
    """proj_a uploads a reference DB; proj_a downloads it back. sha256
    holds end-to-end. The resolved path is SHARED (no project prefix)."""
    access_path, common_root = _setup_local_common(tmp_path)

    payload = b"".join(bytes([i & 0xff]) for i in range(4096))
    expected = _sha256(payload)
    src = tmp_path / "exomiser_v3.2.0.zip"
    src.write_bytes(payload)

    up = common_data.upload_to_common_data(
        project_name="proj_a",
        compute_env_name="fake_cluster",
        local_path=str(src),
        remote_subpath="exomiser/v3.2.0/data.zip",
        access_path=str(access_path),
    )
    assert up.get("success") is True, up
    assert up["sha256"] == expected
    # Shared namespace: NO project prefix in the resolved path.
    assert up["remote_path"].endswith("/exomiser/v3.2.0/data.zip")
    assert "/proj_a/" not in up["remote_path"], up["remote_path"]

    dst = tmp_path / "fetched.zip"
    de = common_data.download_from_common_data(
        project_name="proj_a",
        compute_env_name="fake_cluster",
        remote_subpath="exomiser/v3.2.0/data.zip",
        local_path=str(dst),
        access_path=str(access_path),
    )
    assert de.get("success") is True, de
    assert de["sha256"] == expected
    assert dst.read_bytes() == payload


@pytest.mark.integration
def test_two_projects_share_namespace(tmp_path):
    """The mix-and-match contract: proj_a uploads; proj_b downloads the
    SAME bytes from the SAME path. No isolation between projects on
    common_data — that's the whole point."""
    access_path, common_root = _setup_local_common(tmp_path)

    payload = b"shared reference content"
    src = tmp_path / "ref.bin"
    src.write_bytes(payload)

    # proj_a uploads.
    up = common_data.upload_to_common_data(
        project_name="proj_a", compute_env_name="fake_cluster",
        local_path=str(src), remote_subpath="refs/shared.bin",
        access_path=str(access_path))
    assert up["success"] is True
    proj_a_upload_path = up["remote_path"]

    # proj_b downloads — sees the SAME path proj_a used.
    dst_b = tmp_path / "fetched_by_b.bin"
    de_b = common_data.download_from_common_data(
        project_name="proj_b", compute_env_name="fake_cluster",
        remote_subpath="refs/shared.bin", local_path=str(dst_b),
        access_path=str(access_path))
    assert de_b["success"] is True
    assert de_b["remote_path"] == proj_a_upload_path
    assert dst_b.read_bytes() == payload
    assert de_b["sha256"] == _sha256(payload)


@pytest.mark.integration
def test_upload_refuses_overwrite(tmp_path):
    """Reference data is versioned, not replaced. A second upload to the
    same path is refused; the original bytes are preserved."""
    access_path, common_root = _setup_local_common(tmp_path)

    src1 = tmp_path / "v1.bin"; src1.write_bytes(b"v1 content")
    src2 = tmp_path / "v2.bin"; src2.write_bytes(b"v2 content")

    r1 = common_data.upload_to_common_data(
        project_name="proj_a", compute_env_name="fake_cluster",
        local_path=str(src1), remote_subpath="db/data.bin",
        access_path=str(access_path))
    assert r1["success"] is True

    r2 = common_data.upload_to_common_data(
        project_name="proj_a", compute_env_name="fake_cluster",
        local_path=str(src2), remote_subpath="db/data.bin",
        access_path=str(access_path))
    assert "error" in r2, r2
    assert "already exists" in r2["error"]
    # The user is told to version: pick fresh subpath suggestion in the
    # error message.
    assert "fresh remote_subpath" in r2["error"]
    assert Path(r1["remote_path"]).read_bytes() == b"v1 content"
