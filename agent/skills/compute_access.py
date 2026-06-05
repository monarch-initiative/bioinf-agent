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


# The permission tokens. Adding a new one requires:
#   1. Adding it here
#   2. Adding the operation that consumes it to OPERATION_REQUIRES
#   3. Implementing the primitive in its own module
#   4. Writing a cheat-guard test in tests/integration/honesty/L14_*
PERMISSIONS: frozenset[str] = frozenset({
    "none",              # default; no agent access
    "file_name_only",    # list a dir's IMMEDIATE contents (one level only).
                         # See agent/skills/snapshot.py module docstring for the
                         # one-level visibility contract.
    "upload",            # write NEW files (never overwrites); single-shot push
    "download",          # pull files FROM the env back to local (sha256
                         # round-trip). Symmetric to `upload`.
    "exec",              # a SLURM job inside this dir may read+write its OWN
                         # outputs (data_acquisition into common_data; workflow
                         # run output into scratch). Distinct from `upload`,
                         # which is the agent's single-shot push primitive —
                         # the cluster job itself doesn't "upload"; it writes-
                         # in-place during execution. Phase 2.
})

# Which permission an operation requires. Match is EXACT — a dir with `upload`
# does NOT implicitly grant `file_name_only`. Discrete capabilities, not a
# lattice. A dir can declare multiple tokens via
# `permissions: [file_name_only, upload]`.
OPERATION_REQUIRES: dict[str, str] = {
    "snapshot": "file_name_only",
    # Phase 2 — wired as primitives land:
    "upload_to_scratch":         "upload",
    "download_from_scratch":     "download",
    "upload_to_common_data":     "upload",
    "download_from_common_data": "download",
    # Project workspaces — Phase-1 directories[] gate (project-explicit auth):
    "upload_to_project_path":     "upload",
    "download_from_project_path": "download",
    # "job_workdir_scratch":     "exec",
    # "job_output_common_data":  "exec",
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

    # --- Phase 2 env-level target blocks (all optional) ---
    #
    # Authorization model: the env-level target blocks are CAPABILITY
    # DECLARATIONS — they say "this path exists on the env and these ops
    # are supported on it". Any project listed under this env via
    # `compute_env_access` inherits ALL declared targets en bloc; there is
    # no per-project re-declaration of these paths. The security posture:
    # these paths are PUBLIC-DATA ZONES (no PHI, no protected datasets).
    # Project-specific / protected paths go in `projects[].compute_env_access
    # [].directories[]` where the project's `file_name_only` permission is
    # explicit, and the agent can never list a dir it wasn't authorized for.
    where_env = f"compute_envs[{idx}] (name={name!r})"
    scratch = env.get("agent_scratch_target")
    if scratch is not None:
        # The agent's sandbox: writable AND downloadable AND job-executable.
        # All three permissions are intrinsic to what the sandbox is FOR —
        # if you don't want one of them, don't declare the block.
        _validate_dir_block(scratch, f"{where_env}.agent_scratch_target", path,
                            must_include=["upload", "download", "exec"])

    common = env.get("agent_common_data_target")
    if common is not None:
        # Singular sibling of agent_scratch_target — the env's "common data"
        # zone (reference genomes, public databases, anything the agent may
        # need to push to and that jobs may read/write from). One per env.
        _validate_dir_block(common, f"{where_env}.agent_common_data_target",
                            path, must_include=["upload", "download", "exec"])

    slurm = env.get("slurm")
    if slurm is not None:
        _validate_slurm_block(slurm, f"{where_env}.slurm", path)

    # Disjoint-subtree check ACROSS all of this env's declared paths. No path
    # may be a prefix of another (or equal). A breach of one target dir must
    # never grant access to another. Cross-env disjointness is not checked —
    # the security boundary is the env, not the manifest.
    _check_env_paths_disjoint(env, where_env, path)


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
# Phase 2 — env-level block validators
# ---------------------------------------------------------------------------

# The closed key set for the slurm block. Unknown keys are rejected at load
# time — silent typos (`max_corees_per_job: 9999`) would otherwise let the
# agent submit jobs exceeding the user's intended cap. The values here are
# the FENCEPOSTS — every submit_cluster_job call validates against them.
_SLURM_REQUIRED_KEYS: frozenset[str] = frozenset({
    "queue_default",
    "allowed_queues",
    "account",
    "max_cores_per_job",
    "max_mem_gb_per_job",
    "max_time_hours_per_job",
})

# Optional slurm keys — present, use them; absent, the launcher omits the
# corresponding lines. Same closed-key discipline (typos rejected) — just
# not required to declare.
_SLURM_OPTIONAL_KEYS: frozenset[str] = frozenset({
    "mail_user",     # str — what `#SBATCH --mail-user=` points at
    "module_loads",  # list[str] — module-load lines the launcher emits
})


def _validate_slurm_block(blk: object, where: str, path: Path) -> None:
    """Validate the closed `slurm:` block. Unknown keys rejected. Type-check
    each field; the max_* values are the upper bound submit_cluster_job
    enforces — they must be positive ints."""
    if not isinstance(blk, dict):
        raise ConfigError(f"{path}: {where} must be a mapping")
    allowed = _SLURM_REQUIRED_KEYS | _SLURM_OPTIONAL_KEYS
    extra = set(blk.keys()) - allowed
    if extra:
        raise ConfigError(
            f"{path}: {where} has unknown keys {sorted(extra)!r}. "
            f"Allowed keys: {sorted(allowed)!r}")
    missing = _SLURM_REQUIRED_KEYS - set(blk.keys())
    if missing:
        raise ConfigError(
            f"{path}: {where} missing required keys {sorted(missing)!r}")

    qd = blk["queue_default"]
    if not isinstance(qd, str) or not qd:
        raise ConfigError(
            f"{path}: {where}.queue_default must be a non-empty string")
    aq = blk["allowed_queues"]
    if not isinstance(aq, list) or not aq or not all(
            isinstance(x, str) and x for x in aq):
        raise ConfigError(
            f"{path}: {where}.allowed_queues must be a non-empty list of "
            f"non-empty strings (got {aq!r})")
    if qd not in aq:
        raise ConfigError(
            f"{path}: {where}.queue_default={qd!r} must appear in "
            f".allowed_queues={aq!r}")
    acct = blk["account"]
    if not isinstance(acct, str) or not acct:
        raise ConfigError(
            f"{path}: {where}.account must be a non-empty string")
    for k in ("max_cores_per_job", "max_mem_gb_per_job", "max_time_hours_per_job"):
        v = blk[k]
        # `bool` is a subclass of int in Python — exclude it explicitly.
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise ConfigError(
                f"{path}: {where}.{k} must be a positive integer (got {v!r})")

    # Optional keys — present? type-check. Absent? fine.
    if "mail_user" in blk:
        mu = blk["mail_user"]
        if not isinstance(mu, str) or not mu or "@" not in mu:
            raise ConfigError(
                f"{path}: {where}.mail_user must be a non-empty email-like "
                f"string (got {mu!r})")
        # Newline injection defense — `--mail-user=<X>` would split a header
        # if X contains a newline. Boring constraint, real defense.
        if any(c in mu for c in "\n\r"):
            raise ConfigError(
                f"{path}: {where}.mail_user contains newline/CR "
                f"(SBATCH header injection); refused")
    if "module_loads" in blk:
        ml = blk["module_loads"]
        if not isinstance(ml, list) or not all(
                isinstance(x, str) and x for x in ml):
            raise ConfigError(
                f"{path}: {where}.module_loads must be a list of non-empty "
                f"strings (got {ml!r})")
        # Each entry becomes `module load <X>` in the launcher. Refuse
        # shell metacharacters that would break out of that line. Module
        # names canonically match [A-Za-z0-9_+\-./], so anything outside
        # that set is forbidden.
        for item in ml:
            if any(c in item for c in "\x00\n\r\t ;|&$`<>(){}[]*?\"'\\"):
                raise ConfigError(
                    f"{path}: {where}.module_loads entry {item!r} contains "
                    f"forbidden character (newline/space/shell metachar)")


def _is_safe_token(s: str) -> bool:
    """A token usable as a kwarg / SBATCH placeholder / path prefix: alnum
    + `_-`, 1..64. Rejects `..`, `/`, dots (path traversal), spaces, shell
    metacharacters, and newlines (sbatch header injection). Used for the
    project-name prefix the scratch/common_data primitives auto-prepend."""
    if not s or len(s) > 64:
        return False
    return all(c.isalnum() or c in "_-" for c in s)


def _check_env_paths_disjoint(env: dict, where: str, path: Path) -> None:
    """No declared path on this env may be a prefix of (or equal to) another.
    A breach of one target dir must never grant access to another. The check
    is across container_upload_target, agent_scratch_target, and
    agent_common_data_target — all of these are absolute paths on the same
    filesystem and they're trust-isolated by being disjoint subtrees."""
    paths: list[tuple[str, str]] = []  # (path_normalized, label)
    cut = env.get("container_upload_target")
    if cut is not None:
        p = (cut.get("path") or "").rstrip("/")
        if p:
            paths.append((p, "container_upload_target"))
    scratch = env.get("agent_scratch_target")
    if scratch is not None:
        p = (scratch.get("path") or "").rstrip("/")
        if p:
            paths.append((p, "agent_scratch_target"))
    common = env.get("agent_common_data_target")
    if common is not None:
        p = (common.get("path") or "").rstrip("/")
        if p:
            paths.append((p, "agent_common_data_target"))
    for i, (pa, la) in enumerate(paths):
        for pb, lb in paths[i + 1:]:
            if pa == pb:
                raise ConfigError(
                    f"{path}: {where} declares the SAME path {pa!r} under "
                    f"BOTH {la} and {lb} — target dirs must be disjoint "
                    f"subtrees on the env")
            if pa.startswith(pb + "/") or pb.startswith(pa + "/"):
                a, b = sorted([(pa, la), (pb, lb)], key=lambda t: len(t[0]))
                raise ConfigError(
                    f"{path}: {where} target path {a[0]!r} ({a[1]}) is a "
                    f"PARENT of {b[0]!r} ({b[1]}) — target dirs must be "
                    f"disjoint subtrees on the env (a breach of one must "
                    f"not grant access to another)")


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
# Phase 2 — env-level lookups
#
# These return the RAW yaml dicts after validation has passed. They do NOT
# check per-project authorization; the consuming primitives (upload_to_*,
# fetch_from_*, submit_cluster_job) layer project-level grants on top in
# their own modules, in the same shape as Phase 1's check_permission.
# ---------------------------------------------------------------------------

def get_agent_scratch_target(env: dict) -> Optional[dict]:
    """Return the agent_scratch_target dir-access block for this env, or
    None if undeclared. The block carries `path`, `permissions` (always
    ⊇ {upload, download, exec} by validator), and `description`."""
    blk = env.get("agent_scratch_target")
    return blk if isinstance(blk, dict) else None


def get_agent_common_data_target(env: dict) -> Optional[dict]:
    """Return the agent_common_data_target dir-access block for this env,
    or None if undeclared. Singular sibling of agent_scratch_target — the
    env's shared common-data zone (reference genomes, public DBs). Block
    carries `path`, `permissions` (always ⊇ {upload, download, exec}),
    and `description`."""
    blk = env.get("agent_common_data_target")
    return blk if isinstance(blk, dict) else None


def get_slurm_config(env: dict) -> Optional[dict]:
    """Return the closed slurm config block for this env, or None if
    undeclared. The block carries queue_default, allowed_queues, account,
    and the three max_* caps. submit_cluster_job refuses any submission
    on an env without this block."""
    blk = env.get("slurm")
    return blk if isinstance(blk, dict) else None


# ---------------------------------------------------------------------------
# Phase 2 — env-implicit grant helper
#
# The Phase-2 actuator primitives (upload_to_scratch, download_from_scratch,
# upload_to_common_data, etc.) do NOT walk the project's `directories[]`
# entries for env-level target paths. Instead the env declares the target
# block ONCE; any project that lists the env in its `compute_env_access`
# inherits the target en bloc. The required permission (`upload`,
# `download`, `exec`) must appear in the env-level target's `permissions`.
#
# This helper centralizes that check so every Phase-2 primitive uses the
# same shape.
# ---------------------------------------------------------------------------

def check_env_target_capability(project: dict, env_name: str,
                                target: dict, operation: str,
                                target_kind: str) -> None:
    """Verify that `project` has access to compute env `env_name` (via its
    `compute_env_access`) AND that `target` (an env-level dir-access block,
    typically returned by `get_agent_scratch_target` / `get_agent_common_data_target`)
    advertises the permission token required by `operation`. Raises
    PermissionDenied on any failure.

    `target_kind` is purely cosmetic — included in error messages so the
    user knows WHICH env-level block failed validation (e.g.
    'agent_scratch_target' vs 'agent_common_data_target').

    The project's `directories[]` is NOT consulted here — those entries
    are reserved for PROJECT-SPECIFIC paths and use the older Phase-1
    `check_permission` path."""
    if operation not in OPERATION_REQUIRES:
        raise PermissionDenied(f"unknown operation: {operation!r}")
    required = OPERATION_REQUIRES[operation]

    proj_name = project.get("name", "?")
    # 1. Project must have a compute_env_access entry for this env.
    grants = [b for b in (project.get("compute_env_access") or [])
              if isinstance(b, dict) and b.get("compute_env") == env_name]
    if not grants:
        raise PermissionDenied(
            f"project {proj_name!r} has no compute_env_access entry for "
            f"compute_env {env_name!r} — add one to use {target_kind} on it.")

    # 2. The target block must advertise the required capability.
    if not isinstance(target, dict):
        raise PermissionDenied(
            f"compute_env {env_name!r} has no {target_kind} declared — "
            f"operation {operation!r} requires it.")
    actual = list(target.get("permissions") or [])
    if required not in actual:
        raise PermissionDenied(
            f"compute_env {env_name!r}.{target_kind}.permissions={actual!r} "
            f"does not include {required!r} (operation {operation!r}). "
            f"Either add it on the env or use a different primitive.")


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
