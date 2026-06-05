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


# ---------------------------------------------------------------------------
# cluster_job_resources — I7 evidence (wall_seconds + peak_rss_mb)
# ---------------------------------------------------------------------------
#
# The job-summary row (returned by cluster_job_status's `-X` query) is
# correct for state + exit code, but SLURM doesn't populate MaxRSS at
# the summary level — that lives on the batch step (`<job_id>.batch`).
# We do a SECOND sacct query that omits `-X` and reads the batch row's
# Elapsed + MaxRSS + AveCPU, then return them in the shape
# (`wall_seconds`, `peak_rss_mb`, `max_cpu_percent`) that the I7
# invariant in spec_writer expects — drop-in compatible with
# run_pipeline_step's psutil-monitor output.


_RES_FIELDS = ("JobID", "Elapsed", "MaxRSS", "AveCPU", "TotalCPU")


def _build_sacct_resource_cmd(job_id: str) -> str:
    fields = ",".join(_RES_FIELDS)
    inner = f"sacct -j {shlex.quote(job_id)} -P --noheader -o {fields}"
    return f"bash -lc {shlex.quote(inner)}"


def _parse_hhmmss(t: str) -> float:
    """Parse SLURM's Elapsed (`HH:MM:SS` or `D-HH:MM:SS`) into seconds."""
    if not isinstance(t, str) or not t:
        return 0.0
    days = 0
    if "-" in t:
        d, t = t.split("-", 1)
        days = int(d)
    parts = t.split(":")
    if len(parts) != 3:
        return 0.0
    try:
        h, m, s = int(parts[0]), int(parts[1]), float(parts[2])
        return days * 86400 + h * 3600 + m * 60 + s
    except (ValueError, TypeError):
        return 0.0


def _parse_max_rss_mb(rss: str) -> float:
    """Parse SLURM's MaxRSS (`123456K` / `1.5G` / `0`) into MB."""
    if not isinstance(rss, str) or not rss or rss == "0":
        return 0.0
    rss = rss.strip()
    suffix = rss[-1] if rss[-1].isalpha() else ""
    num = rss[:-1] if suffix else rss
    try:
        v = float(num)
    except (ValueError, TypeError):
        return 0.0
    mult = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}.get(
        suffix.upper(), 1 / 1024 if not suffix else 1)
    return round(v * mult, 1)


def cluster_job_resources(project_name: str,
                          compute_env_name: str,
                          job_id: str,
                          *,
                          access_path: Optional[str] = None,
                          timeout: int = 60) -> dict:
    """Fetch resource_usage for a completed SLURM job in the shape the
    I7 invariant in spec_writer expects: {wall_seconds, peak_rss_mb,
    max_cpu_percent, locus, sacct_rows}.

    sacct without `-X` returns both the job-summary row (Elapsed,
    nothing for MaxRSS) AND the `<job_id>.batch` row (Elapsed + MaxRSS
    + AveCPU populated). We take the batch-row evidence — that's the
    only place MaxRSS lives — and surface them in the standard shape.

    `peak_rss_mb` is real (SLURM's cgroup accounting; same accuracy as
    `time -v`'s "Maximum resident set size"). `max_cpu_percent` is a
    derived estimate from AveCPU/Elapsed × 100 — coarser than psutil's
    sampling but the only thing sacct offers; spec_writer accepts a
    rough number. `locus: "cluster"` tags the evidence so a
    sanity-check downstream knows this didn't come from a host psutil
    monitor.

    Auth: same gate as cluster_job_status (project on env, ssh-only,
    job_id validated as `\\d{1,12}(_\\d{1,12})?`)."""
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
                f"cluster_job_resources only supports ssh compute envs; "
                f"got type={env_type!r}"}

        remote_cmd = _build_sacct_resource_cmd(norm_id)
        argv = _ssh_argv(env, remote_cmd)
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
        if res.returncode != 0:
            hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
            return {"error":
                f"ssh invocation failed (rc={res.returncode}): "
                f"{(res.stderr or '').strip()[:500]}",
                **({"hint": hint} if hint else {})}

        rows = []
        for line in (res.stdout or "").splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split("|")
            if len(parts) != len(_RES_FIELDS):
                continue
            rows.append({
                "job_id":   parts[0],
                "elapsed":  parts[1],
                "max_rss":  parts[2],
                "ave_cpu":  parts[3],
                "total_cpu": parts[4],
            })

        if not rows:
            return {"error":
                f"sacct returned no rows for job_id={norm_id!r} — "
                f"job may not yet be in slurmdbd, or never existed."}

        # The batch row is the source of truth for MaxRSS. Some SLURM
        # builds emit it as `<id>.batch`, others as `<id>.0`. Take the
        # last row with MaxRSS != 0; if none, fall back to the
        # job-summary row's Elapsed-only evidence.
        batch = next((r for r in reversed(rows)
                      if r["max_rss"] and r["max_rss"] != "0"), rows[-1])
        summary = rows[0]

        wall_seconds = _parse_hhmmss(batch["elapsed"] or summary["elapsed"])
        peak_rss_mb = _parse_max_rss_mb(batch["max_rss"])
        ave_cpu_seconds = _parse_hhmmss(batch["ave_cpu"])
        max_cpu_percent = (
            round((ave_cpu_seconds / wall_seconds) * 100.0, 1)
            if wall_seconds > 0 else 0.0)

        return {
            "wall_seconds":    wall_seconds,
            "peak_rss_mb":     peak_rss_mb,
            "max_cpu_percent": max_cpu_percent,
            "locus":           "cluster",
            "sacct_job_id":    norm_id,
            "sacct_rows":      rows,
        }

    except (ValueError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return {"error": f"{type(e).__name__}: {e}"}
    except subprocess.TimeoutExpired as e:
        return {"error": f"sacct timed out after {e.timeout}s"}
