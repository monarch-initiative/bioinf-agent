"""observability_tools — pure-read snapshot surfaces.

Two tools that answer "what's currently going on?" — one for the local agent
state across all subsystems (`agent_status`), one for the user's cluster
project tree (`snapshot_project`). Both are zero-mutation.
"""
from __future__ import annotations

# IMPORT-BINDING: see workflow_tools.py — singletons go through `_ms.X`
# so test monkeypatching on mcp_server reaches us.
from agent import mcp_server as _ms
from agent.mcp_server import mcp  # FastMCP app, never monkeypatched
@mcp.tool()
def snapshot_project(project_name: str) -> dict:
    """Walk a project's authorized directories — across every compute env it
    spans — and return a file-tree snapshot tagged by env. Each entry is
    {compute_env, path, size, mtime, type}; one-level visibility per declared
    directory (no recursion).

    This is the *only* primitive the agent has against a user's compute env
    today. The shell that runs is fixed: `find <authorized_path> -maxdepth 1
    -printf '...'` locally (or `ssh <user>@<host> "find …"` remotely). No
    file contents are read; no other commands are reachable.

    Authorization lives at the PROJECT level: each project's
    `compute_env_access[].directories[]` block lists the dirs the agent may
    walk on each env, with explicit `permissions:` (file_name_only / upload).
    A dir not in that allowlist, or declared with permissions that don't
    include `file_name_only`, raises PermissionDenied before any shell runs.

    See `agent/skills/projects_access.yaml.example` for the schema; see
    `tests/integration/honesty/L14_compute_env_safety/` for the contract
    tests pinning what this primitive can and cannot do.
    """
    from agent.skills import snapshot
    from agent.skills.compute_access import PermissionDenied, ConfigError
    try:
        return snapshot.snapshot_project(project_name)
    except (PermissionDenied, ConfigError, FileNotFoundError, KeyError) as e:
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool()
def agent_status() -> dict:
    """Where am I in this workflow? A pure-read snapshot of every subsystem
    the agent maintains state in — pipeline drafts in flight, frozen envs +
    deliverables, sealed workflows, core test data counts, compute-env
    bridge _ms.config + ssh tunnel state, background jobs, repo state.

    Use this when:
      - you (or the user) ask "what state am I in?"
      - the user mentions a primitive but it's not clear if Layer 0/1/2 ran
      - debugging why a pipeline isn't finding test data / a frozen env
      - checking if the cluster ssh tunnel is still alive before a snapshot

    No mutation, no LLM-tier work. Each subsystem query is fault-tolerant:
    a corrupt manifest degrades to an `{"error": "..."}` in that slice
    rather than crashing the whole call."""
    from agent.skills.agent_status import agent_status as _agent_status
    from agent.skills import compute_access as _compute_access
    # Prefer the repo-root projects_access.yaml (the live file convention
    # the user is using); fall back to the canonical homedir path.
    repo_root = _ms._env_mgr.project_root
    candidate = repo_root / "projects_access.yaml"
    access_path = candidate if candidate.exists() else _compute_access.default_access_path()
    if not access_path.exists():
        access_path = None
    return _agent_status(
        pipeline_state=_ms._pipeline_state,
        env_cache=_ms._env_cache,
        job_manager=_ms._job_manager,
        config=_ms.config,
        access_path=access_path,
        include_repo=True,
    )
