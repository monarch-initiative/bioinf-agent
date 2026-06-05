"""
Snapshot the on-disk file structure of a project's authorized directories.

Constrained operation contract
------------------------------
The agent's ONLY shell invocation through this module is `find` with a
fixed `-printf` template AND `-maxdepth 1`. The path is the only variable;
it is `shlex.quote`'d before remote (SSH) invocation and passed as a single
subprocess argv element for local invocation (no shell interpretation).

One-level visibility (the `file_name_only` contract)
----------------------------------------------------
A snapshot returns the root + its IMMEDIATE children — never deeper.
Subdirectories are listed BY NAME (so the agent can see "there's a
samples/ dir") but their CONTENTS are NOT walked. To inspect a subdir,
the user must declare it as its own directory entry in the project's
compute_env_access[].directories[].

This is intentional: every directory the agent peers into is an explicit
user-declared concession. A single declaration cannot snowball into a
multi-million-file walk. The agent can't go fishing in a sub-tree just
because the parent was authorized.

Permission gate
---------------
Before any subprocess runs, every directory we plan to walk is verified
against the project's authorized directories via
`compute_access.check_permission(project, env, path, 'snapshot')` which
requires the directory's `permissions:` list to include `file_name_only`.
A path not in the manifest, or with the wrong permissions, raises
`PermissionDenied` — fail-closed, no shell, no leak.

Multi-env projects
------------------
A project may declare `compute_env_access` blocks for multiple envs. The
snapshot iterates each, walks each env's authorized directories, and
aggregates the result with `compute_env` tagged on every entry so the
downstream consumer can partition by env.

This module exposes a single function: `snapshot_project(project_name)`.
Adding a new operation (upload, download, hpc_run, …) requires a new
entry in `compute_access.OPERATION_REQUIRES`, a new gate call, and a new
cheat-guard test under tests/integration/honesty/L14_compute_env_safety/.
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
    """The local-mode walk — root + IMMEDIATE children only, no recursion.

    Mirrors the SSH mode's `find -maxdepth 1`. The one-level contract is
    deliberate (see module docstring): a single declaration buys the agent
    visibility into exactly one directory's contents, not its subtree.
    Subdirs are listed BY NAME (so the agent can SEE "there's a samples/
    dir") but their interiors require a separate declaration.

    Uses pathlib directly — ZERO subprocess. The shell surface for local
    snapshots is empty. GNU find's `-printf` isn't portable to BSD find
    (macOS), and we don't want to depend on `gfind` being installed; a
    handful of `stat()` calls is bounded, cancelable, and platform-
    independent. Same return shape as the SSH/find parser, so downstream
    code treats both modes identically."""
    root = Path(path)
    if not root.exists():
        return []
    targets: list[Path] = [root]
    if root.is_dir():
        try:
            targets.extend(root.iterdir())
        except (PermissionError, OSError):
            # Read on the directory denied (rare for an authorized path,
            # but a hostile mount could surface it). Return just the root.
            pass
    out: list[dict] = []
    for p in targets:
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
    asserting this exact shape.

    `-maxdepth 1` enforces the one-level visibility contract on the remote
    side, matching the local walk. The agent sees the root + its
    immediate children; the subtree below is invisible until the user
    declares it as a separate authorized directory."""
    q = shlex.quote(path)
    return (f"find {q} -maxdepth 1 '(' -type f -o -type d ')' "
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


def _ssh_failure_hint(stderr: str, host: str) -> Optional[str]:
    """Detect well-known ssh failure modes and return a user-facing hint.

    The most common failure in our setup is "no open ControlMaster
    connection" — the user hasn't run `ssh <host>` in their interactive
    terminal yet (or has closed it). BatchMode then tries to authenticate
    with no method available and dies with "Permission denied".

    Returning a concrete next-step message in the snapshot result means the
    user sees "run `ssh hpc-agent` to open a session" instead of an opaque
    rc=255. Returns None if no known pattern matches."""
    s = stderr or ""
    if ("Permission denied" in s
            or "Connection closed by remote host" in s
            or "no matching host key" in s
            or "Host key verification failed" in s):
        return (
            f"Looks like there's no open ssh session to '{host}'. "
            f"Open a terminal and run `ssh {host}` to authenticate "
            f"(you'll be prompted for your password by ssh itself; the "
            f"agent never sees or stores it). Leave that terminal open, "
            f"then retry the snapshot. The agent piggybacks on your open "
            f"session — closing the terminal cleanly ends everything.")
    if "Could not resolve hostname" in s or "Name or service not known" in s:
        return (
            f"ssh can't resolve hostname '{host}'. Check `~/.ssh/config` "
            f"for the Host alias, or your network connection.")
    if "Connection timed out" in s or "Network is unreachable" in s:
        return (
            f"ssh to '{host}' timed out / network unreachable. "
            f"Are you on the right VPN / network?")
    return None


def _parse_find_output(stdout: str) -> list[dict]:
    """Parse the tab-separated output of `find -printf '%p\\t%s\\t%T@\\t%y\\n'`
    into the canonical entry-dict shape. A stray line (ssh banner, etc.)
    fails parsing and is dropped silently — the captured returncode
    would have caught a real find failure already."""
    out: list[dict] = []
    for line in stdout.splitlines():
        try:
            path, size, mtime, type_char = line.split("\t")
            out.append({
                "path":  path,
                "size":  int(size),
                "mtime": float(mtime),
                "type":  {"f": "file", "d": "dir", "l": "link"}.get(type_char, "?"),
            })
        except ValueError:
            continue
    return out


def _snapshot_paths_for_env(project: dict, env_name: str,
                            env: dict) -> list[dict]:
    """The list of directories the agent will walk on `env_name` for this
    project. Each entry is a tagged dict so the caller can apply the
    right permission gate per source:

      {"path": <abs>, "kind": "project_directory"}
        — from project's compute_env_access[].directories[]; the path
          must advertise `file_name_only` in its permissions[]. The
          Phase-1 `check_permission` gate is applied.

      {"path": <abs>, "kind": "env_target",
       "target_kind": "agent_scratch_target" | "agent_common_data_target",
       "target_block": <dict>}
        — from the env-level Phase-2 target blocks. The path is the
          target's path AUTO-PREFIXED with the project name (multi-
          project isolation: this project sees only its own namespace).
          The env-implicit `check_env_target_capability` gate is applied.

    Upload-only dirs (no `file_name_only`) are authorized for upload
    but invisible to the snapshot — they don't appear here."""
    out: list[dict] = []
    # Source 1: project's compute_env_access[].directories[] (Phase 1)
    for d in compute_access.get_project_directories(project, env_name):
        if "file_name_only" in (d.get("permissions") or []):
            p = d.get("path")
            if isinstance(p, str):
                out.append({"path": p, "kind": "project_directory"})
    # Source 2: env-level Phase-2 target blocks — visible if the target's
    # permissions include `file_name_only`. The walked path is auto-
    # prefixed by project name so projects don't see each other.
    proj_name = project.get("name", "")
    for target_kind, getter in (
            ("agent_scratch_target", compute_access.get_agent_scratch_target),
            ("agent_common_data_target", compute_access.get_agent_common_data_target)):
        blk = getter(env)
        if blk is None:
            continue
        if "file_name_only" not in (blk.get("permissions") or []):
            continue
        root = (blk.get("path") or "").rstrip("/")
        if not root:
            continue
        out.append({
            "path": f"{root}/{proj_name}",
            "kind": "env_target",
            "target_kind": target_kind,
            "target_block": blk,
        })
    return out


def snapshot_project(project_name: str,
                     *, access_path: Optional[str] = None,
                     timeout: int = 120) -> dict:
    """Return a directory snapshot for `project_name` across every compute
    env it touches.

    Walks every dir-access block in the project whose `permissions:`
    include `file_name_only`, on every env declared in the project's
    `compute_env_access[]`. Each entry in the result is tagged with the
    `compute_env` it came from so downstream consumers can partition.

    Returns:
      {
        "project":        <name>,
        "compute_envs":   [<env_name>, ...],   # envs this project spans
        "captured_at":    <iso utc>,
        "entries":        [{compute_env, path, size, mtime, type}, ...],
        "entry_count":    <int>,
        "per_env_counts": {<env_name>: <int>, ...},
      }

    Raises:
      FileNotFoundError    — projects_access.yaml missing
      KeyError             — project / compute_env not found
      PermissionDenied     — a directory isn't authorized for `snapshot`

    Returns {"error": <str>} for:
      - project has no compute_env_access blocks
      - no directory grants `file_name_only` (so snapshot is a no-op)
      - find/ssh non-zero exit (per-env, reported in `errors`)
      - unsupported compute_env type
    """
    access = compute_access.load_access(Path(access_path) if access_path else None)
    project = compute_access.get_project(project_name, access)

    access_blocks = project.get("compute_env_access") or []
    if not access_blocks:
        return {"error": f"project '{project_name}' has no compute_env_access blocks"}

    # First pass: collect (env_name, env_dict, tagged_paths). Gate every
    # path BEFORE any subprocess runs — defense-in-depth, even though every
    # walked path is by construction in the project's authorized dir set.
    # Tagged paths carry their source so the right gate fires:
    #   project_directory → check_permission (Phase 1, project-level grant)
    #   env_target        → check_env_target_capability (Phase 2, env grant)
    plans: list[tuple[str, dict, list[dict]]] = []
    for block in access_blocks:
        env_name = block.get("compute_env")
        env = compute_access.get_compute_env(env_name, access)
        tagged = _snapshot_paths_for_env(project, env_name, env)
        for t in tagged:
            if t["kind"] == "project_directory":
                compute_access.check_permission(
                    project, env_name, t["path"], "snapshot")
            else:  # env_target
                compute_access.check_env_target_capability(
                    project, env_name, t["target_block"], "snapshot",
                    t["target_kind"])
        plans.append((env_name, env, tagged))

    # If no env contributed any file_name_only directory, the snapshot is a
    # no-op — surface that as a clean error rather than an empty record.
    if not any(tagged for _e, _env, tagged in plans):
        return {"error": (
            f"project '{project_name}' has no directories with "
            f"file_name_only permission — nothing to snapshot")}

    all_entries: list[dict] = []
    per_env_counts: dict[str, int] = {}
    errors: list[dict] = []

    for env_name, env, tagged in plans:
        env_type = env.get("type")
        env_entries: list[dict] = []
        for t in tagged:
            p = t["path"]
            kind = t["kind"]
            if env_type == "local":
                env_entries.extend(_local_walk(p))
                continue
            if env_type == "ssh":
                res = subprocess.run(_ssh_argv(env, _ssh_remote_cmd(p)),
                                     capture_output=True, text=True, timeout=timeout)
                if res.returncode != 0:
                    # An env_target path may not exist yet (project hasn't
                    # uploaded anything to its namespace) — that's not an
                    # error condition, just an empty result. Detect via the
                    # canonical find error and skip silently.
                    is_missing = (
                        kind == "env_target"
                        and ("No such file or directory" in (res.stderr or "")))
                    if is_missing:
                        continue
                    err: dict = {
                        "compute_env": env_name,
                        "path": p,
                        "error": f"find failed (rc={res.returncode})",
                        "stderr": (res.stderr or "")[-500:],
                    }
                    hint = _ssh_failure_hint(res.stderr or "", env.get("host", ""))
                    if hint:
                        err["hint"] = hint
                    errors.append(err)
                    continue
                env_entries.extend(_parse_find_output(res.stdout))
                continue
            errors.append({
                "compute_env": env_name,
                "path": p,
                "error": f"unsupported compute_env type: {env_type!r}",
            })
        # Tag every entry with its compute_env for the aggregated record.
        for e in env_entries:
            e["compute_env"] = env_name
        all_entries.extend(env_entries)
        per_env_counts[env_name] = len(env_entries)

    out = {
        "project":        project_name,
        "compute_envs":   [env_name for env_name, _e, _p in plans],
        "captured_at":    datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "entries":        all_entries,
        "entry_count":    len(all_entries),
        "per_env_counts": per_env_counts,
    }
    if errors:
        out["errors"] = errors
    return out
