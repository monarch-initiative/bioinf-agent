"""
Snapshot the on-disk file structure of a project's authorized directories.

Constrained operation contract
------------------------------
The agent's ONLY shell invocation through this module is `find` with a
fixed `-printf` template. The path is the only variable; it is `shlex.quote`'d
before remote (SSH) invocation and passed as a single subprocess argv element
for local invocation (no shell interpretation).

Before any subprocess runs, every path is verified against the compute_env's
authorized directories via `compute_access.check_permission(path, 'snapshot')`
which requires permission `file_name_only`. A path not in the manifest, or in
the manifest with the wrong permission, raises `PermissionDenied` — fail-
closed, no shell, no leak.

This module exposes a single function: `snapshot_project(project_name)`. There
is no API for arbitrary path snapshots or arbitrary commands. Adding either
would require adding a new operation in compute_access.OPERATION_REQUIRES,
a new gate call, and a new cheat-guard test under
tests/integration/honesty/L14_compute_env_safety/.
"""
from __future__ import annotations

import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.skills import compute_access


# The single shell shape this module emits. Pinned by a test.
# %p path, %s size in bytes, %T@ mtime (unix epoch float), %y type (f|d|l).
_FIND_PRINTF = r"%p\t%s\t%T@\t%y\n"


def _local_walk(path: str) -> list[dict]:
    """The local-mode walk. Uses pathlib directly — ZERO subprocess. This
    eliminates the shell surface entirely for local snapshots; the only place
    a shell can possibly be invoked is the ssh path below.

    GNU find's `-printf` isn't portable to BSD find (macOS), and we don't want
    to depend on `gfind` being installed. The local walk is a few `stat()`
    calls in pure Python — bounded, cancelable, and platform-independent.
    Same return shape as the SSH/find parser produces, so downstream code
    treats both modes identically."""
    root = Path(path)
    if not root.exists():
        return []
    out: list[dict] = []
    # Include the root itself plus every descendant.
    for p in [root, *root.rglob("*")]:
        try:
            st = p.stat()
        except (OSError, FileNotFoundError):
            continue
        if p.is_file():
            t = "file"
        elif p.is_dir():
            t = "dir"
        elif p.is_symlink():
            t = "link"
        else:
            t = "?"
        out.append({"path": str(p), "size": st.st_size,
                    "mtime": float(st.st_mtime), "type": t})
    return out


def _ssh_remote_cmd(path: str) -> str:
    """The remote-mode command string passed as `ssh user@host '<this>'`.
    Path is the ONLY interpolated piece and is `shlex.quote`'d. Returns the
    literal string that the remote shell will execute. Pinned by a test
    asserting this exact shape."""
    q = shlex.quote(path)
    return (f"find {q} '(' -type f -o -type d ')' "
            f"-printf {shlex.quote(_FIND_PRINTF)}")


def _ssh_argv(env: dict, remote_cmd: str) -> list[str]:
    """Build the ssh argv. user@host comes from the validated compute_env;
    remote_cmd is the pre-built (and quoted) find invocation."""
    host = env["host"]
    user = env.get("user")
    target = f"{user}@{host}" if user else host
    # `-o BatchMode=yes` so a missing ssh-agent fails fast instead of
    # interactively prompting for a password (which would deadlock the agent).
    return ["ssh", "-o", "BatchMode=yes", target, remote_cmd]


def snapshot_project(project_name: str,
                     *, access_path: Optional[str] = None,
                     timeout: int = 120) -> dict:
    """Return a directory snapshot for `project_name`'s configured paths.

    Returns:
      {
        "project":        <name>,
        "compute_env":    <env_name>,
        "snapshot_paths": [<authorized paths walked>],
        "captured_at":    <iso utc>,
        "entries":        [{path, size, mtime, type}, ...],
        "entry_count":    <int>,
      }

    Raises:
      FileNotFoundError    — projects_access.yaml missing
      KeyError             — project / compute_env not found
      PermissionDenied     — a snapshot_path isn't authorized (or wrong permission)

    Returns {"error": <str>} for:
      - empty snapshot_paths
      - find/ssh non-zero exit
      - unsupported compute_env type

    The shell that runs is fixed by `_local_argv` / `_ssh_remote_cmd`; tests
    pin both shapes.
    """
    access = compute_access.load_access(Path(access_path) if access_path else None)
    proj = compute_access.get_project(project_name, access)
    env_name = proj.get("compute_env")
    if not env_name:
        return {"error": f"project '{project_name}' has no compute_env reference"}
    env = compute_access.get_compute_env(env_name, access)

    snapshot_paths = proj.get("snapshot_paths") or []
    if not snapshot_paths:
        return {"error": f"project '{project_name}' has no snapshot_paths declared"}

    # PERMISSION GATE — runs BEFORE any subprocess. Fail-closed.
    for p in snapshot_paths:
        compute_access.check_permission(env, p, "snapshot")  # raises on denial

    env_type = env.get("type")
    all_entries: list[dict] = []
    for p in snapshot_paths:
        if env_type == "local":
            # Zero subprocess — pure Python walk. The smallest possible
            # security surface for the local case.
            all_entries.extend(_local_walk(p))
            continue
        if env_type == "ssh":
            res = subprocess.run(_ssh_argv(env, _ssh_remote_cmd(p)),
                                 capture_output=True, text=True, timeout=timeout)
        else:
            return {"error": f"unsupported compute_env type: {env_type!r}"}

        if res.returncode != 0:
            return {"error": f"find failed for {p!r} (rc={res.returncode})",
                    "stderr": (res.stderr or "")[-500:]}

        for line in res.stdout.splitlines():
            try:
                path, size, mtime, type_char = line.split("\t")
                all_entries.append({
                    "path":  path,
                    "size":  int(size),
                    "mtime": float(mtime),
                    "type":  {"f": "file", "d": "dir", "l": "link"}.get(type_char, "?"),
                })
            except ValueError:
                # A stray line (e.g. an ssh banner that snuck through) is
                # dropped silently. The captured returncode would have caught
                # a real find failure already.
                continue

    return {
        "project":        project_name,
        "compute_env":    env_name,
        "snapshot_paths": list(snapshot_paths),
        "captured_at":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entries":        all_entries,
        "entry_count":    len(all_entries),
    }
