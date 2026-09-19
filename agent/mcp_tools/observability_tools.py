"""observability_tools — pure-read snapshot surfaces.

Two tools that answer "what's currently going on?" — one for the local agent
state across all subsystems (`agent_status`), one for the user's cluster
project tree (`snapshot_project`). Both are zero-mutation.
"""
from __future__ import annotations

from typing import Optional

# IMPORT-BINDING: see workflow_tools.py — singletons go through `_ms.X`
# so test monkeypatching on mcp_server reaches us.
from agent import mcp_server as _ms
from agent.mcp_server import mcp  # FastMCP app, never monkeypatched
from agent.skills.outcomes import refused
@mcp.tool()
def snapshot_project(project_name: str, path: Optional[str] = None,
                     name_glob: Optional[str] = None,
                     max_entries: int = 20000) -> dict:
    """List a project's authorized directories — across every compute env it
    spans — as a file-tree snapshot tagged by env. Each entry is
    {compute_env, path, size, mtime, type}. Two modes:

    OVERVIEW (no `path`): every authorized dir at ONE level — root + its
    immediate children, subdirs by name. The cheap orientation call.

    DEEP LISTING (`path=` an absolute path under any granted dir): the
    RECURSIVE listing of that subtree. `name_glob` filters by basename
    ('*.fastq.gz' turns an 11k-sample tree into exactly the sample files);
    `max_entries` caps ONE CALL's output (default 20000) and is raisable
    without ceiling — a truncated result says `truncated: true` and names
    the remedy, so a complete sweep is always reachable and never silently
    short. Building a sample sheet: deep-list with a glob, raise the cap if
    `truncated`, then write the CSV from `entries[].path`.

    This is the read-only INSPECTION primitive for a user's compute env —
    `upload` / `download` / `submit_workflow_job` / `run_step_on_cluster` are
    the actuators. The shell that runs here is fixed: `find` with a pinned
    printf template (plus `-name <glob> | head -n <cap>` in deep mode) over
    ssh; local envs use no subprocess at all. No file contents are read; no
    other commands are reachable.

    Authorization lives at the PROJECT level: each project's flat
    `directories[]` list (entries tagged with `env:`) names the dirs the
    agent may touch, with explicit `permissions:` tokens (see
    compute_access.PERMISSIONS). Listing requires `file_name_only`; a
    `path` not under any such grant (or under another project's zone
    namespace) raises PermissionDenied before any shell runs.

    See `agent/skills/projects_access.yaml.example` for the schema; see
    `tests/integration/honesty/L14_compute_env_safety/` for the contract
    tests pinning what this primitive can and cannot do.
    """
    from agent.skills import snapshot
    from agent.skills.compute_access import PermissionDenied, ConfigError
    try:
        return snapshot.snapshot_project(project_name, path=path,
                                         name_glob=name_glob,
                                         max_entries=max_entries)
    except (PermissionDenied, ConfigError, FileNotFoundError, KeyError) as e:
        return refused("status.snapshot_gate_rejected", error=f"{type(e).__name__}: {e}")


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
    access_path = _compute_access.default_access_path()
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
