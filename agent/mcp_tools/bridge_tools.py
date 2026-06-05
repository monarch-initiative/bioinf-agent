"""bridge_tools — HPC bridge actuator surface (Phase 2+).

Sibling to observability_tools.py (Phase 1's read-only `snapshot_project`).
While observability is pure-read, this module's primitives push bytes,
submit jobs, monitor them, and fetch results back — all under the same
permission gate (`compute_access.check_permission`) and the same
ControlMaster ssh pattern.

Today the surface is:
  upload_to_scratch / fetch_from_scratch  — Step 2 (paired, sha256
                                            round-trip on every transfer)

Coming as each step lands:
  upload_to_refdata                       — Step 3
  submit_cluster_job                      — Steps 4, 6, 7 (diagnostic →
                                            data_acquisition → run modes)
  cluster_job_status                      — Step 5

Authorization shape (identical to Phase 1):
  - `project_name` + `compute_env_name` resolve to a project's
    compute_env_access entry
  - The agent-supplied path component (`remote_subpath`) is pure-string
    validated BEFORE any I/O (no traversal, no shell metacharacters,
    no absolute path leak)
  - The resolved absolute path goes through
    `compute_access.check_permission(project, env, abs_path, operation)`
    which requires the project's `directories[]` to grant the operation's
    permission token

All cheat-guards live under
`tests/integration/honesty/L14_compute_env_safety/`.
"""
from __future__ import annotations

# IMPORT-BINDING (see feedback-mcp-tools-conventions): singletons go through
# `_ms.X` so test monkeypatching on mcp_server attribute names reaches us.
# `mcp` is the FastMCP app and is never monkeypatched, so a bare import is
# safe. Same shape as every other agent/mcp_tools/ submodule.
from pathlib import Path

from agent import mcp_server as _ms
from agent.mcp_server import mcp  # FastMCP app, never monkeypatched


def _resolve_access_path() -> str | None:
    """Return the path to projects_access.yaml as a string, preferring the
    repo-root convention used in this codebase (the user's live file lives
    alongside the source tree), falling back to ~/.bioinf/. Returns None if
    neither exists — the primitive then surfaces a clean FileNotFoundError.

    Same resolution that `agent_status` uses; kept inline here so this
    submodule's MCP wrappers don't grow a cross-submodule import."""
    from agent.skills import compute_access as _ca
    repo_root = _ms._env_mgr.project_root
    candidate = repo_root / "projects_access.yaml"
    if candidate.exists():
        return str(candidate)
    default = _ca.default_access_path()
    return str(default) if default.exists() else None


@mcp.tool()
def upload_to_scratch(project_name: str,
                     compute_env_name: str,
                     local_path: str,
                     remote_subpath: str) -> dict:
    """Push a local file into the project's authorized scratch sandbox on
    a compute env. sha256 round-trip is verified; mismatch refuses.

    Authorization: the project must declare a `directories[]` entry on
    `compute_env_name` whose `permissions` include `upload` and whose
    path contains the resolved destination (longest-prefix match). The
    env must declare an `agent_scratch_target` block; `remote_subpath`
    is RELATIVE to that scratch root.

    `remote_subpath` rules: non-empty, ≤255 chars, no leading '/', no
    '..' segments, no shell metacharacters (newline / `;` / `|` / `$` /
    backticks / etc.), no whitespace. Resolved path must normalize
    INSIDE the scratch root (defense-in-depth against symlink trickery).

    `local_path`: must exist, be a REGULAR file (symlinks refused —
    defense against user's home symlink redirecting to /etc/shadow),
    and be under the 5 GiB head-node cap. Anything larger should go
    through `submit_cluster_job(job_type='data_acquisition')` (Step 6)
    which curl-resumes inside a SLURM job, or eventually Globus.

    Returns {success, compute_env, remote_path, sha256, bytes,
    duration_s, transferred_at} on success; {"error": "..."} on any
    refusal or transfer failure (no exception escapes to the caller —
    the MCP surface is dict-in-dict-out)."""
    from agent.skills import scratch
    return scratch.upload_to_scratch(
        project_name=project_name,
        compute_env_name=compute_env_name,
        local_path=local_path,
        remote_subpath=remote_subpath,
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def fetch_from_scratch(project_name: str,
                      compute_env_name: str,
                      remote_subpath: str,
                      local_path: str) -> dict:
    """Pull a file from the project's authorized scratch sandbox back to
    a local path. sha256 round-trip is verified BEFORE declaring success;
    on mismatch the partial local file is removed and an error is
    returned.

    Symmetric to `upload_to_scratch` — same authorization shape with
    `fetch` (instead of `upload`) as the required permission. The
    project's `directories[]` entry for the scratch root must include
    `fetch`. A permission for `upload` alone does NOT satisfy `fetch`
    (discrete capabilities, not a lattice — see
    `compute_access.OPERATION_REQUIRES`).

    `local_path` rules: must NOT exist yet (no silent overwrites; the
    same never-overwrite contract upload uses on the remote side). Its
    parent directory must exist and be writable.

    Returns {success, compute_env, remote_path, local_path, sha256,
    bytes, duration_s, fetched_at} on success; {"error": "..."} on
    refusal/failure."""
    from agent.skills import scratch
    return scratch.fetch_from_scratch(
        project_name=project_name,
        compute_env_name=compute_env_name,
        remote_subpath=remote_subpath,
        local_path=local_path,
        access_path=_resolve_access_path(),
    )
