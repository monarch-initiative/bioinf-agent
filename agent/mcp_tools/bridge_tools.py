"""bridge_tools — HPC bridge actuator surface (Phase 2+).

Sibling to observability_tools.py (Phase 1's read-only `snapshot_project`).
While observability is pure-read, this module's primitives push bytes,
submit jobs, monitor them, and fetch results back — all under the same
permission gate (`compute_access.check_permission`) and the same
ControlMaster ssh pattern.

Today the surface is:
  upload_to_scratch / download_from_scratch          — Step 2 (paired,
                                                       sha256 round-trip)
  upload_to_common_data / download_from_common_data  — Step 3 (shared
                                                       namespace; no
                                                       project prefix)

Coming as each step lands:
  cluster_job_status                                 — Step 5
  submit_data_acquisition_job                        — Step 6
  submit_workflow_job                                — Step 8

Authorization shape (env-implicit grant, Phase 2):
  - `project_name` + `compute_env_name` resolve to a project's
    compute_env_access entry (the project must have ACCESS to the env)
  - The env declares `agent_scratch_target` / `agent_common_data_target`
    blocks at the env level; their `permissions:` lists the supported
    capabilities — these are the GRANT (no per-project re-declaration)
  - Multi-project isolation: the resolved path is auto-prefixed with
    `project_name` (`<target>/<project>/<remote_subpath>`)
  - The agent-supplied path component (`remote_subpath`) is pure-string
    validated BEFORE any I/O

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
    """Push a local file into the agent's scratch sandbox on a compute env,
    under THIS project's auto-prefixed namespace. sha256 round-trip is
    verified; mismatch refuses.

    Authorization (env-implicit): the project must have a `compute_env_access`
    entry naming `compute_env_name`, AND the env must declare an
    `agent_scratch_target` block whose `permissions` include `upload`.
    The project's `directories[]` is NOT consulted — that list is for
    project-specific paths only.

    Multi-project isolation: the resolved path is auto-prefixed with
    project_name. A call to upload_to_scratch('proj_a', ..., 'x.txt')
    lands at `<scratch.path>/proj_a/x.txt`; 'proj_b' lands elsewhere.

    `remote_subpath` rules: non-empty, ≤255 chars, no leading '/', no
    '..' segments, no shell metacharacters (newline / `;` / `|` / `$` /
    backticks / etc.), no whitespace.

    `project_name` rules: safe token (alnum + '_-', ≤64 chars) — used
    as the auto-prefix path component.

    `local_path`: must exist, be a REGULAR file (symlinks refused —
    defense against user's home symlink redirecting to /etc/shadow),
    and be under the 5 GiB head-node cap. Anything larger should go
    through `submit_cluster_job(job_type='data_acquisition')` (Step 6)
    which curl-resumes inside a SLURM job, or eventually Globus.

    Returns {success, compute_env, remote_path, sha256, bytes,
    duration_s, transferred_at} on success; {"error": "..."} on any
    refusal or transfer failure (no exception escapes — the MCP surface
    is dict-in-dict-out)."""
    from agent.skills import scratch
    return scratch.upload_to_scratch(
        project_name=project_name,
        compute_env_name=compute_env_name,
        local_path=local_path,
        remote_subpath=remote_subpath,
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def upload_to_common_data(project_name: str,
                         compute_env_name: str,
                         local_path: str,
                         remote_subpath: str) -> dict:
    """Push a local file into the env's SHARED common-data zone.

    Authorization (env-implicit): project has compute_env_access for the
    env; env declares an `agent_common_data_target` block whose
    `permissions` include `upload`. The project's directories[] is NOT
    consulted.

    No project auto-prefix — common_data is intentionally SHARED across
    projects so reference data can be mixed and matched. The resolved
    path is `<common_data.path>/<remote_subpath>` directly.

    Overwrite refusal: if the resolved path already exists, the primitive
    refuses to upload. Reference data is versioned (e.g.
    `exomiser/v3.2.0/data.zip`), not silently replaced. Delete remotely
    before uploading a fresh version.

    Same path-safety + sha256-round-trip + 5 GiB head-node cap rules as
    upload_to_scratch.

    Returns {success, compute_env, remote_path, sha256, bytes, duration_s,
    transferred_at} on success; {"error": "..."} on refusal/failure."""
    from agent.skills import common_data
    return common_data.upload_to_common_data(
        project_name=project_name,
        compute_env_name=compute_env_name,
        local_path=local_path,
        remote_subpath=remote_subpath,
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def download_from_common_data(project_name: str,
                             compute_env_name: str,
                             remote_subpath: str,
                             local_path: str) -> dict:
    """Pull a file from the env's SHARED common-data zone back to local.

    Symmetric to `upload_to_common_data` — same env-implicit auth with
    `download` (instead of `upload`) as the required capability on the
    env's `agent_common_data_target.permissions`. Discrete capabilities,
    not a lattice — `upload` alone does NOT satisfy `download`.

    No project auto-prefix; any project with env access can read any
    file in common_data.

    `local_path`: must NOT exist (no overwrite); parent must be writable.

    Returns {success, compute_env, remote_path, local_path, sha256,
    bytes, duration_s, fetched_at} on success; {"error": "..."} on
    refusal/failure."""
    from agent.skills import common_data
    return common_data.download_from_common_data(
        project_name=project_name,
        compute_env_name=compute_env_name,
        remote_subpath=remote_subpath,
        local_path=local_path,
        access_path=_resolve_access_path(),
    )


@mcp.tool()
def download_from_scratch(project_name: str,
                         compute_env_name: str,
                         remote_subpath: str,
                         local_path: str) -> dict:
    """Pull a file from THIS project's scratch namespace back to a local
    path. sha256 round-trip is verified BEFORE declaring success; on
    mismatch the partial local file is removed and an error is returned.

    Symmetric to `upload_to_scratch` — same env-implicit authorization
    with `download` (instead of `upload`) as the required capability on
    the env's `agent_scratch_target.permissions`. Discrete capabilities,
    not a lattice — `upload` alone does NOT satisfy `download`.

    Multi-project isolation: the resolved path is auto-prefixed with
    project_name. This project sees only its OWN namespace; cross-project
    visibility requires another project.

    `local_path` rules: must NOT exist yet (no silent overwrites; the
    same never-overwrite contract upload uses on the remote side). Its
    parent directory must exist and be writable.

    Returns {success, compute_env, remote_path, local_path, sha256,
    bytes, duration_s, fetched_at} on success; {"error": "..."} on
    refusal/failure."""
    from agent.skills import scratch
    return scratch.download_from_scratch(
        project_name=project_name,
        compute_env_name=compute_env_name,
        remote_subpath=remote_subpath,
        local_path=local_path,
        access_path=_resolve_access_path(),
    )
