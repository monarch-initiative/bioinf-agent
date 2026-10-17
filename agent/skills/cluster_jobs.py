"""
cluster_job_status — query SLURM job state on a compute env so the
agent can poll a submitted job to completion.

Why this earns its place:
  - run_production_pipeline returns a SLURM job_id.
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
SLURM's own ACL limits visibility to the user's own jobs. A `run_dir`, whose
records are read, must be a path the project may `download`.

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
import time
from datetime import datetime, timezone
from collections import Counter
from pathlib import Path
from typing import Optional

from agent.skills import compute_access
from agent.skills.outcomes import refused, broke, loop
from agent.skills.snapshot import _SEEN_FN, _ssh_argv, _ssh_failure_hint


# The fields we ask sacct for, in order, each with the key its row dict reads it
# under: the command and the parse both read this one map.
_SACCT_KEYS = {"JobID": "job_id", "State": "state", "Elapsed": "elapsed", "ExitCode": "exit_code",
               "NodeList": "nodelist", "Reason": "reason", "Start": "start", "End": "end"}
_SACCT_FIELDS = tuple(_SACCT_KEYS)

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
    function, not a preference: SLURM reports a signal death as `0:<signal>`,
    so the rc is ZERO for every job the scheduler itself killed, and a verdict
    taken from `int(ExitCode.split(':')[0]) == 0` sends six of these eight
    death shapes down the success path:

        FAILED        0:9     SIGKILL (a cgroup OOM on a cluster that does not
                              account OUT_OF_MEMORY) — the sharpest case
        OUT_OF_MEMORY 0:125   the accounted OOM
        CANCELLED     0:15    scancel
        TIMEOUT       0:0     wall-clock kill
        NODE_FAIL     0:0     the node died under it
        PREEMPTED     0:0     preempted

    Only `FAILED 1:0` — a job whose TOOL exited non-zero — is the one death
    that happens to route through the rc.

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
    """The remote shell command for one validated job id, or several joined
    by commas. Login shell so sacct is on PATH on systems that add it via
    /etc/profile.d/slurm.sh. The field list is fixed; pinned by a test."""
    fields = ",".join(_SACCT_FIELDS)
    inner = f"sacct -j {shlex.quote(job_id)} -P --noheader -X -o {fields}"
    return f"bash -lc {shlex.quote(inner)}"


def _sacct_rows(text: str, keys: tuple[str, ...]) -> list[dict]:
    """`sacct -P --noheader` output as row dicts, one per non-empty line, its `|`
    columns named by `keys`. A row whose column count differs (a truncated line) is
    dropped rather than misread. Empty output -> [] (job not in slurmdbd yet, or
    never existed)."""
    rows: list[dict] = []
    for line in text.splitlines():
        parts = line.strip().split("|")
        if line.strip() and len(parts) == len(keys):
            rows.append(dict(zip(keys, parts)))
    return rows


def _parse_sacct_output(text: str) -> list[dict]:
    """The status rows, keyed by `_SACCT_KEYS`; `exit_code` keeps sacct's
    `<rc>:<signal>` shape verbatim."""
    return _sacct_rows(text, tuple(_SACCT_KEYS.values()))


#: The longest one call waits (under the client's ~600 s stream watchdog), and how
#: often it looks again while it does.
_WAIT_CAP_S = 540
_POLL_S = 30
#: ssh hops in one look: the manager's sacct row, the run's records, its tasks in flight.
_HOPS_PER_LOOK = 3


def cluster_job_status(project_name: str,
                       compute_env_name: str,
                       job_id: str,
                       *,
                       run_dir: str = "",
                       wait_s: int = 0,
                       access_path: Optional[str] = None,
                       timeout: int = 60) -> dict:
    """Look up SLURM state for `job_id` on `compute_env_name`.

    `run_dir` names a rendered pipeline's launch directory: the job is then the
    MANAGER (Nextflow submitting one SLURM job per stage and sample, doing none of
    the work), and the answer gains `pipeline` — every task from the run's own
    `runs/<stamp>/trace.txt` with its status, exit code, runtime, peak memory and
    SLURM job id, plus every task submitted and not finished yet with the
    scheduler's state for it (PENDING and why, RUNNING where and for how long),
    a count per status, and a verdict over the tasks. The manager's own row is
    never reported as the work.

    `wait_s` blocks up to that many seconds (capped at 540, under the stream
    watchdog) until something changes: a job's state, or a task starting or
    finishing; it returns at once when the job is over. So one call replaces a
    poll loop; the answer then says `waited_s`.

    Pure-read: each look runs `bash -lc 'sacct -j <id> -P --noheader -X -o
    <fields>'` over ssh, and for a pipeline one more hop for its records and
    one sacct call for its tasks in flight; returns a list of row-dicts (one
    per job, plus one per array-task for array jobs).

    Authorization: project must have a `compute_env_access` entry for
    `compute_env_name`; SLURM's own ACL keeps visibility scoped to the
    user's own jobs. A `run_dir` must be a path the project may `download`.

    Returns:
      {
        "compute_env": "<env_name>",
        "job_id":      "<requested_id>",
        "jobs":        [{job_id, state, elapsed, exit_code, nodelist,
                          reason, start, end, verdict, verdict_reason}, ...],
        "captured_at": "<iso utc>",
        "pipeline":    {...},            # with run_dir
        "waited_s":    <int>,            # with wait_s
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
        if run_dir and not _ABS_SAFE_PATH_RE.match(run_dir):
            return refused("cluster.unsafe_run_dir", error=
                f"run_dir {run_dir!r} is not an absolute safe path (refused before ssh)")
        if run_dir:
            # reading a run's log and trace back is a download from that directory
            from agent.skills import transfer
            transfer._validate_remote_abs_path(run_dir)
            transfer._classify_zone_and_authorize(project=project, env=env, remote_abs_path=run_dir,
                                                  primitive_name="download")

        started = time.monotonic()
        out = _observe(env, compute_env_name, norm_id, run_dir, timeout=timeout)
        look_s = time.monotonic() - started
        mark = _progress(out)
        deadline = started + min(max(int(wait_s or 0), 0), _WAIT_CAP_S)
        # The next look starts only if one as long as the last still ends by the deadline,
        # and each of its ssh hops is held to an equal share of the time left, so a look
        # that hangs ends by the deadline too. The last answer then stands, as it does
        # when a later look reads less than it did.
        while ("error" not in out and not _settled(out) and _progress(out) == mark
               and time.monotonic() + _POLL_S + look_s <= deadline):
            time.sleep(_POLL_S)
            looked = time.monotonic()
            hop_s = int((deadline - looked) / _HOPS_PER_LOOK)
            if hop_s < 1:
                break
            try:
                look = _observe(env, compute_env_name, norm_id, run_dir, timeout=min(timeout, hop_s))
            except subprocess.TimeoutExpired:
                break
            if _read_less(look, out):
                break
            out = look
            look_s = time.monotonic() - looked
        if wait_s and "error" not in out:
            out["waited_s"] = round(time.monotonic() - started)
        return out

    except (ValueError, compute_access.PermissionDenied,
            compute_access.ConfigError, FileNotFoundError, KeyError) as e:
        return refused("cluster.status_bad_arg", error=f"{type(e).__name__}: {e}")
    except subprocess.TimeoutExpired as e:
        return broke("cluster.status_timeout", error=f"sacct timed out after {e.timeout}s")


def _observe(env: dict, compute_env_name: str, job_id: str, run_dir: str, *,
             timeout: int) -> dict:
    """One look: the job's sacct rows, each with its verdict, and the pipeline
    run when `run_dir` is given."""
    res = subprocess.run(_ssh_argv(env, _build_sacct_cmd(job_id)),
                         capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
        return broke("cluster.status_ssh_failed", error=
            f"ssh invocation failed (rc={res.returncode}): "
            f"{(res.stderr or '').strip()[:500]}",
            **({"hint": hint} if hint else {}))

    jobs = _parse_sacct_output(res.stdout)
    # Each row STATES its verdict. This is the only window a PRODUCTION run
    # has — submission is submit-and-document, so nobody polls on the
    # caller's behalf and nobody classifies for them. Handing back a raw
    # `TIMEOUT | 0:0` row invites the wrong reading — rc is zero, so the job
    # must be fine. Additive (the raw columns are untouched), so consumers
    # of the raw shape are unaffected.
    for row in jobs:
        verdict, why = classify_sacct_row(row)
        row["verdict"] = verdict
        row["verdict_reason"] = why
    out = {
        "compute_env": compute_env_name,
        "job_id":      job_id,
        "jobs":        jobs,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }
    if run_dir:
        out["pipeline"] = _read_pipeline_run(env, run_dir, job_id, jobs, timeout=timeout)
    return out


def _over(rows: list[dict]) -> bool:
    """There are rows, and none is still running."""
    return bool(rows) and all(r.get("verdict") != RUNNING for r in rows)


def _settled(status: dict) -> bool:
    """Nothing more will change: every job row is over (a pipeline's manager has
    written its records by the time its job ends)."""
    return _over(status.get("jobs") or [])


def _read_less(look: dict, last: dict) -> bool:
    """`look` failed to read something `last` read: the job's row, the run's records,
    or the scheduler's state for its tasks in flight."""
    def gaps(status: dict) -> tuple:
        run = status.get("pipeline") or {}
        return "error" in status, "error" in run, "scheduler_unread" in run
    return any(now and not before for now, before in zip(gaps(look), gaps(last)))


def _progress(status: dict) -> tuple:
    """What a wait watches for a change in: each job's state and each task's."""
    tasks = (status.get("pipeline") or {}).get("tasks") or []
    return (tuple((r.get("job_id"), r.get("state")) for r in status.get("jobs") or []),
            tuple((t.get("native_id"), t.get("status")) for t in tasks))


# ---------------------------------------------------------------------------
# A rendered pipeline's run, read from its own records
# ---------------------------------------------------------------------------
#
# The manager job prints `run records: runs/<stamp>/` to its SLURM log
# (`<pipeline>-<jobid>.out` in the launch directory) before any task starts,
# nextflow.config writes `runs/<stamp>/trace.txt` as tasks finish, and Nextflow's
# own log (`runs/<stamp>/nextflow.log`) names each task's SLURM job id the moment
# it submits it. One ssh hop reads all three; one sacct call then asks after the
# tasks submitted and not finished. Columns are the renderer's TRACE_FIELDS; the
# parse goes by the header so a reordered trace cannot mislabel a field, and by the
# task_id that opens every record, so a field Nextflow writes over several lines
# (`script`, in a trace from an older render) continues its record instead of
# becoming a blank task.

#: Trace statuses a run can leave a task in, in Nextflow's own vocabulary. A task a
#: resumed run reused is CACHED: done, as much as one it ran.
TASK_DONE, TASK_FAILED = frozenset({"COMPLETED", "CACHED"}), "FAILED"


def _build_pipeline_run_cmd(run_dir: str, job_id: str) -> str:
    q = shlex.quote
    script = (_SEEN_FN + f'out=$(ls -1 {q(run_dir)}/*-{job_id}.out 2>/dev/null | head -n 1); '
              f'if [ -z "$out" ]; then echo NOOUT; exit 0; fi; '
              f'echo "OUT=$out"; '
              # the stamp from the last line main.nf opens with `run records:` (a requeued
              # manager appends its next run to the same log); a task's command echoed
              # into the log may name other runs/ paths, never at the start of that line
              f"stamp=$(sed -n 's|^run records: \\(runs/[0-9_][0-9_]*\\).*|\\1|p' \"$out\" | tail -n 1); "
              f'echo "STAMP=$stamp"; '
              # `echo` ends a trace whose last line is still being written
              f'if [ -n "$stamp" ] && seen {q(run_dir)}/"$stamp"/trace.txt; then '
              f'echo TRACE_BEGIN; cat {q(run_dir)}/"$stamp"/trace.txt; echo; echo TRACE_END; fi; '
              f'if [ -n "$stamp" ] && seen {q(run_dir)}/"$stamp"/nextflow.log; then '
              f'echo SUBMITTED_BEGIN; grep -o "submitted process .* > jobId: [0-9]*; workDir: [^ ]*" '
              f'{q(run_dir)}/"$stamp"/nextflow.log; echo SUBMITTED_END; fi; exit 0')
    return f"bash -lc {q(script)}"


#: A task Nextflow handed to SLURM, as its log says so.
_SUBMITTED_RE = re.compile(
    r"submitted process (?P<name>.+) > jobId: (?P<native_id>\d+); workDir: (?P<workdir>\S+)")


def _in_flight(env: dict, submitted: list[str], finished: set, *,
               timeout: int) -> tuple[list[dict], str]:
    """The tasks submitted and not in the trace yet (it lists a task once it
    finishes), and why the scheduler could not be asked about them, if it could
    not. A task is PENDING (with why) or RUNNING (where, how long) as the scheduler
    says; otherwise SUBMITTED, Nextflow's word for a task it has handed over and
    not yet collected, with the scheduler's own state beside it when it has one
    (a job it reports over, before Nextflow has checked the task's outputs)."""
    live = {m["native_id"]: m.groupdict() for m in map(_SUBMITTED_RE.search, submitted)
            if m and m["native_id"] not in finished}
    if not live:
        return [], ""
    rows: dict = {}
    unread = ""
    try:
        res = subprocess.run(_ssh_argv(env, _build_sacct_cmd(",".join(live))),
                             capture_output=True, text=True, timeout=timeout)
        if res.returncode == 0:
            rows = {r["job_id"]: r for r in _parse_sacct_output(res.stdout)}
        else:
            unread = f"sacct for the tasks in flight failed (rc={res.returncode}): {(res.stderr or '').strip()[:200]}"
    except subprocess.TimeoutExpired as e:
        unread = f"sacct for the tasks in flight timed out after {e.timeout}s"
    tasks = []
    for jid, task in live.items():
        row = rows.get(jid, {})
        state = normalize_state(row.get("state"))
        view = {"name": task["name"], "native_id": jid,
                "status": state if state in ("PENDING", "RUNNING") else "SUBMITTED",
                "workdir": task["workdir"]}
        if state and state != view["status"]:
            view["scheduler_state"] = state
        if row.get("elapsed"):
            view["elapsed"] = row["elapsed"]
        if state == "RUNNING" and row.get("nodelist"):
            view["node"] = row["nodelist"]
        if state == "PENDING" and row.get("reason"):
            view["reason"] = row["reason"]
        tasks.append(view)
    return tasks, unread


_TRACE_RECORD_RE = re.compile(r"^\d+\t")      # a record opens with its task_id


def _parse_trace(text: str) -> list[dict]:
    """Nextflow's tab-separated trace, by its header line. When task_id is the first
    column (the rendered config's), a record is the line that opens with a task_id and
    a line that does not continues the record before it (a field written over several
    lines), in its last column; under any other first column each line is a record."""
    lines = [ln for ln in (text or "").splitlines() if ln.strip()]
    if not lines:
        return []
    header = lines[0].split("\t")
    by_task_id = header[0] == "task_id"           # else every line is a record of its own
    records: list[str] = []
    for ln in lines[1:]:
        if not by_task_id or _TRACE_RECORD_RE.match(ln):
            records.append(ln)
        elif records:
            records[-1] += "\n" + ln.strip()
    rows = []
    for ln in records:
        cells = [c.strip() for c in ln.split("\t")]
        rows.append({header[i]: (cells[i] if i < len(cells) else "") for i in range(len(header))})
    return rows


def _task_view(row: dict) -> dict:
    """One task as the caller reads it: the work, never the manager. The work
    directory rides along only for a task that did not complete — that is where
    `.command.log` and `.command.err` are."""
    view = {k: row.get(k, "") for k in ("name", "native_id", "status", "exit", "realtime", "peak_rss")}
    if row.get("status") not in TASK_DONE:
        view["workdir"] = row.get("workdir", "")
    return view


def _pipeline_verdict(tasks: list[dict], manager_rows: list[dict]) -> str:
    """succeeded | failed | running | not_started | unobserved — over the TASKS, with
    the manager consulted to tell a finished run from one still submitting, a manager
    that died (it can no longer submit what is left) from one that finished, and a
    queued manager from one at work. `unobserved`: the manager ended and no task of
    it is on record in the run directory, so nothing says what the run did; or the
    scheduler holds no row for the manager and no task is in flight, so nothing says
    whether the run is over."""
    statuses = [t.get("status") for t in tasks]
    if any(s == TASK_FAILED for s in statuses):
        return "failed"
    if not manager_rows:
        return "running" if any(s not in TASK_DONE for s in statuses) else "unobserved"
    manager_over = _over(manager_rows)
    if manager_over and any(r.get("verdict") == DIED for r in manager_rows):
        return "failed"
    if tasks and all(s in TASK_DONE for s in statuses) and manager_over:
        return "succeeded"
    if manager_over:
        return "running" if tasks else "unobserved"
    queued = all(str(r.get("state", "")).startswith("PENDING") for r in manager_rows)
    return "not_started" if queued and not tasks else "running"


def _block(lines: list[str], tag: str) -> list[str]:
    """The lines the remote script printed between `<tag>_BEGIN` and `<tag>_END`."""
    if f"{tag}_BEGIN" in lines and f"{tag}_END" in lines:
        return lines[lines.index(f"{tag}_BEGIN") + 1:lines.index(f"{tag}_END")]
    return []


def _read_pipeline_run(env: dict, run_dir: str, job_id: str, manager_rows: list[dict], *,
                       timeout: int) -> dict:
    """What the run's own records say. Never raises: an unreadable run directory is
    reported as such beside the manager's SLURM row."""
    argv = _ssh_argv(env, _build_pipeline_run_cmd(run_dir, job_id))
    try:
        res = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        return broke("cluster.run_records_timeout", run_dir=run_dir,
                     error=f"reading the run records timed out after {e.timeout}s")
    if res.returncode != 0:
        hint = _ssh_failure_hint(res.stderr or "", env.get("host", "?"))
        return broke("cluster.run_records_ssh_failed", run_dir=run_dir,
                     error=f"could not read the run records (rc={res.returncode}): "
                           f"{(res.stderr or '').strip()[:300]}",
                     **({"hint": hint} if hint else {}))
    lines = (res.stdout or "").splitlines()
    if "NOOUT" in lines:
        verdict = _pipeline_verdict([], manager_rows)
        note = {
            "not_started": "the job has not started",
            "running": "the manager job is running; its log is not visible here yet",
            "failed": "the manager job ended without writing it: it died before Nextflow started, "
                      "or run_dir is not the directory it was launched in",
        }.get(verdict, "the manager job ended and left no log here: check that run_dir is "
                       "the directory it was launched in" if manager_rows else
                       "the scheduler holds no record of the job either: check the job id and run_dir")
        return {"run_dir": run_dir, "verdict": verdict, "tasks": [], "task_summary": {},
                "note": f"no manager log (<pipeline>-{job_id}.out) in {run_dir}: {note}"}
    stamp = next((ln.split("=", 1)[1] for ln in lines if ln.startswith("STAMP=")), "")
    out_log = next((ln.split("=", 1)[1] for ln in lines if ln.startswith("OUT=")), "")
    trace_rows = _parse_trace("\n".join(_block(lines, "TRACE")))
    in_flight, unread = _in_flight(env, _block(lines, "SUBMITTED"),
                                   {r.get("native_id") for r in trace_rows}, timeout=timeout)
    tasks = [_task_view(r) for r in trace_rows] + in_flight
    summary = dict(Counter(t.get("status", "") for t in tasks))
    # the verdict and the summary lead: the task list is as long as the run
    return {"run_dir": run_dir, "verdict": _pipeline_verdict(tasks, manager_rows),
            "task_summary": summary, "manager_log": out_log, "stamp": stamp,
            "run_records": f"{run_dir}/{stamp}" if stamp else None,
            **({"scheduler_unread": unread} if unread else {}), "tasks": tasks}


# ---------------------------------------------------------------------------
# jobs_in_dir — which of the user's jobs the scheduler still holds in a directory
# ---------------------------------------------------------------------------

def jobs_in_dir_sh(d: str) -> str:
    """Shell that prints `LIVE=<job id> <state>` for each job of this user the
    scheduler still holds that runs in `d` or under it, by the path as given or as
    resolved (a link); a scheduler that cannot answer prints SQUEUE_FAILED and up to
    three SQUEUE_ERR lines, never nothing."""
    q = shlex.quote
    return (f"p=$(cd {q(d)} 2>/dev/null && pwd -P); "
            f"held=$(squeue -h -u \"$USER\" -o '%i %T %Z' 2>&1) || {{ echo SQUEUE_FAILED; "
            f"printf '%s\\n' \"$held\" | tail -n 3 | sed 's/^/SQUEUE_ERR /'; held=; }}; "
            f"printf '%s\\n' \"$held\" | awk -v d={q(d)} -v p=\"$p\" "
            f"'$3 == d || index($3, d \"/\") == 1 || (p != \"\" && ($3 == p || index($3, p \"/\") == 1)) "
            f"{{print \"LIVE=\" $1 \" \" $2}}'")


def jobs_in_dir(lines: list[str]) -> tuple[list[str], Optional[str]]:
    """What `jobs_in_dir_sh` printed: (the live jobs as `<id> <state>`, None), or
    ([], what squeue said) when the scheduler could not answer."""
    if "SQUEUE_FAILED" in lines:
        said = " | ".join(ln[len("SQUEUE_ERR "):] for ln in lines if ln.startswith("SQUEUE_ERR "))
        return [], said or "no message"
    return [ln[len("LIVE="):] for ln in lines if ln.startswith("LIVE=")], None


# ---------------------------------------------------------------------------
# remote_paths_exist — loud input precondition for run_step_on_cluster
# ---------------------------------------------------------------------------
#
# run_step_on_cluster uploads only the 3 rendered workflow files; it does NOT
# stage input DATA (unlike run_step_in_container, which bind-mounts local
# paths). So a declared input that isn't already on the cluster fails DEEP
# inside the Nextflow run — an opaque, wasted SLURM submission. This does a
# single ssh `seen` batch BEFORE sbatch so the caller gets the offending
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
    checks = _SEEN_FN + "; ".join(
        f'seen {shlex.quote(p)} || echo MISSING:{shlex.quote(p)}'
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


_RES_KEYS = {"JobID": "job_id", "Elapsed": "elapsed", "MaxRSS": "max_rss", "AveCPU": "ave_cpu",
             "TotalCPU": "total_cpu"}
_RES_FIELDS = tuple(_RES_KEYS)


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

    None vs 0.0 is the honesty distinction: collapsing "we don't know" to 0.0 makes
    it indistinguishable from a real observation of zero — and no process that
    actually ran has a zero peak RSS, so on a cluster without cgroup memory
    accounting that fabricates a 0.0 that I7 then seals as honest cost data.
    Callers must translate None into an explicit `sacct_error`, never into a number.

    The zero test is NUMERIC and happens after the parse, because a cluster with no
    memory accounting writes `0K` / `0M` / `0.00M`, not `0`. A string-literal
    comparison (`"" | "0"`) accepts `0K` — the shape a real SLURM actually emits —
    and parses it to 0.0 with no error marker, sealing straight through I7 the very
    defect this function exists to close. Compare the value, never its spelling."""
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

        rows = _sacct_rows(res.stdout or "", tuple(_RES_KEYS.values()))

        if not rows:
            return loop("cluster.res_no_rows_retry", error=
                f"sacct returned no rows for job_id={norm_id!r} — "
                f"job may not yet be in slurmdbd, or never existed.")

        # The batch row is the source of truth for MaxRSS. Some SLURM
        # builds emit it as `<id>.batch`, others as `<id>.0`. Take the
        # last row carrying a REAL MaxRSS; if none, fall back to the
        # job-summary row's Elapsed-only evidence.
        #
        # "Real" is decided by the parser, not by string equality: `!= "0"` accepts
        # `0K` — the spelling a cluster with no cgroup memory accounting actually
        # emits — and promotes that row to authoritative, handing I7 a fabricated
        # zero. One definition of "did sacct measure this?", and it lives in
        # _parse_max_rss_mb.
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
