"""
Compute-env access control. The agent's ONLY interaction with a compute env
is mediated by this module — every primitive that touches a compute env's
filesystem MUST go through `check_permission()` before any subprocess runs.

The trust model
---------------
The agent has a fixed, small set of operations (today: `snapshot`; later:
`upload`, `download`, `hpc_run`, `read_content`). Each operation requires a
specific permission on the target directory. Permissions are declared by the
user in `projects_access.yaml`, at the PROJECT level — the compute_env layer
is just connection + container-upload slot. Any directory not explicitly
listed in a project has permission `none` — fail-closed.

The agent cannot:
  - execute arbitrary commands on the compute env
  - access directories not declared in a project's compute_env_access
  - elevate its own permissions
  - overwrite existing files via the `upload` permission (when wired)

This is enforced at the call-graph level: every subprocess call in
agent/skills/snapshot.py (and future agent/skills/{upload,download,…}.py) is
preceded by `check_permission`. A test under
tests/integration/honesty/L14_compute_env_safety/ pins this invariant —
adding a subprocess call that bypasses check_permission must fail CI.

The schema (the fixed vocabulary)
---------------------------------
Every key is from THIS module's schema language. Every value is user-
supplied. See agent/skills/projects_access.yaml.example for an annotated
template.

    compute_envs:                              # list of named compute envs
      - name: <your label>
        type: ssh | local
        host: <hostname>                       # ssh only
        user: <username>                       # ssh only; ssh-agent handles auth
        container_upload_target:               # dir-access block OR null
          path: <abs path>
          permissions: [upload]                # always exactly [upload]
          description: <free text>

    projects:                                  # list of named projects
      - name: <your label>
        description: <free text>
        compute_env_access:                    # one block per env this project touches
          - compute_env: <name of a compute_envs[].name>
            directories:                       # dir-access blocks on that env
              - path: <abs path>
                permissions: [<token>, ...]    # any subset of: file_name_only, upload
                description: <free text>

The dir-access block `{path, permissions, description}` is reused in both
`compute_envs[].container_upload_target` and projects[]…directories[] — same
building block, two locations.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import yaml


# The permission tokens for v0. Adding a new one requires:
#   1. Adding it here
#   2. Adding the operation that consumes it to OPERATION_REQUIRES
#   3. Implementing the primitive in its own module
#   4. Writing a cheat-guard test in tests/integration/honesty/L14_*
PERMISSIONS: frozenset[str] = frozenset({
    "none",              # default; no agent access
    "file_name_only",    # list a dir's IMMEDIATE contents (one level only).
                         # See agent/skills/snapshot.py module docstring for the
                         # one-level visibility contract.
    "upload",            # write NEW files (never overwrites); primitive not yet wired
})

# Which permission an operation requires. Match is EXACT — a dir with `upload`
# does NOT implicitly grant `file_name_only`. Discrete capabilities, not a
# lattice. A dir can declare both via `permissions: [file_name_only, upload]`.
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
    """The canonical location for projects_access.yaml. The MCP wrapper / agent
    invocations may override via an explicit `access_path=` kwarg; this is the
    default. (Auto-discovery from cwd/repo-root is a deferred design question —
    today, pass `access_path=` if your file lives elsewhere.)"""
    return Path.home() / ".bioinf" / "projects_access.yaml"


# ---------------------------------------------------------------------------
# Loading + validation
# ---------------------------------------------------------------------------

def load_access(path: Optional[Path] = None) -> dict:
    """Read + validate the projects_access.yaml manifest. Returns the parsed
    dict on success; raises FileNotFoundError if missing, ConfigError if the
    schema is wrong."""
    p = Path(path) if path else default_access_path()
    if not p.exists():
        raise FileNotFoundError(
            f"projects_access.yaml not found at {p}. See "
            f"agent/skills/projects_access.yaml.example for an annotated template.")
    data = yaml.safe_load(p.read_text()) or {}
    _validate(data, p)
    return data


def _validate(data: dict, path: Path) -> None:
    """Shape-check the loaded manifest. The validator is strict about typos
    (unknown permission tokens, unknown env types, missing required fields) —
    a silent default would be a security regression by ergonomic surprise."""
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top-level must be a mapping")

    envs = data.get("compute_envs") or []
    if not isinstance(envs, list):
        raise ConfigError(
            f"{path}: 'compute_envs' must be a LIST of named env blocks "
            f"(got {type(envs).__name__}). See the .example file.")
    env_names: set[str] = set()
    for i, env in enumerate(envs):
        _validate_compute_env(env, i, env_names, path)

    projects = data.get("projects") or []
    if not isinstance(projects, list):
        raise ConfigError(
            f"{path}: 'projects' must be a LIST of named project blocks "
            f"(got {type(projects).__name__}). See the .example file.")
    project_names: set[str] = set()
    for i, proj in enumerate(projects):
        _validate_project(proj, i, project_names, env_names, path)


def _validate_compute_env(env: object, idx: int, env_names: set[str], path: Path) -> None:
    """Validate one compute_envs[] entry."""
    if not isinstance(env, dict):
        raise ConfigError(f"{path}: compute_envs[{idx}] must be a mapping")

    name = env.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(
            f"{path}: compute_envs[{idx}].name must be a non-empty string")
    if name in env_names:
        raise ConfigError(f"{path}: duplicate compute_env name {name!r}")
    env_names.add(name)

    t = env.get("type")
    if t not in ("ssh", "local"):
        raise ConfigError(
            f"{path}: compute_envs[{idx}] (name={name!r}) .type must be "
            f"'ssh' or 'local' (got {t!r})")
    if t == "ssh":
        if not env.get("host"):
            raise ConfigError(
                f"{path}: compute_envs[{idx}] (name={name!r}) is type=ssh "
                f"but has no 'host'")

    # container_upload_target is optional; if present, validate it as a
    # dir-access block. `upload` MUST be in its permissions (else the slot
    # doesn't mean what it says) — but other tokens (e.g. file_name_only,
    # so the agent can SEE what's been uploaded) are allowed.
    cut = env.get("container_upload_target")
    if cut is not None:
        _validate_dir_block(cut, f"compute_envs[{idx}] (name={name!r}) "
                                 f".container_upload_target", path,
                            must_include=["upload"])


def _validate_project(proj: object, idx: int, project_names: set[str],
                       env_names: set[str], path: Path) -> None:
    """Validate one projects[] entry."""
    if not isinstance(proj, dict):
        raise ConfigError(f"{path}: projects[{idx}] must be a mapping")

    name = proj.get("name")
    if not isinstance(name, str) or not name:
        raise ConfigError(f"{path}: projects[{idx}].name must be a non-empty string")
    if name in project_names:
        raise ConfigError(f"{path}: duplicate project name {name!r}")
    project_names.add(name)

    access_list = proj.get("compute_env_access") or []
    if not isinstance(access_list, list):
        raise ConfigError(
            f"{path}: projects[{idx}] (name={name!r}) .compute_env_access "
            f"must be a list")
    for j, block in enumerate(access_list):
        _validate_access_block(block, name, j, env_names, path)


def _validate_access_block(block: object, project_name: str, idx: int,
                            env_names: set[str], path: Path) -> None:
    """Validate one compute_env_access[] entry inside a project."""
    if not isinstance(block, dict):
        raise ConfigError(
            f"{path}: projects[name={project_name!r}].compute_env_access[{idx}] "
            f"must be a mapping")
    env_ref = block.get("compute_env")
    if not isinstance(env_ref, str):
        raise ConfigError(
            f"{path}: projects[name={project_name!r}].compute_env_access[{idx}]"
            f".compute_env must be a string")
    if env_ref not in env_names:
        raise ConfigError(
            f"{path}: projects[name={project_name!r}].compute_env_access[{idx}]"
            f".compute_env={env_ref!r} doesn't match any compute_envs[].name. "
            f"Known envs: {sorted(env_names)}")

    dirs = block.get("directories") or []
    if not isinstance(dirs, list):
        raise ConfigError(
            f"{path}: projects[name={project_name!r}]"
            f".compute_env_access[{idx}].directories must be a list")
    for k, d in enumerate(dirs):
        _validate_dir_block(
            d, f"projects[name={project_name!r}]"
               f".compute_env_access[{idx}].directories[{k}]", path)


def _validate_dir_block(block: object, where: str, path: Path,
                         must_include: Optional[list[str]] = None) -> None:
    """The reusable dir-access block: {path, permissions, description?}.
    Used in both compute_envs[].container_upload_target and projects[]…
    .directories[].

    `must_include` (when set) requires the permissions[] list to contain
    every listed token — used by container_upload_target to enforce that
    the slot has `upload`. Additional tokens beyond those required are
    allowed; users can combine permissions freely on the same dir.
    """
    if not isinstance(block, dict):
        raise ConfigError(f"{path}: {where} must be a mapping")
    dp = block.get("path")
    if not isinstance(dp, str) or not dp.startswith("/"):
        raise ConfigError(
            f"{path}: {where}.path must be an absolute path string (got {dp!r})")
    perms = block.get("permissions")
    if not isinstance(perms, list) or not perms:
        raise ConfigError(
            f"{path}: {where}.permissions must be a non-empty list of "
            f"permission tokens (got {perms!r}). Allowed tokens: "
            f"{sorted(t for t in PERMISSIONS if t != 'none')}")
    for tok in perms:
        if tok not in PERMISSIONS or tok == "none":
            raise ConfigError(
                f"{path}: {where}.permissions has unknown token {tok!r}. "
                f"Allowed tokens: {sorted(t for t in PERMISSIONS if t != 'none')}")
    if must_include is not None:
        missing = [t for t in must_include if t not in perms]
        if missing:
            raise ConfigError(
                f"{path}: {where}.permissions must include {must_include!r} "
                f"(missing: {missing!r}; got {perms!r})")


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

def get_compute_env(name: str, access: dict) -> dict:
    """Look up a compute_env by its declared `name:` field. Raises KeyError."""
    for env in access.get("compute_envs") or []:
        if env.get("name") == name:
            return env
    known = [e.get("name") for e in access.get("compute_envs") or []]
    raise KeyError(
        f"compute_env not found in projects_access.yaml: {name!r}. "
        f"Available: {sorted(n for n in known if n)}")


def get_project(name: str, access: dict) -> dict:
    """Look up a project by its declared `name:` field. Raises KeyError."""
    for proj in access.get("projects") or []:
        if proj.get("name") == name:
            return proj
    known = [p.get("name") for p in access.get("projects") or []]
    raise KeyError(
        f"project not found in projects_access.yaml: {name!r}. "
        f"Available: {sorted(n for n in known if n)}")


def get_project_directories(project: dict, compute_env_name: str) -> list[dict]:
    """Return the dir-access blocks declared for `compute_env_name` inside
    `project`. Returns [] if the project has no access block for that env.
    A dir's presence here means it's authorized for at least one operation;
    `check_permission` enforces the specific operation."""
    for block in project.get("compute_env_access") or []:
        if block.get("compute_env") == compute_env_name:
            return list(block.get("directories") or [])
    return []


# ---------------------------------------------------------------------------
# The core security gate
# ---------------------------------------------------------------------------

def check_permission(project: dict, compute_env_name: str, path: str,
                     operation: str) -> dict:
    """Return the matching directory entry if `project` permits `operation`
    on `path` on `compute_env_name`; raises PermissionDenied otherwise.

    Every primitive in agent/skills/ that subprocesses anything against a
    compute_env MUST call this first.

    Match semantics: longest-prefix match against the project's authorized
    directories for the given compute_env. A request for `/scratch/x/sub/file`
    matches the entry `/scratch/x/` (with or without trailing slash). The
    MORE SPECIFIC entry wins.

    Permission match: the operation's required permission token MUST appear
    in the matched directory's `permissions:` list. A dir declared
    `permissions: [upload]` does NOT satisfy `snapshot` (which requires
    `file_name_only`). Multiple tokens on the same dir are allowed:
    `permissions: [file_name_only, upload]` satisfies both.

    `path` MUST be an absolute path string; otherwise the match is rejected
    immediately — relative paths have no meaning at the security boundary."""
    if operation not in OPERATION_REQUIRES:
        raise PermissionDenied(f"unknown operation: {operation!r}")
    required = OPERATION_REQUIRES[operation]

    if not isinstance(path, str) or not path.startswith("/"):
        raise PermissionDenied(
            f"path must be an absolute string, got {path!r}")

    # Normalize for comparison (strip trailing slash so '/a/b' and '/a/b/'
    # match the same entry).
    req_norm = path.rstrip("/")
    dirs = get_project_directories(project, compute_env_name)
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

    proj_name = project.get("name", "?")
    if best is None:
        raise PermissionDenied(
            f"path {path!r} is not authorized in project {proj_name!r} "
            f"on compute_env {compute_env_name!r} — no matching directory entry. "
            f"Add this path to projects[name={proj_name!r}].compute_env_access"
            f"[compute_env={compute_env_name!r}].directories with permissions "
            f"including {required!r} to allow.")

    actual_perms = list(best.get("permissions") or [])
    if required not in actual_perms:
        raise PermissionDenied(
            f"path {path!r} has permissions {actual_perms!r}, but operation "
            f"{operation!r} requires {required!r}. "
            f"Matched directory entry: {best.get('path')!r}.")
    return best
