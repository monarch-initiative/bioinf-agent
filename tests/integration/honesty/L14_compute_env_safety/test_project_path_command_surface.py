"""
L14 cheat-guards — project_path primitives' refuse-to-emit surface.

The THIRD transfer auth family (after scratch + common_data). Different
auth model: Phase-1 explicit grant via the project's
`compute_env_access[].directories[]` list, NOT env-implicit. The path
is supplied LITERALLY as an absolute path, not as a relative
remote_subpath that gets auto-prefixed.

These tests pin the rejection paths SPECIFIC to project_path:

  - abs_path validation (must be absolute, no '..', no shell metachars)
  - check_permission gate fires BEFORE subprocess for:
      - project not in yaml
      - project has no directories[] entry covering abs_path
      - matched entry's permissions don't include `upload`/`download`
        (discrete capabilities, not a lattice)
  - upload refuses overwrites (uniform policy)
  - download refuses local-path overwrites + symlinks
  - all rejections happen BEFORE any subprocess fires

Other shared validators (sha256, scp argv, etc.) are covered by
test_scratch_command_surface.py at their canonical source.
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agent.skills import compute_access, project_path
from agent.skills.compute_access import PermissionDenied
from agent.skills.scratch import ScratchPathError


def _write_access(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


@pytest.fixture
def _project_workspace(tmp_path):
    """Local-mode setup: env has no Phase-2 target blocks; the project
    has a directories[] entry granting [upload, download] on tmp_path /
    workspace. Returns (access_path, workspace_dir)."""
    workspace = tmp_path / "PROJECT_WORKSPACE"
    workspace.mkdir()
    access = {
        "compute_envs": [{
            "name": "laptop",
            "type": "local",
            "container_upload_target": None,
        }],
        "projects": [{
            "name": "myproj",
            "description": "test",
            "compute_env_access": [{
                "compute_env": "laptop",
                "directories": [{
                    "path": str(workspace) + "/",
                    "permissions": ["file_name_only", "upload", "download"],
                    "description": "project workspace",
                }],
            }],
        }],
    }
    return _write_access(tmp_path, access), workspace


# ===========================================================================
# 1. abs_path validator (pure-string, no I/O)
# ===========================================================================

class TestAbsPathValidator:
    @pytest.mark.integration
    @pytest.mark.parametrize("good", [
        "/work/proj/output.txt",
        "/work/proj/runs/2026-06-05/output.vcf",
        "/work/proj/a/b/c/deep.json",
        "/tmp/single",
    ])
    def test_accepts_good_abs_paths(self, good):
        # Doesn't raise. Returns the normalized form.
        assert project_path._validate_abs_remote_path(good) == good

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "relative/path",
        "./relative",
        "",
        "no_leading_slash",
    ])
    def test_refuses_non_absolute(self, bad):
        with pytest.raises(ScratchPathError) as exc:
            project_path._validate_abs_remote_path(bad)
        msg = str(exc.value)
        assert "absolute" in msg or "non-empty" in msg

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "/work/../etc/passwd",
        "/..",
        "/a/b/../../c",
    ])
    def test_refuses_traversal(self, bad):
        with pytest.raises(ScratchPathError) as exc:
            project_path._validate_abs_remote_path(bad)
        assert "traversal" in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("evil_char", [
        ";", "|", "&", "$", "`", "\n", "\t",
    ])
    def test_refuses_shell_metacharacters(self, evil_char):
        bad = f"/work/safe{evil_char}part"
        with pytest.raises(ScratchPathError) as exc:
            project_path._validate_abs_remote_path(bad)
        assert "forbidden character" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_non_string(self):
        with pytest.raises(ScratchPathError):
            project_path._validate_abs_remote_path(42)  # type: ignore[arg-type]
        with pytest.raises(ScratchPathError):
            project_path._validate_abs_remote_path(None)  # type: ignore[arg-type]

    @pytest.mark.integration
    def test_allows_slashes_in_path(self):
        # `/` is REQUIRED in abs_path (paths have slashes). The validator
        # must NOT reject them as forbidden chars.
        assert project_path._validate_abs_remote_path("/a/b/c/d") == "/a/b/c/d"


# ===========================================================================
# 2. Phase-1 check_permission gate fires BEFORE subprocess
# ===========================================================================

class TestProjectPathAuthGate:
    @pytest.mark.integration
    def test_unauthorized_path_refused_no_subprocess(
            self, _project_workspace, monkeypatch):
        access_path, workspace = _project_workspace
        src = workspace.parent / "src.txt"
        src.write_text("hi")

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        # Path NOT in the project's directories[] list at all.
        result = project_path.upload_to_project_path(
            project_name="myproj",
            compute_env_name="laptop",
            abs_path="/etc/passwd",
            local_path=str(src),
            access_path=str(access_path),
        )
        assert "error" in result and "PermissionDenied" in result["error"]
        assert called == [], f"subprocess invoked on rejected path: {called!r}"

    @pytest.mark.integration
    def test_upload_perm_does_not_satisfy_download(
            self, tmp_path, monkeypatch):
        # The project's directory grants `upload` only; download_from_project_path
        # must refuse (discrete capabilities, not a lattice).
        workspace = tmp_path / "WORKSPACE"
        workspace.mkdir()
        (workspace / "x.txt").write_text("hi")
        access = {
            "compute_envs": [{
                "name": "laptop", "type": "local",
                "container_upload_target": None,
            }],
            "projects": [{
                "name": "myproj",
                "compute_env_access": [{
                    "compute_env": "laptop",
                    "directories": [{
                        "path": str(workspace) + "/",
                        "permissions": ["upload"],  # no download
                    }],
                }],
            }],
        }
        access_path = _write_access(tmp_path, access)
        dst = tmp_path / "fetched.txt"

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = project_path.download_from_project_path(
            project_name="myproj", compute_env_name="laptop",
            abs_path=str(workspace / "x.txt"), local_path=str(dst),
            access_path=str(access_path))
        assert "error" in result
        assert "PermissionDenied" in result["error"]
        assert "download" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_download_perm_does_not_satisfy_upload(
            self, tmp_path, monkeypatch):
        # The project's directory grants `download` only; upload_to_project_path
        # must refuse.
        workspace = tmp_path / "WORKSPACE"
        workspace.mkdir()
        access = {
            "compute_envs": [{
                "name": "laptop", "type": "local",
                "container_upload_target": None,
            }],
            "projects": [{
                "name": "myproj",
                "compute_env_access": [{
                    "compute_env": "laptop",
                    "directories": [{
                        "path": str(workspace) + "/",
                        "permissions": ["download"],  # no upload
                    }],
                }],
            }],
        }
        access_path = _write_access(tmp_path, access)
        src = tmp_path / "src.txt"
        src.write_text("hi")

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = project_path.upload_to_project_path(
            project_name="myproj", compute_env_name="laptop",
            abs_path=str(workspace / "out.txt"), local_path=str(src),
            access_path=str(access_path))
        assert "error" in result
        assert "PermissionDenied" in result["error"]
        assert "upload" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_unknown_project_refused_no_subprocess(
            self, _project_workspace, monkeypatch):
        access_path, workspace = _project_workspace
        src = workspace.parent / "src.txt"; src.write_text("hi")

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = project_path.upload_to_project_path(
            project_name="not_a_project",
            compute_env_name="laptop",
            abs_path=str(workspace / "x.txt"),
            local_path=str(src),
            access_path=str(access_path))
        assert "error" in result and "KeyError" in result["error"]
        assert called == []


# ===========================================================================
# 3. Overwrite refusal — uniform policy
# ===========================================================================

class TestOverwriteRefusal:
    @pytest.mark.integration
    def test_upload_refuses_overwrite(self, _project_workspace):
        access_path, workspace = _project_workspace
        # Pre-stage the destination.
        (workspace / "out.txt").write_text("original")
        src = workspace.parent / "src.txt"; src.write_text("replacement")

        result = project_path.upload_to_project_path(
            project_name="myproj", compute_env_name="laptop",
            abs_path=str(workspace / "out.txt"), local_path=str(src),
            access_path=str(access_path))
        assert "error" in result, result
        assert "already exists" in result["error"]
        assert "refuses overwrites" in result["error"]
        # Original content untouched.
        assert (workspace / "out.txt").read_text() == "original"


# ===========================================================================
# 4. End-to-end: bad abs_paths never reach subprocess
# ===========================================================================

class TestRejectionsNeverHitSubprocess:
    @pytest.mark.integration
    @pytest.mark.parametrize("bad_abs_path", [
        "relative/no/leading/slash",
        "/work/../etc/passwd",
        "/work/with;rm",
        "/work/with\nnewline",
        "",
    ])
    def test_upload_rejects_bad_path_no_subprocess(
            self, _project_workspace, monkeypatch, bad_abs_path):
        access_path, workspace = _project_workspace
        src = workspace.parent / "src.txt"; src.write_text("hi")

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = project_path.upload_to_project_path(
            project_name="myproj", compute_env_name="laptop",
            abs_path=bad_abs_path, local_path=str(src),
            access_path=str(access_path))
        assert "error" in result, result
        assert "ScratchPathError" in result["error"]
        assert called == []
