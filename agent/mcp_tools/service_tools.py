"""service_tools — service-dependency lifecycle + GPU probe.

start_service / stop_service / check_service_health / verify_service_dependency
manage the long-running companions a tool may need (Redis, Postgres, Spark,
web servers). They satisfy I10 (every declared service_dependency has at least
one healthy probe in its log).

check_gpu is the read-only NVIDIA probe used by the agent and by freeze() to
decide whether to install GPU deps. Sits here because it's the same "is this
host capable?" family.
"""
from __future__ import annotations

import subprocess
from typing import Optional

# IMPORT-BINDING: see workflow_tools.py — singletons go through `_ms.X`
# so test monkeypatching on mcp_server reaches us.
from agent import mcp_server as _ms
from agent.mcp_server import mcp  # FastMCP app, never monkeypatched


@mcp.tool()
def check_gpu() -> dict:
    """Check if an NVIDIA GPU is available for GPU-accelerated tools.

    Returns differ by outcome:
      Available:    {available: True, gpus: [{name, driver_version, memory_mb}, ...], cuda_version}
      Unavailable:  {available: False, reason: "<why>", fallback: "Use CPU mode for testing"}

    Use the `available` field to decide whether to install GPU deps and to set
    `runtime_environment.gpu_required` on the spec. If False, fall back to CPU
    mode (most tools expose `--device cpu`)."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return {"available": False, "reason": "nvidia-smi not found", "fallback": "Use CPU mode for testing"}
    if r.returncode != 0:
        return {"available": False, "reason": r.stderr.strip()[:200], "fallback": "Use CPU mode for testing"}

    gpus = []
    for line in r.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            gpus.append({"name": parts[0], "driver_version": parts[1], "memory_mb": parts[2]})

    # Extract CUDA version from nvidia-smi header line
    header = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=10)
    cuda_ver = ""
    for ln in header.stdout.splitlines():
        if "CUDA Version:" in ln:
            cuda_ver = ln.split("CUDA Version:")[-1].strip().split()[0]
            break

    return {"available": bool(gpus), "gpus": gpus, "cuda_version": cuda_ver}


@mcp.tool()
def start_service(
    env_name: str,
    service_name: str,
    start_command: str,
    health_check_command: str,
    health_check_timeout_seconds: int = 30,
    working_dir: str = "",
    env_vars: dict[str, str] = {},
    pipeline_id: str = "",
    service_type: str = "custom",
    stop_command: str = "",
    port: int = 0,
    version: str = "",
) -> dict:
    """Start a background service (web server, database, Spark, cache, …) inside
    a conda env. Polls health_check_command until healthy or timeout.

    If pipeline_id is supplied, the service is registered into the draft as a
    ServiceDependency: the declaration (start_command, stop_command,
    health_check_command, port, version, type) is recorded, the initial
    readiness probe is appended to health_check_log, and status is set to
    `running` (or `failed` if the probe never succeeds).

    `service_dependencies` is BLOCKED from patch_pipeline; the only path to
    declare a service in the spec is through this primitive (or
    verify_service_dependency for follow-up probes). I10 requires every
    declared dependency to have ≥1 healthy probe in its log.

    Returns: {success, pid, log, health_probe?}.
    """
    from datetime import datetime, timezone

    result = _ms._env_mgr.start_service(
        env_name, service_name, start_command, health_check_command,
        health_check_timeout_seconds=health_check_timeout_seconds,
        working_dir=working_dir or None,
        env_vars=env_vars or None,
    )

    if pipeline_id:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        # Always run one explicit probe + record it (success OR failure). When
        # start_service times out, the inner loop's last probe wasn't returned,
        # so we re-probe here to make the failure case provable too.
        probe_raw = _ms._env_mgr.check_service_health(env_name, health_check_command, working_dir=working_dir or None)
        probe = {
            "timestamp":      now,
            "command":        health_check_command,
            "returncode":     int(probe_raw["returncode"]) if probe_raw.get("returncode") is not None else 1,
            "healthy":        bool(probe_raw.get("healthy")),
            "output_excerpt": ((probe_raw.get("stdout") or "") + (probe_raw.get("stderr") or ""))[-500:],
        }
        pid_int: Optional[int] = None
        if result.get("pid"):
            try:
                pid_int = int(result["pid"])
            except (TypeError, ValueError):
                pid_int = None
        fields = {
            "type":                 service_type or "custom",
            "version":              version or None,
            "start_command":        start_command,
            "stop_command":         stop_command or "",
            "health_check_command": health_check_command,
            "health_check_timeout_seconds": health_check_timeout_seconds,
            "port":                 port or None,
            "env_vars":             env_vars or {},
            "pid":                  pid_int,
            "started_at":           now,
            # Restarting a previously stopped service clears the prior
            # stopped_at — without this, the spec shows status=running with a
            # stale stopped_at timestamp, which is contradictory.
            "stopped_at":           None,
            "status":               "running" if (result.get("success") and probe["healthy"]) else "failed",
            "health_check_log":     [probe],
        }
        _ms._pipeline_state.upsert_service_dependency(pipeline_id, service_name, fields)
        result["pipeline_merge"] = {
            "status":         "merged",
            "pipeline_id":    pipeline_id,
            "service_name":   service_name,
            "probe_healthy":  probe["healthy"],
        }
        result["health_probe"] = probe

    return result


@mcp.tool()
def stop_service(
    env_name: str,
    service_name: str,
    stop_command: str = "",
    pipeline_id: str = "",
) -> dict:
    """Stop a background service started with start_service.
    Prefers stop_command if provided; falls back to killing by PID file.

    If pipeline_id is supplied, the spec's service_dependency entry is updated
    with status=stopped and stopped_at timestamp."""
    from datetime import datetime, timezone

    result = _ms._env_mgr.stop_service(env_name, service_name, stop_command=stop_command)
    if pipeline_id:
        _ms._pipeline_state.upsert_service_dependency(
            pipeline_id, service_name,
            {
                "status":     "stopped" if result.get("success") else "failed",
                "stopped_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            },
        )
        result["pipeline_merge"] = {
            "status":       "merged",
            "pipeline_id":  pipeline_id,
            "service_name": service_name,
        }
    return result


@mcp.tool()
def check_service_health(
    env_name: str,
    health_check_command: str,
    working_dir: str = "",
) -> dict:
    """Run a health-check command to verify a background service is responding.
    Returns: healthy (bool), returncode, stdout, stderr.

    For recording the probe into a pipeline spec (so I10 is satisfied), use
    verify_service_dependency instead — this primitive is unrecorded by design
    (debug / one-off probe)."""
    return _ms._env_mgr.check_service_health(env_name, health_check_command, working_dir=working_dir or None)


@mcp.tool()
def verify_service_dependency(
    pipeline_id: str,
    service_name: str,
    env_name: str,
    health_check_command: str = "",
    working_dir: str = "",
) -> dict:
    """Probe a declared service and append the observation to its
    health_check_log. The honest companion to start_service.

    If `health_check_command` is empty, the command recorded on the existing
    service_dependency entry is reused — so repeat probes are easy.

    I10 — at finalize, every service_dependency must have ≥1 entry in its
    health_check_log with healthy=true. start_service records one such probe
    on successful start; use this primitive to record additional probes
    (mid-pipeline checkpoint, post-step verification, recovery after a flap).

    Returns: {success, healthy, returncode, output, probe_recorded}.
    """
    from datetime import datetime, timezone

    draft = _ms._pipeline_state.get_draft(pipeline_id)
    if draft is None:
        return {"error": f"unknown pipeline_id: {pipeline_id}"}

    existing_cmd = ""
    for d in draft.get("service_dependencies", []) or []:
        if isinstance(d, dict) and d.get("name") == service_name:
            existing_cmd = d.get("health_check_command") or ""
            break

    cmd = health_check_command or existing_cmd
    if not cmd:
        return {
            "error": (
                f"service '{service_name}' has no recorded health_check_command "
                f"and none was supplied. Call start_service first, or pass "
                f"health_check_command explicitly."
            ),
        }

    probe_raw = _ms._env_mgr.check_service_health(env_name, cmd, working_dir=working_dir or None)
    probe = {
        "timestamp":      datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "command":        cmd,
        "returncode":     int(probe_raw["returncode"]) if probe_raw.get("returncode") is not None else 1,
        "healthy":        bool(probe_raw.get("healthy")),
        "output_excerpt": ((probe_raw.get("stdout") or "") + (probe_raw.get("stderr") or ""))[-500:],
    }
    idx = _ms._pipeline_state.upsert_service_dependency(
        pipeline_id, service_name,
        {"health_check_log": [probe]},
    )
    return {
        "success":         True,
        "healthy":         probe["healthy"],
        "returncode":      probe["returncode"],
        "output":          probe["output_excerpt"],
        "probe_recorded":  True,
        "pipeline_merge":  {
            "status":       "merged" if idx is not None else "no_op",
            "pipeline_id":  pipeline_id,
            "service_name": service_name,
        },
    }
