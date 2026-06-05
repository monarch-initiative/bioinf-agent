"""
L14 cheat-guards — scratch primitives' refuse-to-emit surface.

The agent's Phase 2 sandbox surface is upload_to_scratch +
fetch_from_scratch. These tests pin every refusal path:

  - pure-string validation of remote_subpath (BEFORE any I/O)
    - absolute path leak ("/etc/passwd")
    - traversal (`..`, `a/../b`)
    - empty / over-length
    - shell metacharacters (newline = SBATCH injection; ;, |, $, etc.)
    - whitespace (would break scp argv assembly)
    - null byte
  - local_path validation (BEFORE any subprocess)
    - missing file refused
    - directory / device refused (must be a regular file)
    - symlink refused (defense vs. user's homedir redirect to /etc/shadow)
    - >5GiB refused (head-node transfer cap)
    - fetch destination already exists (no silent overwrite)
    - fetch destination parent missing (no auto-mkdir on host)
  - permission gate fires BEFORE subprocess
    - unauthorized project
    - unknown compute_env
    - env without agent_scratch_target
    - project's dir grants `upload` but op is `fetch` (discrete capabilities)
    - project's dir doesn't include the scratch path at all
  - command-shape pinning
    - _scp_argv shape (BatchMode=yes, -p, no other surface)
    - _remote_sha256_cmd shape (sha256sum + shlex.quote)
    - _build_remote_target shape (user@host:path)
  - resolve-defense
    - _resolve_remote_path refuses to produce an out-of-root path even
      if called with a (synthetic) bad subpath that bypassed validation

Pattern mirrors test_snapshot_command_surface.py and
test_phase2_schema_load.py.
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


def _local_env_with_scratch(scratch_root: Path) -> dict:
    return {
        "name": "laptop",
        "type": "local",
        "container_upload_target": None,
        "agent_scratch_target": {
            "path": str(scratch_root) + "/",
            "permissions": ["upload", "fetch", "exec"],
            "description": "test scratch",
        },
    }


def _project_grant(env_name: str, scratch_root: Path, perms=None) -> dict:
    return {
        "name": "myproj",
        "description": "test",
        "compute_env_access": [{
            "compute_env": env_name,
            "directories": [{
                "path": str(scratch_root) + "/",
                "permissions": perms if perms is not None
                               else ["file_name_only", "upload", "fetch", "exec"],
                "description": "scratch grant",
            }],
        }],
    }


@pytest.fixture
def _local_scratch(tmp_path):
    """A complete local-mode setup with scratch wired end-to-end.
    Returns (access_path, scratch_root)."""
    scratch_root = tmp_path / "CLAUDE_SCRATCH"
    scratch_root.mkdir()
    access = {
        "compute_envs": [_local_env_with_scratch(scratch_root)],
        "projects": [_project_grant("laptop", scratch_root)],
    }
    return _write_access(tmp_path, access), scratch_root


# ===========================================================================
# 1. Pure-string remote_subpath validation (no I/O — must fire before any
#    subprocess; the L14 cheat-guard pattern)
# ===========================================================================

class TestRemoteSubpathValidator:
    @pytest.mark.integration
    @pytest.mark.parametrize("good", [
        "out.txt", "runs/2026/r1.vcf", "deep/a/b/c/d/e.json",
        "a/./b",            # normalize collapses to "a/b"
        "single_dir/file",
        "_underscores-and-hyphens.txt",
    ])
    def test_accepts_good_subpaths(self, good):
        # Doesn't raise → fine. Returns the normalized form.
        scratch._validate_remote_subpath(good)

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "/etc/passwd",
        "/absolute/start",
        "/",
    ])
    def test_refuses_absolute_subpath(self, bad):
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_remote_subpath(bad)
        assert "RELATIVE" in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", [
        "..",
        "../escape",
        "a/../b",
        "a/b/../../c",
        "..//",
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
        # `a/./b` → `a/b`; `b//c` → `b/c`.
        assert scratch._validate_remote_subpath("a/./b") == "a/b"


# ===========================================================================
# 2. Resolve-defense: _resolve_remote_path catches bad inputs even if a
#    caller bypassed the front-door validator
# ===========================================================================

class TestResolveRemotePathDefense:
    @pytest.mark.integration
    def test_resolves_normal_subpath_inside_root(self):
        resolved = scratch._resolve_remote_path("/scratch/u/agent/", "runs/a.txt")
        assert resolved == "/scratch/u/agent/runs/a.txt"

    @pytest.mark.integration
    def test_strips_trailing_slash_on_root(self):
        a = scratch._resolve_remote_path("/scratch/u/agent/", "f.txt")
        b = scratch._resolve_remote_path("/scratch/u/agent",  "f.txt")
        assert a == b == "/scratch/u/agent/f.txt"

    @pytest.mark.integration
    def test_refuses_synthetic_traversal_bypass(self):
        # The front-door validator already refuses this; the resolve check
        # is the defense-in-depth — pretend we got past validation with a
        # `..`-containing normalized subpath. It should still bounce.
        with pytest.raises(ScratchPathError) as exc:
            scratch._resolve_remote_path("/scratch/u/agent/", "../etc/passwd")
        assert "escapes" in str(exc.value)


# ===========================================================================
# 3. Local-path validation for upload — must fire before any subprocess
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
        # Synthetic — actually creating a 5GB file in CI would be a waste.
        # Drop the cap to 100 bytes for this test and write 200 bytes.
        monkeypatch.setattr(scratch, "_MAX_TRANSFER_BYTES", 100)
        f = tmp_path / "big.bin"
        f.write_bytes(b"x" * 200)
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_local_path_for_upload(str(f))
        assert "transfer cap" in str(exc.value)


# ===========================================================================
# 4. Local-path validation for fetch
# ===========================================================================

class TestLocalPathFetchValidator:
    @pytest.mark.integration
    def test_accepts_nonexisting_path_with_writable_parent(self, tmp_path):
        p = scratch._validate_local_path_for_fetch(str(tmp_path / "out.txt"))
        assert p == tmp_path / "out.txt"

    @pytest.mark.integration
    def test_refuses_existing_path(self, tmp_path):
        f = tmp_path / "existing.txt"
        f.write_text("data")
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_local_path_for_fetch(str(f))
        assert "already exists" in str(exc.value)
        assert "overwrite" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_missing_parent(self, tmp_path):
        with pytest.raises(ScratchPathError) as exc:
            scratch._validate_local_path_for_fetch(
                str(tmp_path / "no_such_dir" / "out.txt"))
        assert "parent" in str(exc.value)
        assert "does not exist" in str(exc.value)


# ===========================================================================
# 5. Permission gate fires BEFORE subprocess for unauthorized configurations
# ===========================================================================

class TestUploadPermissionGate:
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
        # An env that has the project but NO agent_scratch_target.
        access = {
            "compute_envs": [{
                "name": "laptop", "type": "local",
                "container_upload_target": None,
                # NB: no agent_scratch_target
            }],
            "projects": [{
                "name": "myproj", "compute_env_access": [{
                    "compute_env": "laptop",
                    "directories": [{
                        "path": str(tmp_path) + "/",
                        "permissions": ["upload"],
                    }],
                }],
            }],
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
    def test_dir_grants_upload_but_op_is_fetch_refused(self, tmp_path, monkeypatch):
        # Build a config whose project's directories[] entry grants ONLY
        # `upload` on the scratch path. Then call fetch_from_scratch and
        # confirm it's refused — discrete capabilities, not a lattice.
        scratch_root = tmp_path / "CLAUDE_SCRATCH"
        scratch_root.mkdir()
        # Pre-stage a remote file so the early permission check is what fires.
        (scratch_root / "x.txt").write_text("hi")
        access = {
            "compute_envs": [_local_env_with_scratch(scratch_root)],
            "projects": [_project_grant("laptop", scratch_root, perms=["upload"])],
        }
        access_path = _write_access(tmp_path, access)
        local_dest = tmp_path / "fetched.txt"

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = scratch.fetch_from_scratch(
            project_name="myproj", compute_env_name="laptop",
            remote_subpath="x.txt", local_path=str(local_dest),
            access_path=str(access_path))
        assert "error" in result, result
        assert "PermissionDenied" in result["error"]
        assert "fetch" in result["error"]
        assert called == []

    @pytest.mark.integration
    def test_project_does_not_grant_scratch_path_at_all(self, tmp_path, monkeypatch):
        # Project grants a totally different directory. Resolved path is
        # inside scratch root, but no project-level entry matches → refused.
        scratch_root = tmp_path / "CLAUDE_SCRATCH"
        scratch_root.mkdir()
        access = {
            "compute_envs": [_local_env_with_scratch(scratch_root)],
            "projects": [{
                "name": "myproj", "compute_env_access": [{
                    "compute_env": "laptop",
                    "directories": [{
                        "path": "/some/elsewhere/",  # NOT scratch_root
                        "permissions": ["upload", "fetch"],
                    }],
                }],
            }],
        }
        access_path = _write_access(tmp_path, access)
        src = tmp_path / "src.txt"; src.write_text("hi")

        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())

        result = scratch.upload_to_scratch(
            project_name="myproj", compute_env_name="laptop",
            local_path=str(src), remote_subpath="x.txt",
            access_path=str(access_path))
        assert "error" in result
        assert "PermissionDenied" in result["error"]
        assert "not authorized" in result["error"]
        assert called == []


# ===========================================================================
# 6. End-to-end: rejected remote_subpaths never reach subprocess
# ===========================================================================

class TestUploadRejectionsNeverHitSubprocess:
    @pytest.mark.integration
    @pytest.mark.parametrize("bad_subpath", [
        "/etc/passwd",         # absolute leak
        "../etc/passwd",       # traversal
        "",                    # empty
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
        # All these refusals come from _validate_remote_subpath.
        assert "ScratchPathError" in result["error"]
        assert called == []


# ===========================================================================
# 7. Subprocess command-shape pinning (ssh-mode shapes)
# ===========================================================================

class TestCommandShapePinning:
    @pytest.mark.integration
    def test_scp_argv_shape(self):
        # The canonical scp invocation: BatchMode=yes (no password prompt),
        # -p (preserve mtime — useful for debugging), then src + dst as
        # separate argv elements. NO additional surface.
        env = {"name": "x", "type": "ssh", "host": "h.example", "user": "u"}
        argv = scratch._scp_argv(env, "/local/a", "u@h.example:/remote/b")
        assert argv == ["scp", "-o", "BatchMode=yes", "-p",
                        "/local/a", "u@h.example:/remote/b"]

    @pytest.mark.integration
    def test_remote_sha256_cmd_shape(self):
        # The remote shell string for sha256sum. Path is the ONLY interpolated
        # piece and is shlex.quote'd. A test pins the exact shape.
        cmd = scratch._remote_sha256_cmd("/scratch/u/agent/x.txt")
        # No metacharacters in the path → shlex.quote returns it AS-IS.
        assert cmd == "sha256sum /scratch/u/agent/x.txt"

    @pytest.mark.integration
    @pytest.mark.parametrize("evil_path", [
        "/scratch/x; rm -rf /",
        "/scratch/x && cat /etc/shadow",
        "/scratch/x`whoami`",
        "/scratch/x$(id)",
    ])
    def test_remote_sha256_cmd_neutralizes_path_injection(self, evil_path):
        # Even if a future caller foolishly bypassed the path validator,
        # shlex.quote MUST keep the path inside a single-quoted shell arg.
        cmd = scratch._remote_sha256_cmd(evil_path)
        quoted = shlex.quote(evil_path)
        assert quoted in cmd, f"path not properly quoted in: {cmd!r}"
        # Round-trip: shlex.split should recover the path as ONE argv element.
        parsed = shlex.split(cmd)
        assert evil_path in parsed
        # The other argv element is the literal sha256sum tool name only.
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
        # The path validator already strips these, but the defensive assertion
        # at the build site catches a future regression.
        with pytest.raises(ScratchPathError):
            scratch._build_remote_target(env, "/work/with space.txt")


# ===========================================================================
# 8. Hashing — sha256 of empty + known content
# ===========================================================================

class TestSha256:
    @pytest.mark.integration
    def test_empty_file_sha256(self, tmp_path):
        f = tmp_path / "empty"
        f.write_bytes(b"")
        h = scratch._compute_local_sha256(f)
        # The well-known SHA256 of the empty string.
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
