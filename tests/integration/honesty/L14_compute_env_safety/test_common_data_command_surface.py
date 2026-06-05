"""
L14 cheat-guards — common_data primitives' refuse-to-emit surface.

Sibling to test_scratch_command_surface.py. The common_data primitives
share most of their refusal paths with scratch (validators, sha256,
ssh argv shapes are imported directly), so this file focuses on what
DIFFERS:

  - Resolver: NO project auto-prefix (shared namespace). Path is
    `<common_data.path>/<remote_subpath>` directly.
  - Auth gate: looks at `agent_common_data_target` (not scratch).
    Different env-target block ⇒ different capability declaration.
  - Overwrite refusal: explicit test that uploading to an existing
    remote path returns an error rather than silently clobbering.
  - download from common_data follows the same shared-namespace shape.

The shared validators (`_validate_remote_subpath`, `_validate_project_name_token`,
`_validate_local_path_for_upload`, etc.) are exercised by the
test_scratch_command_surface.py suite — re-testing them here would be
duplication. We test them once at their canonical source.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agent.skills import common_data, compute_access
from agent.skills.compute_access import PermissionDenied
from agent.skills.scratch import ScratchPathError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_access(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _local_env_with_common_data(common_root: Path,
                                permissions=("upload", "download", "exec"),
                                name: str = "laptop") -> dict:
    return {
        "name": name,
        "type": "local",
        "container_upload_target": None,
        "agent_common_data_target": {
            "path": str(common_root) + "/",
            "permissions": list(permissions),
            "description": "test common_data",
        },
    }


def _project(name: str = "myproj", env_name: str = "laptop") -> dict:
    return {
        "name": name,
        "description": "test",
        "compute_env_access": [{
            "compute_env": env_name,
            "directories": [],
        }],
    }


@pytest.fixture
def _local_common(tmp_path):
    common_root = tmp_path / "CLAUDE_GENOMES"
    common_root.mkdir()
    access = {
        "compute_envs": [_local_env_with_common_data(common_root)],
        "projects": [_project()],
    }
    return _write_access(tmp_path, access), common_root


# ===========================================================================
# 1. Resolver: NO project auto-prefix (the key divergence from scratch)
# ===========================================================================

class TestResolveCommonDataPath:
    @pytest.mark.integration
    def test_resolves_without_project_prefix(self):
        # The whole point of common_data: shared namespace, no per-project
        # prefix injected. The path is exactly `<root>/<remote_subpath>`.
        resolved = common_data._resolve_common_data_path(
            "/work/u/CLAUDE_GENOMES/", "exomiser/v3.2.0/data.zip")
        assert resolved == "/work/u/CLAUDE_GENOMES/exomiser/v3.2.0/data.zip"

    @pytest.mark.integration
    def test_strips_trailing_slash_on_root(self):
        a = common_data._resolve_common_data_path("/x/", "f.txt")
        b = common_data._resolve_common_data_path("/x",  "f.txt")
        assert a == b == "/x/f.txt"

    @pytest.mark.integration
    def test_refuses_synthetic_traversal_bypass(self):
        with pytest.raises(ScratchPathError) as exc:
            common_data._resolve_common_data_path("/x/", "../etc/passwd")
        assert "escapes" in str(exc.value)

    @pytest.mark.integration
    def test_two_projects_resolve_to_SAME_path_for_same_subpath(self):
        # Symmetric with scratch's two-project-disjoint test — common_data
        # MUST give the same path for the same subpath regardless of which
        # project asked. The shared-namespace contract.
        # (We don't pass project_name to _resolve_common_data_path at all;
        # this is a structural property of the resolver.)
        a = common_data._resolve_common_data_path(
            "/work/u/COMMON/", "refseq/GRCh38/genome.fa")
        b = common_data._resolve_common_data_path(
            "/work/u/COMMON/", "refseq/GRCh38/genome.fa")
        assert a == b


# ===========================================================================
# 2. Env-implicit permission gate (DIFFERENT target block)
# ===========================================================================

class TestEnvImplicitPermissionGate:
    @pytest.mark.integration
    def test_unauthorized_project_refused_no_subprocess(
            self, _local_common, monkeypatch):
        access_path, common_root = _local_common
        src = common_root.parent / "src.txt"
        src.write_text("payload")

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = common_data.upload_to_common_data(
            project_name="not_a_project",
            compute_env_name="laptop",
            local_path=str(src),
            remote_subpath="x.txt",
            access_path=str(access_path),
        )
        assert "error" in result and "KeyError" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_env_without_common_data_target_refused_no_subprocess(
            self, tmp_path, monkeypatch):
        # The env has NO agent_common_data_target. Refusal is a clean
        # error pointing to the missing block.
        access = {
            "compute_envs": [{
                "name": "laptop", "type": "local",
                "container_upload_target": None,
                # NB: no agent_common_data_target
            }],
            "projects": [_project()],
        }
        access_path = _write_access(tmp_path, access)
        src = tmp_path / "src.txt"; src.write_text("x")

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = common_data.upload_to_common_data(
            project_name="myproj", compute_env_name="laptop",
            local_path=str(src), remote_subpath="x.txt",
            access_path=str(access_path))
        assert "error" in result and "agent_common_data_target" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_target_missing_upload_permission_refused(self, tmp_path):
        # Target advertises only [download, exec] — NOT upload.
        common_root = tmp_path / "CLAUDE_GENOMES"
        common_root.mkdir()
        env = {
            "name": "laptop",
            "agent_common_data_target": {
                "path": str(common_root) + "/",
                "permissions": ["download", "exec"],
            },
        }
        project = _project()
        with pytest.raises(PermissionDenied) as exc:
            compute_access.check_env_target_capability(
                project, "laptop", env["agent_common_data_target"],
                "upload_to_common_data", "agent_common_data_target")
        assert "does not include 'upload'" in str(exc.value)
        # Surfaces the right target kind in the error.
        assert "agent_common_data_target" in str(exc.value)

    @pytest.mark.integration
    def test_target_missing_download_permission_refused(self, tmp_path):
        common_root = tmp_path / "CLAUDE_GENOMES"
        common_root.mkdir()
        env = {
            "name": "laptop",
            "agent_common_data_target": {
                "path": str(common_root) + "/",
                "permissions": ["upload", "exec"],
            },
        }
        project = _project()
        with pytest.raises(PermissionDenied) as exc:
            compute_access.check_env_target_capability(
                project, "laptop", env["agent_common_data_target"],
                "download_from_common_data", "agent_common_data_target")
        assert "does not include 'download'" in str(exc.value)


# ===========================================================================
# 3. Overwrite refusal — common_data version (also backfill tested for
#    scratch in test_scratch_command_surface.py)
# ===========================================================================

class TestOverwriteRefusal:
    @pytest.mark.integration
    def test_upload_refuses_overwrite_local_mode(self, _local_common):
        access_path, common_root = _local_common
        # Pre-stage the destination so the second upload sees it exists.
        (common_root / "exists.txt").write_text("preexisting")
        src = common_root.parent / "src.txt"; src.write_text("new payload")

        result = common_data.upload_to_common_data(
            project_name="myproj", compute_env_name="laptop",
            local_path=str(src), remote_subpath="exists.txt",
            access_path=str(access_path))
        assert "error" in result, result
        assert "already exists" in result["error"]
        assert "refuses overwrites" in result["error"]
        # The existing content is untouched.
        assert (common_root / "exists.txt").read_text() == "preexisting"

    @pytest.mark.integration
    def test_upload_refuses_overwrite_at_nested_subpath(self, _local_common):
        access_path, common_root = _local_common
        nested = common_root / "deep" / "tree"
        nested.mkdir(parents=True)
        (nested / "thing.bin").write_bytes(b"original")
        src = common_root.parent / "src.bin"; src.write_bytes(b"replacement")

        result = common_data.upload_to_common_data(
            project_name="myproj", compute_env_name="laptop",
            local_path=str(src), remote_subpath="deep/tree/thing.bin",
            access_path=str(access_path))
        assert "error" in result, result
        assert "already exists" in result["error"]
        assert (nested / "thing.bin").read_bytes() == b"original"


# ===========================================================================
# 4. End-to-end: bad remote_subpaths never reach subprocess (the
#    inherited validator behaviour also applies to common_data)
# ===========================================================================

class TestCommonDataRejectionsNeverHitSubprocess:
    @pytest.mark.integration
    @pytest.mark.parametrize("bad_subpath", [
        "/etc/passwd",
        "../etc/passwd",
        "x" * 300,
        "ok;rm",
        "ok\nrm",
        "ok with space",
    ])
    def test_upload_rejects_bad_subpath_no_subprocess(
            self, _local_common, monkeypatch, bad_subpath):
        access_path, common_root = _local_common
        src = common_root.parent / "src.txt"; src.write_text("hi")

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = common_data.upload_to_common_data(
            project_name="myproj", compute_env_name="laptop",
            local_path=str(src), remote_subpath=bad_subpath,
            access_path=str(access_path))
        assert "error" in result, result
        assert "ScratchPathError" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_unsafe_project_name_refused_no_subprocess(
            self, _local_common, monkeypatch):
        access_path, common_root = _local_common
        src = common_root.parent / "src.txt"; src.write_text("hi")

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = common_data.upload_to_common_data(
            project_name="../etc",  # unsafe
            compute_env_name="laptop",
            local_path=str(src),
            remote_subpath="x.txt",
            access_path=str(access_path))
        assert "error" in result and "ScratchPathError" in result["error"]
        assert "safe token" in result["error"]
        assert called == []
