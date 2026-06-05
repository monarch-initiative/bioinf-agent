"""
End-to-end round-trip for upload_to_scratch + fetch_from_scratch in
LOCAL mode.

The local mode (`type: local`) is the cluster-simulator pattern we
established in Phase 1: pretend a tmp_path is the cluster's scratch
root, swap shutil.copy for scp, and exercise the full primitive code
paths without touching any real network or real cluster. The bytes
flow, the sha256 round-trip runs, the permission gate fires, the
audit fields are populated — everything except scp/ssh's particular
shape, which is unit-pinned in test_scratch_command_surface.py.

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


def _setup_local_scratch(tmp_path: Path) -> Path:
    """Build the cluster-simulator: a tmp_path-rooted scratch + a project
    that grants upload/fetch on it. Returns the access_path."""
    scratch_root = tmp_path / "FAKE_CLAUDE_SCRATCH"
    scratch_root.mkdir()
    access = {
        "compute_envs": [{
            "name": "fake_cluster",
            "type": "local",
            "container_upload_target": None,
            "agent_scratch_target": {
                "path": str(scratch_root) + "/",
                "permissions": ["upload", "fetch", "exec"],
                "description": "fake cluster scratch sandbox",
            },
        }],
        "projects": [{
            "name": "e2e_proj",
            "description": "e2e fixture",
            "compute_env_access": [{
                "compute_env": "fake_cluster",
                "directories": [{
                    "path": str(scratch_root) + "/",
                    "permissions": ["file_name_only", "upload", "fetch", "exec"],
                    "description": "scratch grant",
                }],
            }],
        }],
    }
    ap = tmp_path / "projects_access.yaml"
    ap.write_text(yaml.safe_dump(access))
    return ap


@pytest.mark.integration
def test_upload_then_fetch_roundtrip_sha256_matches(tmp_path):
    """The full contract: upload N bytes; fetch them; sha256 matches end
    to end. The reported sha256 + bytes are stable; the audit fields are
    populated."""
    access_path = _setup_local_scratch(tmp_path)

    # Source payload (a few KB of pseudo-random data) — not tiny so we
    # exercise the hashing chunk loop a couple of iterations.
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
    assert up["remote_path"].endswith("/runs/2026-06-04/source.bin")
    assert Path(up["remote_path"]).read_bytes() == payload
    # Audit timestamps are populated.
    assert "transferred_at" in up and "T" in up["transferred_at"]
    assert isinstance(up["duration_s"], float)

    # 2. Fetch back to a fresh local path.
    fetched = tmp_path / "fetched.bin"
    fe = scratch.fetch_from_scratch(
        project_name="e2e_proj",
        compute_env_name="fake_cluster",
        remote_subpath="runs/2026-06-04/source.bin",
        local_path=str(fetched),
        access_path=str(access_path),
    )
    assert fe.get("success") is True, fe
    assert fe["sha256"] == expected
    assert fe["bytes"] == len(payload)
    assert fe["local_path"] == str(fetched)
    assert "fetched_at" in fe
    assert fetched.read_bytes() == payload


@pytest.mark.integration
def test_upload_creates_remote_parent_dirs(tmp_path):
    """`remote_subpath` is `a/b/c/d/e.txt` — none of the intermediate dirs
    exist; the primitive must mkdir -p them (local-mode: pathlib's mkdir
    parents=True path)."""
    access_path = _setup_local_scratch(tmp_path)
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
    # Parents exist; the leaf is the file.
    assert p.parent.is_dir()


@pytest.mark.integration
def test_fetch_refuses_overwrite(tmp_path):
    """The fetch contract: never overwrite an existing local file. A second
    fetch to the same local_path is rejected with a clear error."""
    access_path = _setup_local_scratch(tmp_path)
    src = tmp_path / "src.txt"
    src.write_bytes(b"first")
    scratch.upload_to_scratch(
        project_name="e2e_proj", compute_env_name="fake_cluster",
        local_path=str(src), remote_subpath="x.txt",
        access_path=str(access_path))

    dst = tmp_path / "dst.txt"
    ok = scratch.fetch_from_scratch(
        project_name="e2e_proj", compute_env_name="fake_cluster",
        remote_subpath="x.txt", local_path=str(dst),
        access_path=str(access_path))
    assert ok.get("success") is True

    # Second fetch to same dst should refuse.
    bad = scratch.fetch_from_scratch(
        project_name="e2e_proj", compute_env_name="fake_cluster",
        remote_subpath="x.txt", local_path=str(dst),
        access_path=str(access_path))
    assert "error" in bad and "already exists" in bad["error"]


@pytest.mark.integration
def test_fetch_refuses_remote_symlink(tmp_path):
    """The fetch contract's defense-in-depth: if the remote path resolves
    to a symlink, refuse. (Local-mode pretends the scratch dir is the
    cluster; an attacker who somehow got write into scratch could plant a
    symlink to /etc/passwd.) The primitive must NOT follow it."""
    access_path = _setup_local_scratch(tmp_path)
    # Plant a symlink directly inside the simulator's scratch root.
    scratch_root = tmp_path / "FAKE_CLAUDE_SCRATCH"
    secret = tmp_path / "secret.txt"
    secret.write_text("should not be readable")
    link = scratch_root / "innocent.txt"
    link.symlink_to(secret)

    dst = tmp_path / "fetched.txt"
    result = scratch.fetch_from_scratch(
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
    refuse to declare success. Simulate by monkeypatching the local-mode
    `shutil.copy` to write a different payload — the remote sha256 won't
    match the local sha256 we computed before transfer."""
    import shutil as _shutil
    access_path = _setup_local_scratch(tmp_path)
    src = tmp_path / "x.txt"
    src.write_bytes(b"intended payload")

    real_copy = _shutil.copy

    def bad_copy(s, d):
        # Write CORRUPTED bytes to dest while pretending success.
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
def test_upload_then_fetch_under_tree_structure_works(tmp_path):
    """Multi-file upload/fetch into nested subdirs verifies the primitives
    compose — no global state, no leak between calls."""
    access_path = _setup_local_scratch(tmp_path)

    # Stage 3 files at different depths.
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

    # Fetch every file back to a fresh local dir.
    fetched_root = tmp_path / "fetched"
    fetched_root.mkdir()
    for sub, payload in files.items():
        out = fetched_root / sub.replace("/", "_")
        r = scratch.fetch_from_scratch(
            project_name="e2e_proj", compute_env_name="fake_cluster",
            remote_subpath=sub, local_path=str(out),
            access_path=str(access_path))
        assert r.get("success") is True, (sub, r)
        assert out.read_bytes() == payload
        assert r["sha256"] == _sha256(payload)
