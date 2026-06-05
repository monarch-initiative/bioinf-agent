"""
End-to-end round-trip for upload_to_project_path + download_from_project_path
in LOCAL mode.

Mirrors test_scratch_roundtrip_e2e.py and test_common_data_roundtrip_e2e.py.
Exercises the THIRD auth family: Phase-1 explicit directories[] grant +
literal abs_path (no auto-prefix). The bytes flow, the permission gate
fires, the overwrite policy holds.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from agent.skills import project_path


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _setup_local_project(tmp_path: Path) -> tuple[Path, Path]:
    """Build the simulator: a tmp_path-rooted project workspace +
    a project that grants upload+download on it. Returns (access_path,
    workspace)."""
    workspace = tmp_path / "FAKE_PROJECT_WORKSPACE"
    workspace.mkdir()
    access = {
        "compute_envs": [{
            "name": "fake_cluster",
            "type": "local",
            "container_upload_target": None,
        }],
        "projects": [{
            "name": "e2e_proj",
            "description": "e2e fixture",
            "compute_env_access": [{
                "compute_env": "fake_cluster",
                "directories": [{
                    "path": str(workspace) + "/",
                    "permissions": ["file_name_only", "upload", "download"],
                    "description": "project workspace",
                }],
            }],
        }],
    }
    ap = tmp_path / "projects_access.yaml"
    ap.write_text(yaml.safe_dump(access))
    return ap, workspace


@pytest.mark.integration
def test_upload_then_download_roundtrip(tmp_path):
    """upload N bytes → download them → sha256 holds end-to-end. The
    resolved path is the LITERAL abs_path the agent supplied (no
    auto-prefix anywhere)."""
    access_path, workspace = _setup_local_project(tmp_path)

    payload = b"".join(bytes([i & 0xff]) for i in range(2048))
    expected = _sha256(payload)
    src = tmp_path / "src.bin"
    src.write_bytes(payload)

    abs_dst = str(workspace / "runs" / "2026-06-05" / "output.vcf")

    up = project_path.upload_to_project_path(
        project_name="e2e_proj",
        compute_env_name="fake_cluster",
        abs_path=abs_dst,
        local_path=str(src),
        access_path=str(access_path),
    )
    assert up.get("success") is True, up
    assert up["sha256"] == expected
    # The resolved remote_path is the LITERAL abs_path the agent supplied.
    assert up["remote_path"] == abs_dst
    assert Path(abs_dst).read_bytes() == payload

    fetched = tmp_path / "fetched.vcf"
    de = project_path.download_from_project_path(
        project_name="e2e_proj",
        compute_env_name="fake_cluster",
        abs_path=abs_dst,
        local_path=str(fetched),
        access_path=str(access_path),
    )
    assert de.get("success") is True, de
    assert de["sha256"] == expected
    assert fetched.read_bytes() == payload


@pytest.mark.integration
def test_upload_creates_missing_parent_dirs(tmp_path):
    """abs_path points deep; parents don't exist; primitive mkdir -p's them."""
    access_path, workspace = _setup_local_project(tmp_path)
    src = tmp_path / "x.txt"; src.write_bytes(b"deep")
    abs_dst = str(workspace / "a" / "b" / "c" / "d" / "e.txt")

    up = project_path.upload_to_project_path(
        project_name="e2e_proj", compute_env_name="fake_cluster",
        abs_path=abs_dst, local_path=str(src),
        access_path=str(access_path))
    assert up.get("success") is True, up
    assert Path(abs_dst).read_bytes() == b"deep"


@pytest.mark.integration
def test_upload_refuses_overwrite(tmp_path):
    """Same uniform overwrite refusal as scratch/common_data."""
    access_path, workspace = _setup_local_project(tmp_path)
    abs_dst = str(workspace / "output.txt")
    src1 = tmp_path / "v1.txt"; src1.write_text("v1")
    src2 = tmp_path / "v2.txt"; src2.write_text("v2")

    r1 = project_path.upload_to_project_path(
        project_name="e2e_proj", compute_env_name="fake_cluster",
        abs_path=abs_dst, local_path=str(src1),
        access_path=str(access_path))
    assert r1["success"] is True

    r2 = project_path.upload_to_project_path(
        project_name="e2e_proj", compute_env_name="fake_cluster",
        abs_path=abs_dst, local_path=str(src2),
        access_path=str(access_path))
    assert "error" in r2
    assert "already exists" in r2["error"]
    # First-upload bytes preserved.
    assert Path(abs_dst).read_text() == "v1"


@pytest.mark.integration
def test_download_refuses_overwriting_local(tmp_path):
    access_path, workspace = _setup_local_project(tmp_path)
    abs_dst = str(workspace / "x.txt")
    Path(abs_dst).write_text("server side")

    local_dst = tmp_path / "exists.txt"
    local_dst.write_text("preexisting client side")

    r = project_path.download_from_project_path(
        project_name="e2e_proj", compute_env_name="fake_cluster",
        abs_path=abs_dst, local_path=str(local_dst),
        access_path=str(access_path))
    assert "error" in r
    assert "already exists" in r["error"]
    assert local_dst.read_text() == "preexisting client side"


@pytest.mark.integration
def test_download_refuses_remote_symlink(tmp_path):
    access_path, workspace = _setup_local_project(tmp_path)
    secret = tmp_path / "secret"
    secret.write_text("hush")
    link = workspace / "innocent.txt"
    link.symlink_to(secret)

    dst = tmp_path / "fetched.txt"
    r = project_path.download_from_project_path(
        project_name="e2e_proj", compute_env_name="fake_cluster",
        abs_path=str(link), local_path=str(dst),
        access_path=str(access_path))
    assert "error" in r
    assert "symlink" in r["error"].lower()
    assert not dst.exists()
