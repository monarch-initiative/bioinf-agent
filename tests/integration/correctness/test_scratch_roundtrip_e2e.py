"""
End-to-end round-trip for upload_to_scratch + download_from_scratch in
LOCAL mode.

The local mode (`type: local`) is the cluster-simulator pattern we
established in Phase 1: pretend a tmp_path is the cluster's scratch
root, swap shutil.copy for scp, and exercise the full primitive code
paths without touching any real network or real cluster. The bytes
flow, the sha256 round-trip runs, the env-implicit permission gate
fires, the auto-prefix-by-project is applied — everything except
scp/ssh's particular shape, which is unit-pinned in
test_scratch_command_surface.py.

Why this matters: ZERO ssh in tests means CI is fast and head-node-
clean. The L14 tests pin the refuse-to-emit surface; this test pins
the happy-path bytes-move-and-back contract.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
import yaml

from agent.skills import scratch


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _setup_local_scratch(tmp_path: Path,
                         project_name: str = "e2e_proj") -> tuple[Path, Path]:
    """Build the cluster-simulator: a tmp_path-rooted scratch + a project
    that uses the env (env-implicit grant; no per-project directories[]
    re-declaration of the scratch path).

    Returns (access_path, scratch_root)."""
    scratch_root = tmp_path / "FAKE_CLAUDE_SCRATCH"
    scratch_root.mkdir()
    access = {
        "compute_envs": [{
            "name": "fake_cluster",
            "type": "local",
            "container_upload_target": None,
            "agent_scratch_target": {
                "path": str(scratch_root) + "/",
                "permissions": ["upload", "download", "exec"],
                "description": "fake cluster scratch sandbox",
            },
        }],
        "projects": [{
            "name": project_name,
            "description": "e2e fixture",
            "compute_env_access": [{
                "compute_env": "fake_cluster",
                "directories": [],   # env-implicit grant; no re-declaration
            }],
        }],
    }
    ap = tmp_path / "projects_access.yaml"
    ap.write_text(yaml.safe_dump(access))
    return ap, scratch_root


@pytest.mark.integration
def test_upload_then_download_roundtrip_sha256_matches(tmp_path):
    """The full contract: upload N bytes; download them; sha256 matches end
    to end. The reported sha256 + bytes are stable; the audit fields are
    populated. The resolved remote path is auto-prefixed by the project
    name (multi-project isolation)."""
    access_path, scratch_root = _setup_local_scratch(tmp_path)

    payload = b"".join(bytes([i & 0xff]) for i in range(8192))
    expected = _sha256(payload)
    src = tmp_path / "source.bin"
    src.write_bytes(payload)

    # 1. Upload.
    up = scratch.upload_to_scratch(
        project_name="e2e_proj",
        compute_env_name="fake_cluster",
        local_path=str(src),
        remote_subpath="runs/2026-06-04/source.bin",
        access_path=str(access_path),
    )
    assert up.get("success") is True, up
    assert up["sha256"] == expected
    assert up["bytes"] == len(payload)
    assert up["compute_env"] == "fake_cluster"
    # Auto-prefix-by-project: the resolved path lands under <scratch>/e2e_proj/
    assert up["remote_path"].endswith(
        "/e2e_proj/runs/2026-06-04/source.bin"), up["remote_path"]
    assert Path(up["remote_path"]).read_bytes() == payload
    # Audit timestamps are populated.
    assert "transferred_at" in up and "T" in up["transferred_at"]
    assert isinstance(up["duration_s"], float)

    # 2. Download back to a fresh local path.
    fetched = tmp_path / "fetched.bin"
    de = scratch.download_from_scratch(
        project_name="e2e_proj",
        compute_env_name="fake_cluster",
        remote_subpath="runs/2026-06-04/source.bin",
        local_path=str(fetched),
        access_path=str(access_path),
    )
    assert de.get("success") is True, de
    assert de["sha256"] == expected
    assert de["bytes"] == len(payload)
    assert de["local_path"] == str(fetched)
    assert "fetched_at" in de
    assert fetched.read_bytes() == payload


@pytest.mark.integration
def test_upload_creates_remote_parent_dirs(tmp_path):
    """`remote_subpath` is `a/b/c/d/e.txt` — none of the intermediate dirs
    exist; the primitive must mkdir -p them (including the auto-prefix
    project component)."""
    access_path, scratch_root = _setup_local_scratch(tmp_path)
    src = tmp_path / "x.txt"
    src.write_bytes(b"deep")
    up = scratch.upload_to_scratch(
        project_name="e2e_proj",
        compute_env_name="fake_cluster",
        local_path=str(src),
        remote_subpath="a/b/c/d/e.txt",
        access_path=str(access_path),
    )
    assert up.get("success") is True, up
    p = Path(up["remote_path"])
    assert p.read_bytes() == b"deep"
    assert p.parent.is_dir()
    # Confirm the e2e_proj prefix is present.
    assert "/e2e_proj/" in str(p)


@pytest.mark.integration
def test_two_projects_isolated_by_auto_prefix(tmp_path):
    """Two projects on the same env uploading the SAME remote_subpath must
    land at disjoint absolute paths — the whole point of project-name
    auto-prefix."""
    scratch_root = tmp_path / "SHARED_SCRATCH"
    scratch_root.mkdir()
    access = {
        "compute_envs": [{
            "name": "shared_cluster", "type": "local",
            "container_upload_target": None,
            "agent_scratch_target": {
                "path": str(scratch_root) + "/",
                "permissions": ["upload", "download", "exec"],
            },
        }],
        "projects": [
            {"name": "proj_a", "compute_env_access": [
                {"compute_env": "shared_cluster", "directories": []}]},
            {"name": "proj_b", "compute_env_access": [
                {"compute_env": "shared_cluster", "directories": []}]},
        ],
    }
    ap = tmp_path / "projects_access.yaml"
    ap.write_text(yaml.safe_dump(access))

    src_a = tmp_path / "a.txt"; src_a.write_bytes(b"alpha")
    src_b = tmp_path / "b.txt"; src_b.write_bytes(b"beta")

    ra = scratch.upload_to_scratch(
        project_name="proj_a", compute_env_name="shared_cluster",
        local_path=str(src_a), remote_subpath="data.txt",
        access_path=str(ap))
    rb = scratch.upload_to_scratch(
        project_name="proj_b", compute_env_name="shared_cluster",
        local_path=str(src_b), remote_subpath="data.txt",
        access_path=str(ap))
    assert ra["success"] is True
    assert rb["success"] is True
    # Same remote_subpath; disjoint resolved paths.
    assert ra["remote_path"] != rb["remote_path"]
    assert ra["remote_path"].endswith("/proj_a/data.txt")
    assert rb["remote_path"].endswith("/proj_b/data.txt")
    # Files preserve their distinct content.
    assert Path(ra["remote_path"]).read_bytes() == b"alpha"
    assert Path(rb["remote_path"]).read_bytes() == b"beta"


@pytest.mark.integration
def test_download_refuses_overwrite(tmp_path):
    """The download contract: never overwrite an existing local file."""
    access_path, _ = _setup_local_scratch(tmp_path)
    src = tmp_path / "src.txt"
    src.write_bytes(b"first")
    scratch.upload_to_scratch(
        project_name="e2e_proj", compute_env_name="fake_cluster",
        local_path=str(src), remote_subpath="x.txt",
        access_path=str(access_path))

    dst = tmp_path / "dst.txt"
    ok = scratch.download_from_scratch(
        project_name="e2e_proj", compute_env_name="fake_cluster",
        remote_subpath="x.txt", local_path=str(dst),
        access_path=str(access_path))
    assert ok.get("success") is True

    bad = scratch.download_from_scratch(
        project_name="e2e_proj", compute_env_name="fake_cluster",
        remote_subpath="x.txt", local_path=str(dst),
        access_path=str(access_path))
    assert "error" in bad and "already exists" in bad["error"]


@pytest.mark.integration
def test_download_refuses_remote_symlink(tmp_path):
    """The download contract's defense-in-depth: if the remote path
    resolves to a symlink, refuse. The auto-prefix places the namespace
    at <scratch>/<project>/, so we plant the symlink there."""
    access_path, scratch_root = _setup_local_scratch(tmp_path)
    proj_namespace = scratch_root / "e2e_proj"
    proj_namespace.mkdir(parents=True, exist_ok=True)
    secret = tmp_path / "secret.txt"
    secret.write_text("should not be readable")
    link = proj_namespace / "innocent.txt"
    link.symlink_to(secret)

    dst = tmp_path / "fetched.txt"
    result = scratch.download_from_scratch(
        project_name="e2e_proj", compute_env_name="fake_cluster",
        remote_subpath="innocent.txt", local_path=str(dst),
        access_path=str(access_path))
    assert "error" in result, result
    assert "symlink" in result["error"].lower()
    assert not dst.exists()


@pytest.mark.integration
def test_upload_corruption_simulated_via_post_write_mutation(tmp_path, monkeypatch):
    """If the round-trip sha256 check is bypassed (or the bytes are
    somehow tampered between transfer and verify), the primitive must
    refuse to declare success."""
    access_path, _ = _setup_local_scratch(tmp_path)
    src = tmp_path / "x.txt"
    src.write_bytes(b"intended payload")

    def bad_copy(s, d):
        Path(d).write_bytes(b"wrong payload")
        return d

    monkeypatch.setattr(scratch.shutil, "copy", bad_copy)

    result = scratch.upload_to_scratch(
        project_name="e2e_proj", compute_env_name="fake_cluster",
        local_path=str(src), remote_subpath="x.txt",
        access_path=str(access_path))
    assert "error" in result, result
    assert "sha256 round-trip mismatch" in result["error"]


@pytest.mark.integration
def test_upload_then_download_under_tree_structure_works(tmp_path):
    """Multi-file upload/download into nested subdirs verifies the
    primitives compose under the auto-prefix model — no leak between
    calls."""
    access_path, _ = _setup_local_scratch(tmp_path)

    files = {
        "a.txt":           b"alpha",
        "sub/b.txt":       b"beta",
        "deeper/x/y.json": b'{"z": 1}',
    }
    for sub, payload in files.items():
        src = tmp_path / f"src_{sub.replace('/', '_')}"
        src.write_bytes(payload)
        r = scratch.upload_to_scratch(
            project_name="e2e_proj", compute_env_name="fake_cluster",
            local_path=str(src), remote_subpath=sub,
            access_path=str(access_path))
        assert r.get("success") is True, (sub, r)

    fetched_root = tmp_path / "fetched"
    fetched_root.mkdir()
    for sub, payload in files.items():
        out = fetched_root / sub.replace("/", "_")
        r = scratch.download_from_scratch(
            project_name="e2e_proj", compute_env_name="fake_cluster",
            remote_subpath=sub, local_path=str(out),
            access_path=str(access_path))
        assert r.get("success") is True, (sub, r)
        assert out.read_bytes() == payload
        assert r["sha256"] == _sha256(payload)
