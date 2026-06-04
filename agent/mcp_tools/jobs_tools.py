"""jobs_tools — JobManager surface for background subprocess lifecycle.

The four-call shape for anything that can run silently >10 minutes:
`run_in_background` to spawn → `check_job` to poll → `cancel_job` to stop →
`list_jobs` to enumerate. Used for big downloads, long conda solves, multi-
hour assemblies, and any background `freeze` (via `_freeze_in_background` in
mcp_server.py).
"""
from __future__ import annotations

# IMPORT-BINDING: see workflow_tools.py — singletons go through `_ms.X`
# so test monkeypatching on mcp_server reaches us.
from agent import mcp_server as _ms
from agent.mcp_server import mcp  # FastMCP app, never monkeypatched


# ---------------------------------------------------------------------------
# Async background job tools — watchdog-proof execution
#
# Use these for any operation that may run silently for >5 minutes:
#   - large downloads (Exomiser data bundles, BLAST nt, full genome FASTAs)
#   - long conda solves on big dependency graphs
#   - multi-hour assembler / aligner runs on real data
#   - any tool whose normal output stream is sparse
#
# Pattern:
#   r = run_in_background("curl -L --progress-bar -o big.zip 'URL' && unzip big.zip", env_name="bioinf_x")
#   # job_id returned immediately; agent does other work
#   while check_job(r['job_id'])['state'] == 'running':
#       sleep(30)   # or do unrelated tool calls
#   final = check_job(r['job_id'])
# ---------------------------------------------------------------------------


@mcp.tool()
def run_in_background(
    command: str,
    env_name: str = "",
    job_id: str = "",
    working_dir: str = "",
) -> dict:
    """Spawn `command` as a background process. Returns immediately.

    Use this for any operation that may run silently for more than 5 minutes —
    the agent stream-watchdog kills tool calls that go silent that long. Big
    downloads, conda solves, assembler runs, etc.

    Arguments:
      command:     full shell command (bash -c executes it; pipes / redirects OK)
      env_name:    if set, runs inside that conda env via `conda run --prefix`
      job_id:      caller-supplied (must be unique among running jobs).
                   If empty, a 12-char hex ID is auto-generated.
      working_dir: subprocess cwd (default: project root)

    Returns:
      {job_id, status_path, log_path, pid, state="running"}

    Use `check_job(job_id)` to poll. The status file is durable — survives
    server restarts and is queryable indefinitely after the job ends.
    """
    return _ms._job_manager.start(
        command, env_name=env_name, job_id=job_id, working_dir=working_dir,
    )


@mcp.tool()
def check_job(job_id: str, log_tail_lines: int = 30) -> dict:
    """Return the current state of a background job. Non-blocking; ~50 ms.

    States: running | exited | cancelled
    Always includes a `log_tail` (last N lines of combined stdout+stderr) so you
    can monitor progress (e.g. curl's progress bar) without reading the full log
    file. `bytes_logged` is the size of the log so far — a download's progress
    can be inferred from this.

    For a job that has just exited, this is also where you learn it terminated —
    the state flips from "running" to "exited" on the first check after the
    subprocess died.
    """
    return _ms._job_manager.check(job_id, log_tail_lines=log_tail_lines)


@mcp.tool()
def cancel_job(job_id: str, force: bool = False) -> dict:
    """Terminate a running job. SIGTERM by default; force=True sends SIGKILL.

    Always signals the whole process group so children (e.g. unzip launched
    after curl in a chained command) also die. A SIGTERM is followed by a 5 s
    grace window before promoting to SIGKILL.
    """
    return _ms._job_manager.cancel(job_id, force=force)


@mcp.tool()
def list_jobs(include_terminated: bool = True) -> dict:
    """List all jobs ever started on this machine, newest first.

    include_terminated: when False, only currently-running jobs are returned.
    """
    return {"jobs": _ms._job_manager.list_jobs(include_terminated=include_terminated)}
