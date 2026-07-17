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
    # Unified transfer surface — replaces the six zone-specific
    # primitives. The operation name IS the required token.
    "upload":   "upload",
    "download": "download",
    # The dir that an sbatched SLURM job runs in must declare `exec` so
    # the job is allowed to write its own outputs in-place during
    # execution (distinct from the agent's single-shot `upload`).
    "submit_workflow_job":        "exec",
    # run_step_on_cluster runs the validation/seal job inside the agent's
    # scratch sandbox. Goes through the env-level agent_scratch_target
    # (via check_env_target_capability), NOT project.directories[].
    "run_step_on_cluster":        "exec",
}

# The batch scheduler a compute env runs jobs through, declared via `job_manager`.
# A CONTROLLED enum:
#   slurm — an sbatch launcher submitted via `sbatch`, polled via `sacct` (a real
#           cluster with a batch scheduler).
#   bash  — a plain shell script run directly, no scheduler (a local machine, or a
#           node you drive by hand). "Just a shell script instead of a slurm script."
# A third scheduler (pbs, lsf, …) gets added here AND gets its submit/poll wiring
# in the SAME change — never a pre-declared value with no implementation behind it.
VALID_JOB_MANAGERS: tuple[str, ...] = ("slurm", "bash")

class PermissionDenied(Exception):
    """The agent attempted an operation on a path it isn't authorized for."""


class ConfigError(Exception):
    """projects_access.yaml is malformed or missing required fields."""


def default_access_path() -> Path:
    """Canonical location for projects_access.yaml.

    Preference order:
      1. ``<repo_root>/projects_access.yaml`` — the live-file convention this
         repo uses (the user keeps it alongside the source tree).
      2. ``~/.bioinf/projects_access.yaml`` — historical homedir location;
         returned as a fallback even if it doesn't exist, so callers get a
         deterministic path string to put in their FileNotFoundError.

    Callers may override with an explicit ``access_path=`` kwarg.
    """
    repo_candidate = Path(__file__).resolve().parents[2] / "projects_access.yaml"
    if repo_candidate.exists():
        return repo_candidate
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


# The closed key set for a compute_envs[] entry. The nested blocks (slurm,
# data_transfer, globus) were already closed-key; the env block itself was NOT,
# so a typo was silently ACCEPTED and then went missing at the worst moment:
#   `data_transfr:`          → the globus block is never seen, the env silently
#                              falls back to scp_head_node, and GB-scale .sif
#                              pushes go over the HEAD NODE — the exact thing the
#                              wire-protocol choice exists to avoid.
#   `agent_scratch_targets:` → no scratch target, so run_step_on_cluster refuses
#                              much later with "env has no scratch target" and
#                              nothing points at the typo that caused it.
# A silently-ignored key is a config that doesn't do what it says. Fail closed at
# LOAD, naming the key — the cost of a typo should be an error message, not a
# head-node transfer. Keep in step with the keys the code actually reads.
_ENV_ALLOWED_KEYS: frozenset[str] = frozenset({
    "name", "type", "host", "user", "email",
    "job_manager", "slurm", "data_transfer",
    "agent_scratch_target", "agent_common_data_target", "container_upload_target",
})


def _validate_compute_env(env: object, idx: int, env_names: set[str], path: Path) -> None:
    """Validate one compute_envs[] entry."""
    if not isinstance(env, dict):
        raise ConfigError(f"{path}: compute_envs[{idx}] must be a mapping")

    extra = set(env.keys()) - _ENV_ALLOWED_KEYS
    if extra:
        raise ConfigError(
            f"{path}: compute_envs[{idx}] has unknown keys {sorted(extra)!r}. "
            f"Allowed keys: {sorted(_ENV_ALLOWED_KEYS)!r}. (A key we don't read "
            f"is a setting that silently does nothing — check for a typo.)")

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

    # job_manager: which batch scheduler this env runs jobs through. A CONTROLLED
    # enum (VALID_JOB_MANAGERS) — only 'slurm' is wired today. Optional: an env
    # that declares it must name a supported scheduler; absent means slurm (the
    # historical assumption and only implementation). Irrelevant for type=local
    # (no scheduler), but still enum-checked if present.
    jm = env.get("job_manager")
    if jm is not None and jm not in VALID_JOB_MANAGERS:
        raise ConfigError(
            f"{where_env}.job_manager must be one of {list(VALID_JOB_MANAGERS)} "
            f"(got {jm!r}) — only these batch schedulers are supported")

    slurm = env.get("slurm")
    if slurm is not None:
        _validate_slurm_block(slurm, f"{where_env}.slurm", path)

    dt = env.get("data_transfer")
    if dt is not None:
        _validate_data_transfer_block(dt, f"{where_env}.data_transfer", path)

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
# The env-level `slurm:` block is the HPC's SCHEDULER POLICY — the constants for
# THIS cluster that render_workflow_files merges into every job's header. EVERY
# key is OPTIONAL: an HPC like Longleaf (no partition selection, no account for
# CPU jobs) can omit the whole block. Email is NOT here — it's the env-level
# `email:` field. Unknown keys still rejected (closed-key discipline).
#   account      (str)  → --account on every job (omit ⇒ none, e.g. Longleaf)
#   partition    (str)  → default --partition for CPU jobs (omit ⇒ scheduler default)
#   gpu          (map)  → this HPC's GPU convention {partition, qos}; present ⇒ GPU
#                         jobs supported (a gpus>0 request renders -p/--qos/--gres)
#   max_cores_per_job / max_mem_gb_per_job / max_time_hours_per_job (pos int) caps
#   module_loads (list[str]) → extra `module load X` lines in the launcher
_SLURM_ALLOWED_KEYS: frozenset[str] = frozenset({
    "account", "partition", "gpu",
    "max_cores_per_job", "max_mem_gb_per_job", "max_time_hours_per_job",
    "module_loads",
})
_SLURM_GPU_KEYS: frozenset[str] = frozenset({"partition", "qos"})
# Shell-metacharacter set forbidden in any slurm value that reaches an SBATCH line.
_SLURM_UNSAFE_CHARS = "\x00\n\r\t ;|&$`<>(){}[]*?\"'\\"


def _validate_slurm_gpu_block(blk: object, where: str, path: Path) -> None:
    """The GPU convention `slurm.gpu: {partition, qos}` — both REQUIRED once the
    block is declared (a gpus>0 job's `-p`/`--qos` come from here; a half-declared
    convention would render a broken GPU header). Both are safe tokens; partition
    may be comma-separated (Longleaf permits `a100-gpu,l40-gpu`)."""
    if not isinstance(blk, dict):
        raise ConfigError(f"{path}: {where} must be a mapping")
    extra = set(blk.keys()) - _SLURM_GPU_KEYS
    if extra:
        raise ConfigError(f"{path}: {where} has unknown keys {sorted(extra)!r}. "
                          f"Allowed keys: {sorted(_SLURM_GPU_KEYS)!r}")
    missing = _SLURM_GPU_KEYS - set(blk.keys())
    if missing:
        raise ConfigError(
            f"{path}: {where} missing required keys {sorted(missing)!r} — a GPU "
            f"convention needs BOTH partition and qos")
    for k in _SLURM_GPU_KEYS:
        v = blk[k]
        if not isinstance(v, str) or not v:
            raise ConfigError(f"{path}: {where}.{k} must be a non-empty string (got {v!r})")
        if any(c in v for c in _SLURM_UNSAFE_CHARS):
            raise ConfigError(f"{path}: {where}.{k} contains shell metacharacters "
                              f"(SBATCH header injection); refused")


def _validate_slurm_block(blk: object, where: str, path: Path) -> None:
    """Validate the OPTIONAL, closed `slurm:` block — the HPC's scheduler policy.
    ALL keys optional (Longleaf CPU jobs need none); unknown keys rejected; each
    present key type-checked. Email is NOT here — it's the env-level `email:`."""
    if not isinstance(blk, dict):
        raise ConfigError(f"{path}: {where} must be a mapping")
    extra = set(blk.keys()) - _SLURM_ALLOWED_KEYS
    if extra:
        raise ConfigError(
            f"{path}: {where} has unknown keys {sorted(extra)!r}. "
            f"Allowed keys: {sorted(_SLURM_ALLOWED_KEYS)!r}")

    # account / partition — optional non-empty safe strings (no header injection).
    for k in ("account", "partition"):
        if k in blk:
            v = blk[k]
            if not isinstance(v, str) or not v:
                raise ConfigError(f"{path}: {where}.{k} must be a non-empty string (got {v!r})")
            if any(c in v for c in _SLURM_UNSAFE_CHARS):
                raise ConfigError(f"{path}: {where}.{k} contains shell metacharacters "
                                  f"(SBATCH header injection); refused")

    # caps — optional positive ints (bool is an int subclass; exclude it).
    for k in ("max_cores_per_job", "max_mem_gb_per_job", "max_time_hours_per_job"):
        if k in blk:
            v = blk[k]
            if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
                raise ConfigError(
                    f"{path}: {where}.{k} must be a positive integer (got {v!r})")

    if "gpu" in blk:
        _validate_slurm_gpu_block(blk["gpu"], f"{where}.gpu", path)

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


# --- data_transfer block --------------------------------------------------
#
# Picks the wire protocol every upload_to_X / download_from_X primitive
# uses on this env (also stage_apptainer_image's .tar transfer). Closed-key
# at every level so a typo can't silently revert us to scp without notice.
#
# The two providers:
#   scp_head_node  — the legacy default; scp + sha256 round-trip over ssh.
#                    Fine for tiny files (workflow renders); pisses off the
#                    cluster head node when used for GB-scale .sif images.
#   globus         — encrypted, off-head-node, end-to-end checksummed by
#                    Globus itself. Required for shipping containers and
#                    anything else with real bytes. Hard-errors on failure;
#                    no silent scp fallback (that would defeat the policy).

_DATA_TRANSFER_TYPES: frozenset[str] = frozenset({"scp_head_node", "globus"})

_DATA_TRANSFER_ALLOWED_KEYS: frozenset[str] = frozenset({"type", "globus"})

_GLOBUS_REQUIRED_KEYS: frozenset[str] = frozenset({
    "local_endpoint_id",
    "local_endpoint_name",
    "remote_endpoint_id",
    "remote_endpoint_name",
})

# Globus endpoint IDs are UUIDs. Accept canonical lowercase-hex form.
import re as _re
_UUID_RE = _re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _validate_data_transfer_block(blk: object, where: str, path: Path) -> None:
    """Validate the closed `data_transfer:` block. Unknown keys rejected.
    type ∈ {scp_head_node, globus}. When type=globus, the nested globus
    block is required and its 4 endpoint fields must be valid UUIDs +
    non-empty display names."""
    if not isinstance(blk, dict):
        raise ConfigError(f"{path}: {where} must be a mapping")
    extra = set(blk.keys()) - _DATA_TRANSFER_ALLOWED_KEYS
    if extra:
        raise ConfigError(
            f"{path}: {where} has unknown keys {sorted(extra)!r}. "
            f"Allowed keys: {sorted(_DATA_TRANSFER_ALLOWED_KEYS)!r}")
    if "type" not in blk:
        raise ConfigError(
            f"{path}: {where} missing required key 'type' "
            f"(one of {sorted(_DATA_TRANSFER_TYPES)!r})")
    t = blk["type"]
    if t not in _DATA_TRANSFER_TYPES:
        raise ConfigError(
            f"{path}: {where}.type={t!r} must be one of "
            f"{sorted(_DATA_TRANSFER_TYPES)!r}")
    if t == "globus":
        if "globus" not in blk:
            raise ConfigError(
                f"{path}: {where}.type=globus requires a 'globus:' "
                f"sub-block with endpoint IDs and names")
        g = blk["globus"]
        if not isinstance(g, dict):
            raise ConfigError(f"{path}: {where}.globus must be a mapping")
        missing = _GLOBUS_REQUIRED_KEYS - set(g.keys())
        if missing:
            raise ConfigError(
                f"{path}: {where}.globus missing required keys "
                f"{sorted(missing)!r}")
        extra_g = set(g.keys()) - _GLOBUS_REQUIRED_KEYS
        if extra_g:
            raise ConfigError(
                f"{path}: {where}.globus has unknown keys {sorted(extra_g)!r}. "
                f"Allowed: {sorted(_GLOBUS_REQUIRED_KEYS)!r}")
        for k in ("local_endpoint_id", "remote_endpoint_id"):
            v = g[k]
            if not isinstance(v, str) or not _UUID_RE.match(v):
                raise ConfigError(
                    f"{path}: {where}.globus.{k}={v!r} must be a canonical "
                    f"UUID (lowercase hex with dashes, e.g. "
                    f"'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')")
        for k in ("local_endpoint_name", "remote_endpoint_name"):
            v = g[k]
            if not isinstance(v, str) or not v.strip():
                raise ConfigError(
                    f"{path}: {where}.globus.{k} must be a non-empty string")
    elif "globus" in blk:
        # type=scp_head_node but globus block present — refuse rather than
        # silently ignore. Either rename the type or delete the block.
        raise ConfigError(
            f"{path}: {where}.type=scp_head_node but a 'globus:' block "
            f"is also present. Set type=globus to use it, or delete the "
            f"globus block to keep scp.")


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


def get_container_upload_target(env: dict) -> Optional[dict]:
    """Return the container_upload_target dir-access block for this env,
    or None if undeclared. Third env-level zone — the agent's landing
    pad for .sif container images (and Path-5 recipe.yaml + .def files
    when those land). Block carries `path`, `permissions` (validator
    enforces `upload`), and `description`. Per
    [[project-container-artifacts-routing]] — distinct from
    agent_common_data_target (which is for reference data only)."""
    blk = env.get("container_upload_target")
    return blk if isinstance(blk, dict) else None


def get_slurm_config(env: dict) -> Optional[dict]:
    """Return the OPTIONAL slurm policy block for this env, or None if undeclared.
    All keys optional (see _SLURM_ALLOWED_KEYS): account, partition, the gpu
    convention {partition, qos}, the max_* caps, module_loads. render_workflow_files
    merges these into each job's header. An HPC like Longleaf (no partition, no
    account for CPU jobs) legitimately has no block at all."""
    blk = env.get("slurm")
    return blk if isinstance(blk, dict) else None


def get_data_transfer_kind(env: dict) -> str:
    """Return the data_transfer.type for this env. Defaults to
    'scp_head_node' when the block is absent (back-compat with envs that
    haven't been migrated to Globus yet). For type=local envs this is
    still callable but the value is ignored — local transfers don't go
    through a wire-protocol provider."""
    dt = env.get("data_transfer")
    if not isinstance(dt, dict):
        return "scp_head_node"
    return dt.get("type") or "scp_head_node"


def get_globus_endpoints(env: dict) -> Optional[dict]:
    """Return the validated globus endpoint block for this env, or None
    if data_transfer.type != globus. Block carries the four required
    fields: local_endpoint_id, local_endpoint_name, remote_endpoint_id,
    remote_endpoint_name. Validated at config-load time so a non-None
    return guarantees the shape."""
    dt = env.get("data_transfer")
    if not isinstance(dt, dict) or dt.get("type") != "globus":
        return None
    g = dt.get("globus")
    return g if isinstance(g, dict) else None


# ---------------------------------------------------------------------------
# Phase 2 — env-implicit grant helper
#
# The Phase-2 actuator primitives (the unified `transfer.upload` /
# `transfer.download`, routed to the scratch / common_data /
# container_upload zones) do NOT walk the project's `directories[]`
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
    `directories[]` for the given compute_env. A request for
    `/scratch/x/sub/file` matches `/scratch/x/` (with or without trailing
    slash). MORE SPECIFIC entry wins.

    Permission match: the operation's required permission token MUST appear
    in the matched directory's `permissions:` list. A dir declared
    `permissions: [upload]` does NOT satisfy `snapshot` (which requires
    `file_name_only`). Multiple tokens on the same dir are allowed:
    `permissions: [file_name_only, upload]` satisfies both.

    `path` MUST be an absolute path string; otherwise the match is rejected
    immediately — relative paths have no meaning at the security boundary. A
    '..' segment is refused outright (before any prefix match), and the path is
    lexically normalized before matching, so this function is safe to call on an
    un-pre-validated path. It does NOT resolve symlinks — remote POSIX perms
    own that.

    NOTE: scratch + common_data zones are env-level (declared on the
    compute_env, not on the project). They do NOT route through this
    function — use `check_env_target_capability` instead. Cluster
    validation (the agent's own sandbox) goes through scratch auth;
    `check_permission` is exclusively for project-declared
    `directories[]` paths."""
    if operation not in OPERATION_REQUIRES:
        raise PermissionDenied(f"unknown operation: {operation!r}")
    required = OPERATION_REQUIRES[operation]

    if not isinstance(path, str) or not path.startswith("/"):
        raise PermissionDenied(
            f"path must be an absolute string, got {path!r}")

    # Traversal must die HERE, not in whatever the caller happened to run first.
    # This function's docstring says every primitive must call it before touching
    # a compute_env — so it has to hold on its own. It did not: a plain prefix
    # match on the RAW string authorized '/proj/safe/../../etc/passwd' against a
    # '/proj/safe' grant, because the string does start with the prefix even
    # though the path it names is /etc/passwd. Nothing exploited it (every caller
    # today pre-validates separately), which is exactly the trap: the gate that
    # CLAIMED authority was leaning on a check it does not own, and one caller
    # forgetting that check is a full escape. Fail closed on the shape, then
    # match on the NORMALIZED path so '/a/./b' still resolves to '/a/b'.
    #
    # NB: normpath is lexical — it does not resolve symlinks. A symlink out of an
    # authorized dir is the REMOTE filesystem's call to make, not ours; POSIX
    # perms on the far side are the backstop.
    import posixpath
    if any(seg == ".." for seg in path.split("/")):
        raise PermissionDenied(
            f"path {path!r} contains a '..' traversal segment — refused before "
            f"any prefix match. Pass the real absolute path you mean.")

    # Normalize for comparison (collapse '.', duplicate slashes; strip trailing
    # slash so '/a/b' and '/a/b/' match the same entry).
    req_norm = posixpath.normpath(path).rstrip("/") or "/"

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
