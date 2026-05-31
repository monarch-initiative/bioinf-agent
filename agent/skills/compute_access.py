"""
Compute-env access control. The agent's ONLY interaction with a compute env
is mediated by this module — every primitive that touches a compute env's
filesystem MUST go through `check_permission()` before any subprocess runs.

The trust model
---------------
The agent has a fixed, small set of operations (today: `snapshot`; later:
`upload`, `download`, `hpc_run`, `read_content`). Each operation requires a
specific permission on the target directory; permissions are declared by the
user in `~/.bioinf/projects_access.yaml`. Any path not explicitly declared
has permission `none` — the default is fail-closed.

The agent cannot:
  - execute arbitrary commands on the compute env
  - access directories not declared in `projects_access.yaml`
  - elevate its own permissions
  - overwrite existing files via the `upload` permission (when wired)

This is enforced at the call-graph level: every subprocess call in
agent/skills/snapshot.py (and future agent/skills/{upload,download,…}.py) is
preceded by `check_permission`. A test under
tests/integration/honesty/L14_compute_env_safety/ pins this invariant —
adding a subprocess call that bypasses check_permission must fail CI.

The yaml schema
---------------
    compute_envs:
      <env_name>:
        type: ssh | local
        host: <hostname>           # for type=ssh
        user: <username>           # for type=ssh; auth via ssh-agent ONLY
        directories:
          - path: /abs/path/
            permission: none | file_name_only | upload
            description: <free text>

    projects:
      <project_name>:
        compute_env: <env_name>
        snapshot_paths: [/abs/path/, ...]
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml


# The three permission levels for v0. Adding a new level requires:
#   1. Adding it here
#   2. Adding the operation that requires it to OPERATION_REQUIRES
#   3. Implementing the primitive in its own module
#   4. Writing a cheat-guard test in tests/integration/honesty/L14_*
PERMISSIONS: frozenset[str] = frozenset({
    "none",              # default; no agent access
    "file_name_only",    # agent can list paths/size/mtime/type via the snapshot primitive
    "upload",            # agent can write NEW files (no overwrite); primitive not yet wired
})

# Which permission an operation requires. The check is EXACT match — a dir
# with `upload` does not implicitly grant `file_name_only`. Discrete
# capabilities, not a lattice. (We add a partial order later if needed.)
OPERATION_REQUIRES: dict[str, str] = {
    "snapshot": "file_name_only",
    # Future:
    # "upload":   "upload",
}


class PermissionDenied(Exception):
    """The agent attempted an operation on a path it isn't authorized for."""


class ConfigError(Exception):
    """projects_access.yaml is malformed or missing required fields."""


def default_access_path() -> Path:
    """The canonical location for projects_access.yaml. Users may override
    in their MCP/agent invocations, but this is the default."""
    return Path.home() / ".bioinf" / "projects_access.yaml"


def load_access(path: Optional[Path] = None) -> dict:
    """Read + validate the projects_access.yaml manifest. Returns the parsed
    dict on success; raises FileNotFoundError if missing, ConfigError if the
    schema is wrong (unknown permission, missing required fields)."""
    p = Path(path) if path else default_access_path()
    if not p.exists():
        raise FileNotFoundError(
            f"projects_access.yaml not found at {p}. See "
            f"agent/skills/projects_access.yaml.example for a template.")
    data = yaml.safe_load(p.read_text()) or {}
    _validate(data, p)
    return data


def _validate(data: dict, path: Path) -> None:
    """Shape-check the loaded manifest. Refuse early on a permission token
    we don't recognize — that's the most likely silent-misconfiguration class
    (typo in 'file_name_only' yields a default-none permission, which is
    correct-but-surprising)."""
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level must be a mapping")
    envs = data.get("compute_envs") or {}
    if not isinstance(envs, dict):
        raise ConfigError(f"{path}: 'compute_envs' must be a mapping")
    for name, env in envs.items():
        if not isinstance(env, dict):
            raise ConfigError(f"{path}: compute_envs.{name} must be a mapping")
        t = env.get("type")
        if t not in ("ssh", "local"):
            raise ConfigError(
                f"{path}: compute_envs.{name}.type must be 'ssh' or 'local' (got {t!r})")
        if t == "ssh":
            if not env.get("host"):
                raise ConfigError(
                    f"{path}: compute_envs.{name} is type=ssh but has no 'host'")
        dirs = env.get("directories") or []
        if not isinstance(dirs, list):
            raise ConfigError(
                f"{path}: compute_envs.{name}.directories must be a list")
        for i, d in enumerate(dirs):
            if not isinstance(d, dict):
                raise ConfigError(
                    f"{path}: compute_envs.{name}.directories[{i}] must be a mapping")
            dp = d.get("path")
            if not isinstance(dp, str) or not dp.startswith("/"):
                raise ConfigError(
                    f"{path}: compute_envs.{name}.directories[{i}].path must be an "
                    f"absolute path string (got {dp!r})")
            perm = d.get("permission")
            if perm not in PERMISSIONS:
                raise ConfigError(
                    f"{path}: compute_envs.{name}.directories[{i}] has unknown "
                    f"permission {perm!r}. Allowed: {sorted(PERMISSIONS)}")

    projects = data.get("projects") or {}
    if not isinstance(projects, dict):
        raise ConfigError(f"{path}: 'projects' must be a mapping")
    for name, proj in projects.items():
        if not isinstance(proj, dict):
            raise ConfigError(f"{path}: projects.{name} must be a mapping")
        env_ref = proj.get("compute_env")
        if env_ref and env_ref not in envs:
            raise ConfigError(
                f"{path}: projects.{name}.compute_env={env_ref!r} doesn't match "
                f"any compute_envs entry")


def get_compute_env(name: str, access: dict) -> dict:
    """Look up a compute_env by name. Raises KeyError with a helpful message."""
    envs = access.get("compute_envs") or {}
    env = envs.get(name)
    if not env:
        raise KeyError(
            f"compute_env not found in projects_access.yaml: {name!r}. "
            f"Available: {sorted(envs.keys())}")
    return {**env, "_name": name}


def get_project(name: str, access: dict) -> dict:
    """Look up a project by name. Raises KeyError with a helpful message."""
    projects = access.get("projects") or {}
    proj = projects.get(name)
    if not proj:
        raise KeyError(
            f"project not found in projects_access.yaml: {name!r}. "
            f"Available: {sorted(projects.keys())}")
    return {**proj, "_name": name}


def check_permission(compute_env: dict, path: str, operation: str) -> dict:
    """The CORE security gate. Returns the matching directory entry if the
    compute_env permits `operation` on `path`; raises PermissionDenied
    otherwise. Every primitive in agent/skills/ that subprocesses anything
    against a compute_env MUST call this first.

    Match semantics: longest-prefix match against compute_env.directories.
    A request for `/scratch/x/sub/file` matches the entry `/scratch/x/` (with
    or without trailing slash). The MORE SPECIFIC entry wins — so a parent
    `/scratch/x/` with permission file_name_only and a child
    `/scratch/x/uploads/` with permission upload coexist; a request inside
    the child sees `upload`, a request inside the parent (not child) sees
    `file_name_only`.

    Permission match: EXACT. A directory with permission `upload` does NOT
    satisfy an `operation` requiring `file_name_only`. (Discrete capabilities,
    not a lattice. We can introduce a partial order later if needed.)

    `path` MUST be an absolute path string; otherwise the match is rejected
    immediately — relative paths have no meaning at the gate level (the
    compute_env's home directory isn't ours to assume).
    """
    if operation not in OPERATION_REQUIRES:
        raise PermissionDenied(f"unknown operation: {operation!r}")
    required = OPERATION_REQUIRES[operation]

    if not isinstance(path, str) or not path.startswith("/"):
        raise PermissionDenied(
            f"path must be an absolute string, got {path!r}")

    # Normalize for comparison (strip trailing slash so '/a/b' and '/a/b/' match
    # the same entry).
    req_norm = path.rstrip("/")
    dirs = compute_env.get("directories") or []
    best: Optional[dict] = None
    best_len = -1
    for d in dirs:
        dp = (d.get("path") or "").rstrip("/")
        if not dp:
            continue
        if req_norm == dp or req_norm.startswith(dp + "/"):
            if len(dp) > best_len:
                best = d
                best_len = len(dp)

    if best is None:
        raise PermissionDenied(
            f"path {path!r} is not authorized in compute_env "
            f"{compute_env.get('_name', '?')!r} — no matching directory entry. "
            f"Add this path to compute_envs.{compute_env.get('_name','?')}."
            f"directories with permission={required!r} to allow.")

    actual = best.get("permission")
    if actual != required:
        raise PermissionDenied(
            f"path {path!r} has permission {actual!r}, but operation "
            f"{operation!r} requires {required!r}. "
            f"Matched directory entry: {best.get('path')!r}.")
    return best
