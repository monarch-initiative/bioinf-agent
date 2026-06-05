"""
cluster_job_status — query SLURM job state on a compute env so the
agent can poll a submitted job to completion.

Why this earns its place:
  - Phase 2's submit_workflow_job (Step 9) returns a SLURM job_id.
    Without a status primitive, the agent can't tell whether the job
    is pending, running, or done — it would have to ssh manually.
  - sacct is the canonical record: covers running AND completed jobs
    (slurmdbd-backed), and supports pipe-delimited machine output.
    squeue only sees the live queue — strictly less useful here.
  - cluster_job_status is read-only, ssh-only, doesn't submit or
    cancel anything. Same trust posture as cluster_module_avail.

The shell surface
-----------------
`sacct -j <job_id> -P --noheader -X -o <fields>` — one row per job
(or per array task with -X), pipe-delimited, no header. The field
list is FIXED so the parser doesn't drift if sacct's default columns
change between SLURM versions.

Authorization
-------------
project must have a `compute_env_access` entry for `compute_env_name`.
No per-directory permission needed — we're not touching the
filesystem. SLURM's own ACL limits visibility to the user's own jobs.

Job-id input
------------
A SLURM job_id is digits, optionally with `_<task>` for array jobs.
We validate against `^\\d{1,12}(_\\d{1,12})?$` BEFORE building the
remote command, so a smuggled `12345; rm -rf /` never reaches ssh.
"""
from __future__ import annotations

import re
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.skills import compute_access
from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint


# Fields we ask sacct for, in order. Keep this fixed — the parser
# depends on the column ordering matching this tuple exactly.
_SACCT_FIELDS = ("JobID", "State", "Elapsed", "ExitCode",
                 "NodeList", "Reason", "Start", "End")

# A SLURM job_id token: digits, optionally `_<task>` for array tasks.
# Bounded at 12 digits each (longer than any cluster's actual ids).
_JOB_ID_RE = re.compile(r"^\d{1,12}(_\d{1,12})?$")


def _validate_job_id(job_id: str) -> str:
    """job_id is required, must be a safe token. Refuses everything that
    isn't digits + optional `_<task>` — so an attacker can't smuggle a
    shell metacharacter through `sacct -j <smuggled>`."""
    if not isinstance(job_id, str):
        raise ValueError(
            f"job_id must be a string, got {type(job_id).__name__}")
    if not job_id:
        raise ValueError("job_id is required (got empty string)")
    if not _JOB_ID_RE.match(job_id):
        raise ValueError(
            f"job_id {job_id!r} is malformed — expected digits with "
            f"an optional `_<task>` suffix (refused before any ssh)")
    return job_id


def _build_sacct_cmd(job_id: str) -> str:
    """The remote shell command. Login shell so sacct is on PATH on
    systems that add it via /etc/profile.d/slurm.sh. The field list is
    fixed; pinned by a test."""
    fields = ",".join(_SACCT_FIELDS)
    inner = f"sacct -j {shlex.quote(job_id)} -P --noheader -X -o {fields}"
    return f"bash -lc {shlex.quote(inner)}"


def _parse_sacct_output(text: str) -> list[dict]:
    """Parse pipe-delimited sacct output into a list of row-dicts.

    sacct -P uses '|' as the column separator. With --noheader, each
    non-empty line is one job (or array-task with -X) row. Empty
    output → empty list (job not in slurmdbd yet, or never existed).

    Defensively skips rows whose column count doesn't match
    `_SACCT_FIELDS` — sacct on a misbehaving cluster could emit a
    truncated row; we'd rather drop it than crash."""
    rows: list[dict] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) != len(_SACCT_FIELDS):
            continue
        # Build a row dict with snake_case keys; sacct's exit_code is
        # the `0:0` `<rc>:<signal>` shape — preserved verbatim.
        rows.append({
            "job_id":    parts[0],
            "state":     parts[1],
            "elapsed":   parts[2],
            "exit_code": parts[3],
            "nodelist":  parts[4],
            "reason":    parts[5],
            "start":     parts[6],
            "end":       parts[7],
        })
    return rows


def cluster_job_status(project_name: str,
                       compute_env_name: str,
                       job_id: str,
                       *,
                       access_path: Optional[str] = None,
                       timeout: int = 60) -> dict:
    """Look up SLURM state for `job_id` on `compute_env_name`.

    Pure-read: runs ONE ssh invocation of
    `bash -lc 'sacct -j <id> -P --noheader -X -o <fields>'`, parses
    the pipe-delimited output, returns a list of row-dicts (one per
    job, plus one per array-task for array jobs).

    Authorization: project must have a `compute_env_access` entry for
    `compute_env_name`. No per-directory permission needed. SLURM's
    own ACL keeps visibility scoped to the user's own jobs.

    Returns:
      {
        "compute_env": "<env_name>",
        "job_id":      "<requested_id>",
        "jobs":        [{job_id, state, elapsed, exit_code, nodelist,
                          reason, start, end}, ...],
        "captured_at": "<iso utc>",
      }
    Returns {"error": "...", "hint": "..."} on any failure.
    Returns `jobs=[]` when sacct doesn't recognize the id — caller
    distinguishes "not yet in slurmdbd" from "never existed" with a
    short retry (sacct catches up within seconds of submission).
    """
    try:
        norm_id = _validate_job_id(job_id)

        access = compute_access.load_access(
            Path(access_path) if access_path else None)
        project = compute_access.get_project(project_name, access)
        env = compute_access.get_compute_env(compute_env_name, access)

        has_access = any(
            isinstance(b, dict) and b.get("compute_env") == compute_env_name
            for b in (project.get("compute_env_access") or []))
        if not has_access:
            return {"error":
                f"PermissionDenied: project {project_name!r} has no "
                f"compute_env_access entry for compute_env "
                f"{compute_env_name!r}"}

        env_type = env.get("type")
        if env_type != "ssh":
            return {"error":
                f"cluster_job_status only supports ssh compute envs; "
                f"got type={env_type!r} on env {compute_env_name!r}"}

        remote_cmd = _build_sacct_cmd(norm_id)
        argv = _ssh_argv(env, remote_cmd)
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
        if res.returncode != 0:
            hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
            return {"error":
                f"ssh invocation failed (rc={res.returncode}): "
                f"{(res.stderr or '').strip()[:500]}",
                **({"hint": hint} if hint else {})}

        jobs = _parse_sacct_output(res.stdout)
        return {
            "compute_env": compute_env_name,
            "job_id":      norm_id,
            "jobs":        jobs,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }

    except (ValueError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    except subprocess.TimeoutExpired as e:
        return {"error": f"sacct timed out after {e.timeout}s"}
