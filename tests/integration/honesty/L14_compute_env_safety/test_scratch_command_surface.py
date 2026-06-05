"""
L14 cheat-guards — scratch primitives' refuse-to-emit surface.

The agent's Phase 2 sandbox surface is upload_to_scratch +
download_from_scratch. These tests pin every refusal path BEFORE any
subprocess fires (the "no shell, no leak" contract).

Coverage:

  - pure-string validation of remote_subpath (no I/O)
  - pure-string validation of project_name (the auto-prefix token)
  - local_path validation (BEFORE any subprocess)
  - permission gate fires BEFORE subprocess for unauthorized
    configurations (env-implicit auth model: project on env + env-level
    target advertises capability)
  - command-shape pinning (scp argv, sha256sum, build_remote_target)
  - resolve-defense: _resolve_remote_path catches synthetic bypasses
    (project_name prefix is auto-applied; resolved path can't escape
    its own namespace)

The Phase-2 auth model (post-rename): the env-level `agent_scratch_target`
block carries the capability declaration; any project listed under
that env via `compute_env_access` inherits the target en bloc. There
is no project-level re-declaration of the scratch path in
`directories[]` (that list is for project-specific paths only).

The primitives auto-prefix every transfer with the project name —
`<scratch>/<project>/<remote_subpath>` — so two projects on the same
env cannot collide on a path.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agent.skills import compute_access, scratch
from agent.skills.compute_access import PermissionDenied
from agent.skills.scratch import ScratchPathError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_access(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _local_env_with_scratch(scratch_root: Path,
                            permissions=("upload", "download", "exec"),
                            name: str = "laptop") -> dict:
    return {
        "name": name,
        "type": "local",
        "container_upload_target": None,
        "agent_scratch_target": {
            "path": str(scratch_root) + "/",
            "permissions": list(permissions),
            "description": "test scratch",
        },
    }


def _project(name: str = "myproj", env_name: str = "laptop",
             extra_dirs: list[dict] | None = None) -> dict:
    """Build a project that uses `env_name` (env-implicit grant; no
    re-declaration of scratch). `extra_dirs` lets a test add project-
    specific dirs into directories[] (the Phase-1 surface)."""
    return {
        "name": name,
        "description": "test",
        "compute_env_access": [{
            "compute_env": env_name,
            "directories": extra_dirs or [],
        }],
    }


@pytest.fixture
def _local_scratch(tmp_path):
    """Local-mode setup with scratch wired end-to-end under the new auth
    model. Returns (access_path, scratch_root)."""
    scratch_root = tmp_path / "CLAUDE_SCRATCH"
    scratch_root.mkdir()
    access = {
        "compute_envs": [_local_env_with_scratch(scratch_root)],
        "projects": [_project()],
    }
    return _write_access(tmp_path, access), scratch_root


# ===========================================================================
# 1. Pure-string remote_subpath validation
# ===========================================================================

class TestRemoteSubpathValidator:
    @pytest.mark.integration
    @pytest.mark.parametrize("good", [
        "out.txt", "runs/2026/r1.vcf", "deep/a/b/c/d/e.json",
        "a/./b", "single_dir/file", "_underscores-and-hyphens.txt",
    ])
    def test_accepts_good_subpaths(self, good):
        scratch._validate_remote_subpath(good)

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", ["/etc/passwd", "/absolute/start", "/"])
    def test_refuses_absolute_subpath(self, bad):
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_remote_subpath(bad)
        assert "RELATIVE" in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "..", "../escape", "a/../b", "a/b/../../c", "..//",
    ])
    def test_refuses_traversal(self, bad):
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_remote_subpath(bad)
        assert "traversal" in str(exc.value) or "outside" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_empty(self):
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_remote_subpath("")
        assert "non-empty" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_over_length(self):
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_remote_subpath("x" * 256)
        assert "length" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_non_string(self):
        with pytest.raises(ScratchPathError):
            scratch._validate_remote_subpath(42)  # type: ignore[arg-type]
        with pytest.raises(ScratchPathError):
            scratch._validate_remote_subpath(None)  # type: ignore[arg-type]

    @pytest.mark.integration
    @pytest.mark.parametrize("evil_char", [
        ";", "|", "&", "$", "`", "<", ">", "(", ")", "{", "}", "[", "]",
        "*", "?", '"', "'", "\\",
    ])
    def test_refuses_shell_metacharacters(self, evil_char):
        bad = f"safe{evil_char}part"
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_remote_subpath(bad)
        assert "forbidden character" in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("whitespace", [" ", "\t", "\n", "\r"])
    def test_refuses_whitespace(self, whitespace):
        bad = f"a{whitespace}b"
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_remote_subpath(bad)
        assert "forbidden character" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_null_byte(self):
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_remote_subpath("safe\x00ish")
        assert "forbidden character" in str(exc.value)

    @pytest.mark.integration
    def test_normalize_collapses_redundant_dot_segments(self):
        assert scratch._validate_remote_subpath("a/./b") == "a/b"


# ===========================================================================
# 2. project_name validation — the auto-prefix safe-token check
# ===========================================================================

class TestProjectNameTokenValidator:
    @pytest.mark.integration
    @pytest.mark.parametrize("good", [
        "myproj", "hpc_cluster_test", "proj-123", "a", "x" * 64,
    ])
    def test_accepts_safe_tokens(self, good):
        assert scratch._validate_project_name_token(good) == good

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "",                  # empty
        "x" * 65,            # over-length
        "has space",
        "path/like",
        "..",                # traversal
        "with.dot",          # dots get rejected (path-traversal adjacency)
        "shell;injection",
        "newline\nattack",
        "$dollar",
    ])
    def test_refuses_unsafe_tokens(self, bad):
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_project_name_token(bad)
        assert "safe token" in str(exc.value) or "must be a string" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_non_string(self):
        with pytest.raises(ScratchPathError):
            scratch._validate_project_name_token(42)  # type: ignore[arg-type]


# ===========================================================================
# 3. Resolve-defense: _resolve_remote_path auto-prefixes by project + catches
#    bypasses even if a caller skipped the front-door validator
# ===========================================================================

class TestResolveRemotePathDefense:
    @pytest.mark.integration
    def test_resolves_normal_subpath_with_project_prefix(self):
        # The new signature: scratch_root, project_name, remote_subpath_norm.
        # Result is auto-prefixed with the project name.
        resolved = scratch._resolve_remote_path(
            "/scratch/u/agent/", "myproj", "runs/a.txt")
        assert resolved == "/scratch/u/agent/myproj/runs/a.txt"

    @pytest.mark.integration
    def test_strips_trailing_slash_on_root(self):
        a = scratch._resolve_remote_path("/scratch/u/agent/", "p", "f.txt")
        b = scratch._resolve_remote_path("/scratch/u/agent",  "p", "f.txt")
        assert a == b == "/scratch/u/agent/p/f.txt"

    @pytest.mark.integration
    def test_refuses_synthetic_traversal_bypass(self):
        # The front-door validator already refuses this; the resolve check
        # is the defense-in-depth — pretend we got past validation with a
        # `..`-containing normalized subpath. It should still bounce.
        with pytest.raises(ScratchPathError) as exc:
            scratch._resolve_remote_path(
                "/scratch/u/agent/", "p", "../etc/passwd")
        assert "escapes" in str(exc.value)

    @pytest.mark.integration
    def test_two_projects_resolve_to_disjoint_namespaces(self):
        # The whole point of the auto-prefix: two projects on the same env
        # can ask for the SAME remote_subpath without colliding.
        a = scratch._resolve_remote_path("/scratch/", "proj_a", "x.txt")
        b = scratch._resolve_remote_path("/scratch/", "proj_b", "x.txt")
        assert a == "/scratch/proj_a/x.txt"
        assert b == "/scratch/proj_b/x.txt"
        assert a != b


# ===========================================================================
# 4. Local-path validation for upload
# ===========================================================================

class TestLocalPathUploadValidator:
    @pytest.mark.integration
    def test_accepts_regular_file(self, tmp_path):
        f = tmp_path / "ok.txt"
        f.write_text("hi")
        p = scratch._validate_local_path_for_upload(str(f))
        assert p == f

    @pytest.mark.integration
    def test_refuses_missing_file(self, tmp_path):
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_local_path_for_upload(str(tmp_path / "nope.txt"))
        assert "does not exist" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_empty_string(self):
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_local_path_for_upload("")
        assert "non-empty string" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_directory(self, tmp_path):
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_local_path_for_upload(str(tmp_path))
        assert "regular file" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_symlink(self, tmp_path):
        real = tmp_path / "real.txt"
        real.write_text("real")
        link = tmp_path / "link.txt"
        link.symlink_to(real)
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_local_path_for_upload(str(link))
        assert "symlink" in str(exc.value)
        assert "redirect" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_oversize(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scratch, "_MAX_TRANSFER_BYTES", 100)
        f = tmp_path / "big.bin"
        f.write_bytes(b"x" * 200)
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_local_path_for_upload(str(f))
        assert "transfer cap" in str(exc.value)


# ===========================================================================
# 5. Local-path validation for download (renamed from fetch)
# ===========================================================================

class TestLocalPathDownloadValidator:
    @pytest.mark.integration
    def test_accepts_nonexisting_path_with_writable_parent(self, tmp_path):
        p = scratch._validate_local_path_for_download(str(tmp_path / "out.txt"))
        assert p == tmp_path / "out.txt"

    @pytest.mark.integration
    def test_refuses_existing_path(self, tmp_path):
        f = tmp_path / "existing.txt"
        f.write_text("data")
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_local_path_for_download(str(f))
        assert "already exists" in str(exc.value)
        assert "overwrite" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_missing_parent(self, tmp_path):
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_local_path_for_download(
                str(tmp_path / "no_such_dir" / "out.txt"))
        assert "parent" in str(exc.value)
        assert "does not exist" in str(exc.value)


# ===========================================================================
# 6. Env-implicit permission gate fires BEFORE subprocess for unauthorized
#    configurations
# ===========================================================================

class TestEnvImplicitPermissionGate:
    @pytest.mark.integration
    def test_unauthorized_project_refused_no_subprocess(self, _local_scratch, monkeypatch):
        access_path, scratch_root = _local_scratch
        local_src = scratch_root.parent / "src.txt"
        local_src.write_text("payload")

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = scratch.upload_to_scratch(
            project_name="not_a_project",
            compute_env_name="laptop",
            local_path=str(local_src),
            remote_subpath="x.txt",
            access_path=str(access_path),
        )
        assert "error" in result and "KeyError" in result["error"]
        assert called == [], f"subprocess invoked on rejected project: {called!r}"

    @pytest.mark.integration
    def test_unknown_compute_env_refused_no_subprocess(self, _local_scratch, monkeypatch):
        access_path, scratch_root = _local_scratch
        local_src = scratch_root.parent / "src.txt"
        local_src.write_text("payload")

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = scratch.upload_to_scratch(
            project_name="myproj",
            compute_env_name="ghost_env",
            local_path=str(local_src),
            remote_subpath="x.txt",
            access_path=str(access_path),
        )
        assert "error" in result and "KeyError" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_env_without_scratch_target_refused_no_subprocess(self, tmp_path, monkeypatch):
        access = {
            "compute_envs": [{
                "name": "laptop", "type": "local",
                "container_upload_target": None,
                # NB: no agent_scratch_target
            }],
            "projects": [_project()],
        }
        access_path = _write_access(tmp_path, access)
        src = tmp_path / "src.txt"
        src.write_text("x")

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = scratch.upload_to_scratch(
            project_name="myproj", compute_env_name="laptop",
            local_path=str(src), remote_subpath="x.txt",
            access_path=str(access_path))
        assert "error" in result and "agent_scratch_target" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_target_missing_upload_permission_refused_no_subprocess(self, tmp_path, monkeypatch):
        # Env-implicit auth: the scratch target advertises only [download, exec]
        # — NOT upload. upload_to_scratch must refuse.
        scratch_root = tmp_path / "CLAUDE_SCRATCH"
        scratch_root.mkdir()
        access = {
            "compute_envs": [_local_env_with_scratch(
                scratch_root, permissions=("download", "exec"))],
            "projects": [_project()],
        }
        # The scratch validator REQUIRES [upload, download, exec], so a
        # missing-upload config wouldn't even load. We bypass the schema by
        # constructing the env dict manually and calling check_env_target_capability
        # directly to prove the gate logic refuses.
        env = {
            "name": "laptop",
            "agent_scratch_target": {
                "path": str(scratch_root) + "/",
                "permissions": ["download", "exec"],  # no upload
            },
        }
        project = _project()
        with pytest.raises(PermissionDenied) as exc:
            compute_access.check_env_target_capability(
                project, "laptop", env["agent_scratch_target"],
                "upload_to_scratch", "agent_scratch_target")
        assert "does not include 'upload'" in str(exc.value)

    @pytest.mark.integration
    def test_target_missing_download_permission_refused_no_subprocess(self, tmp_path, monkeypatch):
        # Symmetric: an env with scratch advertising only [upload, exec] must
        # refuse a download_from_scratch.
        scratch_root = tmp_path / "CLAUDE_SCRATCH"
        scratch_root.mkdir()
        env = {
            "name": "laptop",
            "agent_scratch_target": {
                "path": str(scratch_root) + "/",
                "permissions": ["upload", "exec"],  # no download
            },
        }
        project = _project()
        with pytest.raises(PermissionDenied) as exc:
            compute_access.check_env_target_capability(
                project, "laptop", env["agent_scratch_target"],
                "download_from_scratch", "agent_scratch_target")
        assert "does not include 'download'" in str(exc.value)

    @pytest.mark.integration
    def test_project_has_no_access_to_env_refused(self, tmp_path):
        # Project has no compute_env_access entry for the env at all → gate
        # refuses with a clear "no compute_env_access entry" message.
        scratch_root = tmp_path / "CLAUDE_SCRATCH"
        scratch_root.mkdir()
        env_blk = _local_env_with_scratch(scratch_root, name="env_a")
        project = _project(env_name="env_b")  # access to env_b, not env_a
        with pytest.raises(PermissionDenied) as exc:
            compute_access.check_env_target_capability(
                project, "env_a", env_blk["agent_scratch_target"],
                "upload_to_scratch", "agent_scratch_target")
        assert "no compute_env_access entry" in str(exc.value)


# ===========================================================================
# 7. End-to-end: rejected remote_subpaths never reach subprocess
# ===========================================================================

class TestUploadRejectionsNeverHitSubprocess:
    @pytest.mark.integration
    @pytest.mark.parametrize("bad_subpath", [
        "/etc/passwd",         # absolute leak
        "../etc/passwd",       # traversal
        "x" * 300,             # over-length
        "ok;rm",               # shell metachar
        "ok\nrm",              # newline (sbatch injection)
        "ok with space",       # whitespace
    ])
    def test_upload_rejects_bad_subpath_no_subprocess(
            self, _local_scratch, monkeypatch, bad_subpath):
        access_path, scratch_root = _local_scratch
        src = scratch_root.parent / "src.txt"; src.write_text("hi")

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = scratch.upload_to_scratch(
            project_name="myproj", compute_env_name="laptop",
            local_path=str(src), remote_subpath=bad_subpath,
            access_path=str(access_path))
        assert "error" in result, result
        assert "ScratchPathError" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_unsafe_project_name_refused_no_subprocess(
            self, _local_scratch, monkeypatch):
        # Unsafe project_name is rejected even BEFORE the project lookup —
        # it never reaches load_access (we validate the token FIRST so a
        # smuggled `..` or `;` can't reach the YAML path resolver).
        access_path, scratch_root = _local_scratch
        src = scratch_root.parent / "src.txt"; src.write_text("hi")

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = scratch.upload_to_scratch(
            project_name="../etc",  # unsafe — traversal in project name
            compute_env_name="laptop",
            local_path=str(src),
            remote_subpath="x.txt",
            access_path=str(access_path))
        assert "error" in result and "ScratchPathError" in result["error"]
        assert "safe token" in result["error"]
        assert called == []


# ===========================================================================
# 7b. Overwrite refusal — the `upload` contract is "never overwrites"
# ===========================================================================

class TestScratchOverwriteRefusal:
    @pytest.mark.integration
    def test_upload_refuses_overwrite_local_mode(self, _local_scratch):
        # Two uploads to the SAME (project, remote_subpath) — second must
        # refuse. Project auto-prefix makes the resolved paths identical.
        access_path, scratch_root = _local_scratch
        src1 = scratch_root.parent / "src1.txt"; src1.write_text("first")
        src2 = scratch_root.parent / "src2.txt"; src2.write_text("second")

        r1 = scratch.upload_to_scratch(
            project_name="myproj", compute_env_name="laptop",
            local_path=str(src1), remote_subpath="output.txt",
            access_path=str(access_path))
        assert r1.get("success") is True, r1

        r2 = scratch.upload_to_scratch(
            project_name="myproj", compute_env_name="laptop",
            local_path=str(src2), remote_subpath="output.txt",
            access_path=str(access_path))
        assert "error" in r2, r2
        assert "already exists" in r2["error"]
        assert "refuses overwrites" in r2["error"]
        # Original content untouched.
        assert Path(r1["remote_path"]).read_text() == "first"


# ===========================================================================
# 8. Subprocess command-shape pinning (ssh-mode shapes — unchanged)
# ===========================================================================

class TestCommandShapePinning:
    @pytest.mark.integration
    def test_scp_argv_shape(self):
        env = {"name": "x", "type": "ssh", "host": "h.example", "user": "u"}
        argv = scratch._scp_argv(env, "/local/a", "u@h.example:/remote/b")
        assert argv == ["scp", "-o", "BatchMode=yes", "-p",
                        "/local/a", "u@h.example:/remote/b"]

    @pytest.mark.integration
    def test_remote_sha256_cmd_shape(self):
        cmd = scratch._remote_sha256_cmd("/scratch/u/agent/x.txt")
        assert cmd == "sha256sum /scratch/u/agent/x.txt"

    @pytest.mark.integration
    @pytest.mark.parametrize("evil_path", [
        "/scratch/x; rm -rf /",
        "/scratch/x && cat /etc/shadow",
        "/scratch/x`whoami`",
        "/scratch/x$(id)",
    ])
    def test_remote_sha256_cmd_neutralizes_path_injection(self, evil_path):
        cmd = scratch._remote_sha256_cmd(evil_path)
        quoted = shlex.quote(evil_path)
        assert quoted in cmd, f"path not properly quoted in: {cmd!r}"
        parsed = shlex.split(cmd)
        assert evil_path in parsed
        contaminants = [a for a in parsed if a != evil_path and a != "sha256sum"]
        assert not contaminants, \
            f"shell metachars leaked out of the path: {contaminants!r}"

    @pytest.mark.integration
    def test_build_remote_target_shape(self):
        env = {"name": "x", "type": "ssh", "host": "h.example", "user": "u"}
        assert scratch._build_remote_target(env, "/work/x.txt") == \
               "u@h.example:/work/x.txt"
        env_no_user = {"name": "x", "type": "ssh", "host": "h.example"}
        assert scratch._build_remote_target(env_no_user, "/work/x.txt") == \
               "h.example:/work/x.txt"

    @pytest.mark.integration
    def test_build_remote_target_refuses_whitespace_path(self):
        env = {"name": "x", "type": "ssh", "host": "h.example", "user": "u"}
        with pytest.raises(ScratchPathError):
            scratch._build_remote_target(env, "/work/with space.txt")


# ===========================================================================
# 9. Hashing — sha256 of empty + known content
# ===========================================================================

class TestSha256:
    @pytest.mark.integration
    def test_empty_file_sha256(self, tmp_path):
        f = tmp_path / "empty"
        f.write_bytes(b"")
        h = scratch._compute_local_sha256(f)
        assert h == "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

    @pytest.mark.integration
    def test_known_content_sha256(self, tmp_path):
        f = tmp_path / "abc"
        f.write_bytes(b"abc")
        h = scratch._compute_local_sha256(f)
        assert h == "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

    @pytest.mark.integration
    def test_parse_sha256sum_output(self):
        ok = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad  /tmp/x\n"
        assert scratch._parse_sha256sum_output(ok) == \
               "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

    @pytest.mark.integration
    def test_parse_sha256sum_output_tolerates_motd(self):
        # Some clusters' login shells emit MOTD banners. The parser skips
        # any line that doesn't start with a 64-hex digest.
        msg = (
            "###################\n"
            "# Welcome to hpc_cluster — please pirate responsibly\n"
            "###################\n"
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad  /tmp/x\n"
        )
        assert scratch._parse_sha256sum_output(msg) == \
               "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad"

    @pytest.mark.integration
    def test_parse_sha256sum_output_returns_none_on_garbage(self):
        assert scratch._parse_sha256sum_output("not a hash") is None
        assert scratch._parse_sha256sum_output("") is None
