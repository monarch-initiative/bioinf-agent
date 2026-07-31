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
from agent.skills.outcomes import refused, broke, loop
from agent.skills.snapshot import _ssh_argv, _ssh_failure_hint


# Fields we ask sacct for, in order. Keep this fixed — the parser
# depends on the column ordering matching this tuple exactly.
_SACCT_FIELDS = ("JobID", "State", "Elapsed", "ExitCode",
                 "NodeList", "Reason", "Start", "End")

# A SLURM job_id token: digits, optionally `_<task>` for array tasks.
# Bounded at 12 digits each (longer than any cluster's actual ids).
_JOB_ID_RE = re.compile(r"^\d{1,12}(_\d{1,12})?$")


# ---------------------------------------------------------------------------
# Did the job succeed? — a STATE-first verdict.
# ---------------------------------------------------------------------------

#: sacct States that mean the job is over, one way or another. Anything not in
#: here means keep polling.
TERMINAL_STATES = frozenset({
    "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT",
    "OUT_OF_MEMORY", "NODE_FAIL", "PREEMPTED", "BOOT_FAIL",
    "DEADLINE", "REVOKED",
})

RUNNING = "running"
SUCCEEDED = "succeeded"
DIED = "died"

#: Why each terminal non-COMPLETED state is a death, in words a caller can show
#: a user without consulting the SLURM manual.
_DEATH_REASON = {
    "FAILED":        "the job failed",
    "CANCELLED":     "the job was cancelled (scancel, or the scheduler revoked it)",
    "TIMEOUT":       "the job hit its wall-clock limit and was killed",
    "OUT_OF_MEMORY": "the job was killed for exceeding its memory allocation",
    "NODE_FAIL":     "the compute node failed under the job",
    "PREEMPTED":     "the job was preempted by a higher-priority job",
    "BOOT_FAIL":     "the compute node failed to boot for the job",
    "DEADLINE":      "the job hit its scheduling deadline",
    "REVOKED":       "the job allocation was revoked",
}


def normalize_state(state: str) -> str:
    """The bare State token. sacct decorates some states with a trailer —
    `CANCELLED by 123456` is the common one — so an equality test against the
    undecorated name silently fails to match and the job never reads terminal."""
    return (str(state or "").strip().split() or [""])[0].upper()


def classify_sacct_row(row: dict) -> tuple[str, str]:
    """(verdict, human reason) for one sacct row: RUNNING / SUCCEEDED / DIED.

    THE STATE DECIDES, NOT THE EXIT CODE. This is the whole point of the
    function, and it is a fix, not a preference. The poller used to read State
    only to test terminality and then take `int(ExitCode.split(':')[0]) == 0` as
    the verdict — but SLURM reports a signal death as `0:<signal>`, so the rc is
    ZERO for every job the scheduler itself killed. Measured against these very
    parsers, six of eight death shapes took the success path:

        FAILED        0:9     SIGKILL (a cgroup OOM on a cluster that does not
                              account OUT_OF_MEMORY) — the sharpest case
        OUT_OF_MEMORY 0:125   the accounted OOM
        CANCELLED     0:15    scancel
        TIMEOUT       0:0     wall-clock kill
        NODE_FAIL     0:0     the node died under it
        PREEMPTED     0:0     preempted

    Only `FAILED 1:0` — a job whose TOOL exited non-zero — was caught, because
    that is the one death that happens to route through the rc.

    A killed job is not harmless just because nothing downstream noticed. It
    dies mid-write, so its outputs are TRUNCATED rather than absent: a TIMEOUT
    during a BAM write leaves a file that exists, is non-empty, and validates.
    That is the shape that seals green.

    SUCCEEDED therefore requires BOTH a COMPLETED state and a `0:0` exit code.
    A COMPLETED row carrying a non-zero rc is a contradiction, and the honest
    reading of a contradiction is not success."""
    state = normalize_state(row.get("state"))
    exit_code = str(row.get("exit_code") or "").strip()
    reason = str(row.get("reason") or "").strip()

    if state not in TERMINAL_STATES:
        return RUNNING, f"state={state or '?'}"

    if state == "COMPLETED":
        if exit_code in ("", "0:0"):
            return SUCCEEDED, "COMPLETED 0:0"
        # Contradiction: the scheduler says clean, the exit code says otherwise.
        return DIED, (f"sacct reports COMPLETED but the exit code is {exit_code} "
                      f"— a clean state with a non-zero exit is a contradiction, "
                      f"and success is not the safe reading of one")

    why = _DEATH_REASON.get(state, f"the job ended in state {state}")
    detail = f"{why} (state={state}, exit_code={exit_code or 'unrecorded'}"
    if reason and reason not in ("None", "none"):
        detail += f", sacct Reason={reason}"
    return DIED, detail + ")"


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
            return refused("cluster.no_env_access", error=
                f"PermissionDenied: project {project_name!r} has no "
                f"compute_env_access entry for compute_env "
                f"{compute_env_name!r}")

        env_type = env.get("type")
        if env_type != "ssh":
            return refused("cluster.not_ssh_env", error=
                f"cluster_job_status only supports ssh compute envs; "
                f"got type={env_type!r} on env {compute_env_name!r}")

        remote_cmd = _build_sacct_cmd(norm_id)
        argv = _ssh_argv(env, remote_cmd)
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
        if res.returncode != 0:
            hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
            return broke("cluster.status_ssh_failed", error=
                f"ssh invocation failed (rc={res.returncode}): "
                f"{(res.stderr or '').strip()[:500]}",
                **({"hint": hint} if hint else {}))

        jobs = _parse_sacct_output(res.stdout)
        # Each row STATES its verdict. This is the only window a PRODUCTION run
        # has — submit_workflow_job is submit-and-document, so nobody polls on
        # the caller's behalf and nobody classifies for them. Handing back a raw
        # `TIMEOUT | 0:0` row invites exactly the reading the poller used to
        # make: rc is zero, so the job must be fine. Additive (the raw columns
        # are untouched), so every existing consumer is unaffected.
        for row in jobs:
            verdict, why = classify_sacct_row(row)
            row["verdict"] = verdict
            row["verdict_reason"] = why
        return {
            "compute_env": compute_env_name,
            "job_id":      norm_id,
            "jobs":        jobs,
            "captured_at": datetime.now(timezone.utc).isoformat(),
        }

    except (ValueError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return refused("cluster.status_bad_arg", error=f"{type(e).__name__}: {e}")
    except subprocess.TimeoutExpired as e:
        return broke("cluster.status_timeout", error=f"sacct timed out after {e.timeout}s")


# ---------------------------------------------------------------------------
# remote_paths_exist — loud input precondition for run_step_on_cluster
# ---------------------------------------------------------------------------
#
# run_step_on_cluster uploads only the 3 rendered workflow files; it does NOT
# stage input DATA (unlike run_step_in_container, which bind-mounts local
# paths). So a declared input that isn't already on the cluster fails DEEP
# inside the Nextflow run — an opaque, wasted SLURM submission. This does a
# single ssh `test -e` batch BEFORE sbatch so the caller gets the offending
# path immediately instead. Fail-loud, no auto-staging (the user's rails say
# where data may go; we don't invent an upload).

# An absolute path made only of filesystem-safe chars. Anything else is
# refused BEFORE it reaches the shell — no metacharacter can ride in.
_ABS_SAFE_PATH_RE = re.compile(r"^/[A-Za-z0-9_./+\-]{1,4096}$")


def remote_paths_exist(env: dict, paths: list[str], *,
                       timeout: int = 120) -> dict:
    """Check that every path in `paths` EXISTS on `env` (one ssh hop).

    Returns {ok: True, checked: [...]} when all present; {error, missing_paths?}
    otherwise. Paths must be absolute + safe-token (validated before any ssh).
    Empty list ⇒ {ok: True} (nothing to check)."""
    clean = [str(p) for p in (paths or []) if p]
    if not clean:
        return {"ok": True, "checked": []}
    bad = [p for p in clean if not _ABS_SAFE_PATH_RE.match(p)]
    if bad:
        return refused("cluster.unsafe_input_path", error=
            f"input path(s) are not absolute safe paths (refused before ssh): "
            f"{bad[:5]}")
    checks = "; ".join(
        f'[ -e {shlex.quote(p)} ] || echo MISSING:{shlex.quote(p)}'
        for p in clean)
    inner = f"bash -lc {shlex.quote(checks)}"
    argv = _ssh_argv(env, inner)
    try:
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return broke("cluster.input_check_timeout", error=
            f"remote input existence check timed out after {e.timeout}s")
    if res.returncode != 0:
        hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
        return broke("cluster.input_check_ssh_failed", error=
            f"remote input check ssh failed (rc={res.returncode}): "
            f"{(res.stderr or '').strip()[:300]}",
            **({"hint": hint} if hint else {}))
    missing = [ln.split("MISSING:", 1)[1]
               for ln in (res.stdout or "").splitlines()
               if ln.startswith("MISSING:")]
    if missing:
        return refused("cluster.inputs_missing", error=
            f"{len(missing)} declared input(s) do not exist on the cluster: "
            f"{missing[:5]}. run_step_on_cluster does NOT stage input DATA — "
            f"upload them into scratch (or point at data already on the "
            f"cluster) before calling.",
            missing_paths=missing)
    return {"ok": True, "checked": clean}


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


def _parse_max_rss_mb(rss: str) -> Optional[float]:
    """Parse SLURM's MaxRSS (`123456K` / `1.5G`) into MB, or None when sacct accounted
    NOTHING (empty / any spelling of zero / unparseable).

    None vs 0.0 is the honesty distinction (audit 2026-07-16): every "we don't know" case
    used to collapse to 0.0, which is indistinguishable from a real observation of zero —
    and no process that actually ran has a zero peak RSS. On a cluster without cgroup
    memory accounting that fabricated a 0.0 that I7 then sealed as honest cost data.
    Callers must translate None into an explicit `sacct_error`, never into a number.

    The zero test is NUMERIC and happens after the parse, because a cluster with no
    memory accounting writes `0K` / `0M` / `0.00M`, not `0`. The first cut of this fix
    compared string literals (`"" | "0"`), so `0K` — the shape a real SLURM actually
    emits — still parsed to 0.0 with no error marker and sealed straight through I7:
    the very defect this function exists to close, surviving in its own fix. Compare
    the value, never its spelling."""
    if not isinstance(rss, str) or not rss.strip():
        return None
    rss = rss.strip()
    suffix = rss[-1] if rss[-1].isalpha() else ""
    num = rss[:-1] if suffix else rss
    try:
        v = float(num)
    except (ValueError, TypeError):
        return None
    mult = {"K": 1 / 1024, "M": 1, "G": 1024, "T": 1024 * 1024}.get(
        suffix.upper(), 1 / 1024 if not suffix else 1)
    mb = round(v * mult, 1)
    # <= 0 is not a measurement. A negative is nonsense; a zero means unaccounted.
    return mb if mb > 0 else None


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
            return refused("cluster.res_no_env_access", error=
                f"PermissionDenied: project {project_name!r} has no "
                f"compute_env_access entry for compute_env "
                f"{compute_env_name!r}")

        env_type = env.get("type")
        if env_type != "ssh":
            return refused("cluster.res_not_ssh_env", error=
                f"cluster_job_resources only supports ssh compute envs; "
                f"got type={env_type!r}")

        remote_cmd = _build_sacct_resource_cmd(norm_id)
        argv = _ssh_argv(env, remote_cmd)
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=timeout)
        if res.returncode != 0:
            hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
            return broke("cluster.res_ssh_failed", error=
                f"ssh invocation failed (rc={res.returncode}): "
                f"{(res.stderr or '').strip()[:500]}",
                **({"hint": hint} if hint else {}))

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
            return loop("cluster.res_no_rows_retry", error=
                f"sacct returned no rows for job_id={norm_id!r} — "
                f"job may not yet be in slurmdbd, or never existed.")

        # The batch row is the source of truth for MaxRSS. Some SLURM
        # builds emit it as `<id>.batch`, others as `<id>.0`. Take the
        # last row carrying a REAL MaxRSS; if none, fall back to the
        # job-summary row's Elapsed-only evidence.
        #
        # "Real" is decided by the parser, not by string equality: this used to read
        # `!= "0"`, which accepted `0K` — the spelling a cluster with no cgroup memory
        # accounting actually emits — and promoted that row to authoritative, handing
        # I7 a fabricated zero. One definition of "did sacct measure this?", and it
        # lives in _parse_max_rss_mb.
        batch = next((r for r in reversed(rows)
                      if _parse_max_rss_mb(r["max_rss"]) is not None), rows[-1])
        summary = rows[0]

        wall_seconds = _parse_hhmmss(batch["elapsed"] or summary["elapsed"])
        peak_rss_mb = _parse_max_rss_mb(batch["max_rss"])
        ave_cpu_seconds = _parse_hhmmss(batch["ave_cpu"])
        max_cpu_percent = (
            round((ave_cpu_seconds / wall_seconds) * 100.0, 1)
            if wall_seconds > 0 else 0.0)

        out = {
            "wall_seconds":    wall_seconds,
            # a placeholder ONLY when paired with the sacct_error below — the schema
            # types this float, so "unknown" is carried by the marker, not by the number.
            "peak_rss_mb":     peak_rss_mb if peak_rss_mb is not None else 0.0,
            "max_cpu_percent": max_cpu_percent,
            "locus":           "cluster",
            "sacct_job_id":    norm_id,
            "sacct_rows":      rows,
        }
        if peak_rss_mb is None:
            # No row carried a usable MaxRSS (the `batch` pick above fell back to the
            # summary row). Say so LOUDLY rather than reporting 0.0 as an observation:
            # I7 already refuses to seal a step carrying a sacct_error, which is exactly
            # the right outcome — we have no honest peak-RSS evidence for this job.
            out["sacct_error"] = (
                f"sacct returned no MaxRSS for job {norm_id} (cgroup memory accounting "
                f"disabled on this cluster, or no batch/step row reported it) — peak RSS "
                f"is a placeholder zero, not an observation")
        return out

    except (ValueError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return refused("cluster.res_bad_arg", error=f"{type(e).__name__}: {e}")
    except subprocess.TimeoutExpired as e:
        return broke("cluster.res_timeout", error=f"sacct timed out after {e.timeout}s")
