"""
JobManager — watchdog-proof async execution for long-running shell commands.

Rationale: the agent stream-watchdog kills a tool call that produces no stdout
for ~600 s. That breaks any legitimately long operation — 22 GB downloads,
30-min conda solves, multi-hour de novo assemblies, full BLAST searches.

Pattern:
  1. Agent calls `start(...)` — fork the command with `subprocess.Popen`, write
     initial status JSON, return `{job_id, status_path, log_path}` immediately.
     No blocking.
  2. Agent does other work (or sleeps), then calls `check(job_id)` periodically.
     Each check is a fast filesystem read (~50 ms) — the watchdog stays asleep.
  3. When the subprocess exits, the next `check` call detects it via
     `Popen.poll()`, updates the status JSON, and returns the final exit code +
     log tail. Job remains queryable forever (status JSON is durable).

Disk layout (rooted at <project>/data/jobs/):
  {job_id}.status.json   — {state, command, env_name, pid, returncode, start_time,
                            end_time, elapsed_seconds, bytes_logged, log_path}
  {job_id}.log           — combined stdout + stderr stream

State machine:
  running → exited (normal exit, any returncode)
  running → cancelled (cancel() called, SIGTERM then SIGKILL after 5s)
  running → orphaned (no PID, no record — e.g. server restart, agent crashed)

Idempotent: re-running start() with the same job_id is rejected unless the prior
job has terminated. The agent picks fresh job IDs (caller-supplied or auto).
"""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from agent.skills import outcomes
from agent.skills.outcomes import broke, refused
from agent.skills import workspace

#: How long `check` will wait for a process whose completion sentinel has ALREADY
#: been written. Bounds the gap between a bash EXIT trap firing and the shell
#: actually exiting — see JobManager._wait_out_teardown. Generous by two orders of
#: magnitude against the observed window, because overshooting costs one slow
#: `check` on a job that is over, and undershooting reports a finished job as
#: running.
_DONE_REAP_GRACE_S = 2.0


class JobManager:
    def __init__(self, config: dict):
        self.config = config
        # One reading: everything that needs the jobs dir asks THIS attribute,
        # which asks the workspace resolver. There is no config key — a second
        # spelling of one directory is identical only for as long as nobody sets it.
        self.jobs_dir = workspace.scratch_dir("jobs")
        # Lazy import to avoid circular reference at module load time.
        from agent.skills.env_manager import EnvManager
        self._env_mgr = EnvManager(config)
        # In-memory Popen handles for jobs spawned by this process. Lost on
        # server restart — for restart resilience the status JSON's PID is used
        # as a fallback (we can detect "no longer alive" via kill -0, but can't
        # recover the exit code since the kernel reaped the zombie).
        self._procs: dict[str, subprocess.Popen] = {}

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def start(
        self,
        command: str,
        env_name: str = "",
        job_id: str = "",
        working_dir: str = "",
        tool: str = "",
    ) -> dict[str, Any]:
        """Spawn `command` as a background process. Returns immediately.

        env_name: if non-empty, command runs inside that conda env via
                  `conda run --prefix ...`. Same activation semantics as
                  run_in_env, just without blocking on completion.
        job_id:   caller-supplied identifier (must be unique). If empty,
                  a 12-char hex ID is auto-generated.
        working_dir: subprocess cwd. Default: project root.
        tool:     for a detached TOOL run, the MCP tool name being executed —
                  captured into the status file so a ledger row can say WHAT the
                  job is. For a backgrounded tool the `command` is the job_runner
                  interpreter line (all N look identical truncated); the producer
                  captures the meaningful name, the reader must not scrape it back
                  out of the argv.
        """
        jid = job_id or self._auto_id()
        if self._is_active(jid):
            return refused("job_manager.already_running", error=f"job_id '{jid}' is already running", job_id=jid)

        if not (command or "").strip():
            return refused("job_manager.empty_command",
                           error="command is empty — nothing to run", job_id=jid)
        # Guard a nonexistent env BEFORE spawning: a doomed `conda run --prefix
        # <missing>` would just fail in the background and cost a spawn + a log.
        if env_name and not (self._env_mgr.envs_dir / env_name).exists():
            return refused("job_manager.env_missing",
                           error=f"env not found: {self._env_mgr.envs_dir / env_name}",
                           job_id=jid, env_name=env_name)

        status_path = self._status_path(jid)
        log_path    = self._log_path(jid)
        done_path   = self.done_path(jid)
        # Wipe prior log on re-use of a non-running job_id, and clear the prior
        # run's completion sentinel — a leftover `.done` would tell a waiter this
        # run finished before it started.
        log_path.write_text("")
        done_path.unlink(missing_ok=True)

        # Build the actual subprocess argv. We always exec through bash -c so
        # callers can use pipes/redirects/etc. Conda activation adds the
        # `conda run --prefix` prefix and inherits env vars.
        script = f"{self._done_trap(done_path)}\n{command}"
        if env_name:
            env_path = self._env_mgr.envs_dir / env_name
            argv = [
                self._env_mgr._conda_exe, "run", "--prefix", str(env_path),
                "--no-capture-output", "/bin/bash", "-c", script,
            ]
        else:
            argv = ["/bin/bash", "-c", script]

        cwd = working_dir or str(workspace.scratch_dir("run"))

        # Open the log file once and hand it to the child as both stdout and
        # stderr. The child can stream gigabytes through it without keeping
        # the parent Python process active.
        log_fh = open(log_path, "wb")
        try:
            proc = subprocess.Popen(
                argv,
                stdout=log_fh,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                cwd=cwd,
                start_new_session=True,   # detach into its own process group
                close_fds=True,
            )
        except Exception as e:
            log_fh.close()
            return broke("job_manager.spawn_failed", error=f"failed to spawn subprocess: {e}", job_id=jid)
        # Parent never writes to log_fh again — close our handle so the child
        # holds it exclusively. The child inherits an open file descriptor.
        log_fh.close()
        # Keep the Popen so check() can poll().
        self._procs[jid] = proc

        # A fast-failing command can exit before we read its process group —
        # getpgid then raises ProcessLookupError. Best-effort: fall back to the
        # pid so a doomed spawn is still RECORDED (check() reaps it) rather than
        # crashing the caller (C4).
        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, OSError):
            pgid = proc.pid

        status = {
            "job_id":          jid,
            "state":           "running",
            "command":         command,
            "tool":            tool,
            "env_name":        env_name,
            "working_dir":     cwd,
            "pid":             proc.pid,
            "pgid":            pgid,
            "returncode":      None,
            "start_time":      time.time(),
            "start_time_iso":  _iso(time.time()),
            "end_time":        None,
            "elapsed_seconds": 0.0,
            "log_path":        str(log_path),
            "status_path":     str(status_path),
            "done_marker":     str(done_path),
        }
        self._write_status(jid, status)
        return {
            "job_id":      jid,
            "status_path": str(status_path),
            "log_path":    str(log_path),
            "done_marker": str(done_path),
            "pid":         proc.pid,
            "state":       "running",
        }

    def check(self, job_id: str, log_tail_lines: int = 30) -> dict[str, Any]:
        """Return current status. Reads disk + polls the process.

        Does not block on a RUNNING job. The one bounded exception is the
        sentinel-reconcile below: a job whose `.done` has appeared has finished
        by its own testimony, and waiting out its teardown is what makes the
        documented "wait on .done, then call check_job once" sequence true.
        """
        status = self._read_status(job_id)
        if not status:
            return refused("job_manager.unknown_job_check", error=f"unknown job_id: {job_id}", job_id=job_id)

        if status["state"] == "running":
            proc = self._procs.get(job_id)
            if proc is not None:
                # We own the Popen — poll() is authoritative (returns None if
                # alive, exit code if exited; the OS won't leak a zombie since
                # poll() reaps).
                rc = proc.poll()
                if rc is None and self.done_path(job_id).exists():
                    rc = self._wait_out_teardown(proc)
                if rc is None:
                    status["elapsed_seconds"] = round(time.time() - status["start_time"], 2)
                    status["bytes_logged"]    = self._log_size(job_id)
                else:
                    status["state"]           = "exited"
                    status["returncode"]      = rc
                    status["end_time"]        = time.time()
                    status["end_time_iso"]    = _iso(time.time())
                    status["elapsed_seconds"] = round(status["end_time"] - status["start_time"], 2)
                    status["bytes_logged"]    = self._log_size(job_id)
                    self._procs.pop(job_id, None)
                self._write_status(job_id, status)
            else:
                # No Popen in memory (server restarted after spawn, or the handle
                # was dropped). Try to reap first: if the process is still OUR
                # child it may be sitting as a zombie, which os.kill(pid, 0)
                # reports as alive, and waitpid recovers the exit code Popen would
                # have given us. Only when there is nothing to reap do we fall
                # back to a liveness probe.
                pid = status.get("pid")
                rc = self._reap(pid)
                if rc is None and self.done_path(job_id).exists():
                    # Same teardown window, reached from a process that did not
                    # spawn this job (server restart) — so re-reap rather than wait.
                    deadline = time.time() + _DONE_REAP_GRACE_S
                    while rc is None and time.time() < deadline:
                        time.sleep(0.02)
                        rc = self._reap(pid)
                if rc is None and pid and self._is_pid_alive(pid, status.get("start_time_iso")):
                    status["elapsed_seconds"] = round(time.time() - status["start_time"], 2)
                    status["bytes_logged"]    = self._log_size(job_id)
                else:
                    status["state"]           = "exited"
                    status["end_time"]        = time.time()
                    status["end_time_iso"]    = _iso(time.time())
                    status["elapsed_seconds"] = round(status["end_time"] - status["start_time"], 2)
                    status["bytes_logged"]    = self._log_size(job_id)
                    if rc is not None:
                        status["returncode"] = rc
                    else:
                        status["note"] = ("exit code unrecoverable: the process is gone and was "
                                          "not reapable by this server — see log_tail for the outcome")
                self._write_status(job_id, status)

        # Always include a log tail so the caller has *something* recent to look at.
        status["log_tail"] = self._read_log_tail(job_id, log_tail_lines)
        self._inline_tool_result(job_id, status)
        return status

    def _inline_tool_result(self, job_id: str, status: dict) -> None:
        """For a job spawned by `@backgroundable`, carry the tool's real return
        value on the status once the job is over.

        This is what makes `check_job` the ONE place a detached outcome is read.
        Before it, the caller polled here, saw `state='exited'`, and then had to
        remember to open `result_path` — two calls, and a forgotten second one
        looks exactly like success while the draft never got its step.

        A job with no args file is a raw `run_in_background` shell command; it
        was never going to produce a result and none is claimed. A job that WAS
        a tool run and exited without writing one gets `result: None` plus an
        explicit `result_missing` — the hole is stated, never left to look like
        a pass.
        """
        if status.get("state") == "running" or not self.args_path(job_id).exists():
            return
        p = self.result_path(job_id)
        if p.exists():
            try:
                status["result"] = json.loads(p.read_text())
                status["tool_outcome"] = {True: "succeeded", False: "failed",
                                          None: "unstated"}[
                    outcomes.call_verdict(status["result"])]
                return
            except Exception as e:
                status["result"] = None
                status["result_missing"] = (
                    f"result file at {p} is unreadable ({e!r}); the tool may have "
                    f"been killed mid-write — see log_tail")
                return
        status["result"] = None
        status["result_missing"] = (
            "the detached tool exited without writing a result — see log_tail for "
            "the traceback. Work already done on disk (images, installs) may be "
            "partially complete; re-run the tool synchronously to find out.")

    def cancel(self, job_id: str, force: bool = False) -> dict[str, Any]:
        """Terminate a running job. SIGTERM by default; force=True sends SIGKILL.

        Always targets the whole process group (start_new_session=True at spawn
        time) so children of the shell command also die — important for chained
        commands like `curl ... | unzip` where two processes are running.
        """
        # First sync state — the job might already have exited.
        self.check(job_id, log_tail_lines=0)
        status = self._read_status(job_id)
        if not status:
            return refused("job_manager.unknown_job_cancel", error=f"unknown job_id: {job_id}", job_id=job_id)
        if status["state"] != "running":
            return {"state": status["state"], "job_id": job_id, "note": "not running, nothing to cancel"}

        pgid = status.get("pgid")
        if not pgid:
            return refused("job_manager.no_pgid", error="no pgid recorded — cannot cancel safely", job_id=job_id)

        proc = self._procs.get(job_id)
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass   # already dead; fall through to state update
        except PermissionError as e:
            return broke("job_manager.signal_failed", error=f"could not signal pgid {pgid}: {e}", job_id=job_id)

        # If we sent SIGTERM, give the child 5s to clean up before promoting to SIGKILL.
        if not force:
            for _ in range(50):
                if proc is not None:
                    if proc.poll() is not None:
                        break
                elif not self._is_pid_alive(status["pid"]):
                    break
                time.sleep(0.1)
            else:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

        # Reap if we own it.
        if proc is not None and proc.poll() is None:
            try:
                proc.wait(timeout=2)
            except Exception:
                pass
        self._procs.pop(job_id, None)

        status["state"]           = "cancelled"
        status["returncode"]      = proc.returncode if proc is not None else None
        status["end_time"]        = time.time()
        status["end_time_iso"]    = _iso(time.time())
        status["elapsed_seconds"] = round(status["end_time"] - status["start_time"], 2)
        status["bytes_logged"]    = self._log_size(job_id)
        self._write_status(job_id, status)
        return {"state": "cancelled", "job_id": job_id, "elapsed_seconds": status["elapsed_seconds"]}

    def list_jobs(self, include_terminated: bool = True) -> list[dict]:
        """Enumerate every job_id with a status file on disk, RECONCILED against reality.

        The status file's `state` is only advanced when someone calls check(); a job
        whose process died unobserved (e.g. the MCP server restarted, losing the Popen
        handle) stays 'running' on disk forever. So a bare read reports zombies as live —
        exactly the agent_status inaccuracy this fixes. Here we re-observe: a 'running'
        record whose PID is no longer alive is reported as 'exited' with reconciled=True,
        so the caller sees the truth without needing to have polled every job.

        Three ledger properties bought by a real cold-start drive (CS17), where ten
        jobs — four refused, six succeeded — came back byte-identical in shape:
          * `returncode` is IN the row. It was always in the status file and this
            reader dropped it, so the one surface for "what happened to my work"
            could not say which jobs failed. None while running / never reaped.
          * NEWEST FIRST — the documented ordering. Globbing sorted by job_id, i.e.
            alphabetically by tool name, which is no order a returning user wants.
          * `tool` is the captured MCP tool name when the producer recorded one
            (detached tool runs); "" on raw shell jobs and on status files from
            before the field existed — absence, not scraped from the argv."""
        out = []
        for f in self.jobs_dir.glob("*.status.json"):
            try:
                status = json.loads(f.read_text())
            except Exception:
                continue
            state = status.get("state")
            reconciled = False
            if state == "running":
                pid = status.get("pid")
                if not (pid and self._is_pid_alive(pid, status.get("start_time_iso"))):
                    state, reconciled = "exited", True   # dead process still marked running
            if not include_terminated and state != "running":
                continue
            row = {
                "job_id":          status.get("job_id"),
                "state":           state,
                "returncode":      status.get("returncode"),
                "tool":            status.get("tool", ""),
                "command":         (status.get("command") or "")[:120],
                "env_name":        status.get("env_name", ""),
                "start_time_iso":  status.get("start_time_iso"),
                "elapsed_seconds": status.get("elapsed_seconds", 0.0),
            }
            if reconciled:
                row["reconciled"] = True   # state was 'running' on disk but the PID is gone
            out.append(row)
        out.sort(key=lambda r: r.get("start_time_iso") or "", reverse=True)
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _auto_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def _status_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.status.json"

    # --- the @backgroundable pair -------------------------------------------
    # A detached TOOL run (as opposed to a raw run_in_background shell command)
    # owns two extra files in this same directory. Both spellings live here so
    # the writer (backgroundable.spawn_detached / job_runner) and the reader
    # (check) cannot drift apart on where they are.

    def args_path(self, job_id: str) -> Path:
        """The kwargs the parent wrote before spawning. Its EXISTENCE is also how
        `check` knows this job was a detached tool and therefore owes a result."""
        return self.jobs_dir / f"{job_id}.args.json"

    def result_path(self, job_id: str) -> Path:
        """Where the detached tool's real return value lands."""
        return self.jobs_dir / f"{job_id}.result.json"

    def done_path(self, job_id: str) -> Path:
        """The completion sentinel — an empty file that exists exactly when the
        job's process has finished.

        THE CHILD WRITES IT ITSELF, from a bash EXIT trap installed by start()
        (see _done_trap). So `until [ -f X.done ]; do sleep 5; done` in any shell
        is a correct wait, whether or not anyone is calling check(). start()
        clears a stale sentinel from a re-used job_id, and _write_status touches
        it as a backstop for a process killed before its trap could run.

        The sentinel says FINISHED, never SUCCEEDED. status.json's `state` +
        `returncode` remain the authoritative outcome, and for a detached tool the
        result is `check_job`'s inlined `result`. The right sequence is: wait on
        .done (cheap, no polling cost), then call check_job once."""
        return self.jobs_dir / f"{job_id}.done"

    @staticmethod
    def _wait_out_teardown(proc: subprocess.Popen) -> int | None:
        """The exit code of a process whose completion sentinel has already been
        written, or None if it somehow outlives the grace window.

        The sentinel is written from a bash EXIT trap, and a trap runs while the
        shell is still alive — so there is a window between "the job says it is
        finished" and "the process is reapable". Inside it `poll()` returns None,
        and `check` reported `running` for a job that had demonstrably finished.
        That window is normally sub-millisecond and widens under load, which is
        what made it look like flakiness rather than a race.

        It matters because `done_marker` is handed to callers on the spawn
        receipt precisely so they can wait without polling; a wait that lands on
        `running` sends them back to polling, which is the affordance's whole
        point. Bounded, and a timeout still reports `running` — a process that
        outlives its own trap by seconds is genuinely stuck and must be said so.
        """
        try:
            return proc.wait(timeout=_DONE_REAP_GRACE_S)
        except subprocess.TimeoutExpired:
            return None

    @staticmethod
    def _done_trap(done: Path) -> str:
        """A bash EXIT trap that touches the completion sentinel.

        Prepended to every job's script so the sentinel is written BY THE JOB,
        which is what lets an external file-wait work. An EXIT trap fires on
        normal exit, an explicit `exit N` and on the usual fatal signals, and
        bash preserves the exit status across it, so the job's returncode is
        unaffected. Prepended rather than appended: appending would land inside
        an unterminated heredoc in the caller's command.

        Two cases it cannot cover, both of which degrade to the old behaviour
        (check() touches the sentinel when it observes the exit): a command that
        `exec`s over the shell, and SIGKILL."""
        touch = f"touch {shlex.quote(str(done))} 2>/dev/null || true"
        return f"trap {shlex.quote(touch)} EXIT"

    def _log_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.log"

    def _is_active(self, job_id: str) -> bool:
        s = self._read_status(job_id)
        if not s or s.get("state") != "running":
            return False
        pid = s.get("pid")
        return bool(pid and self._is_pid_alive(pid, s.get("start_time_iso")))

    @staticmethod
    def _is_pid_alive(pid: int, started_iso: Optional[str] = None) -> bool:
        """PID liveness, hardened against PID REUSE. `os.kill(pid, 0)` alone is unsound:
        an old job's PID can be recycled by an unrelated process, which then reads as
        'alive' (a false positive that keeps a dead job marked running). When `started_iso`
        is known and psutil is available, we also require the live process to have started
        no LATER than the job did — a recycled PID's process is necessarily younger, so
        this rejects it.

        Also unsound alone against a ZOMBIE: a finished child whose parent has not
        reaped it still answers `os.kill(pid, 0)`, which is how a 40-second install
        read as 'running' for 20 minutes (cold-start finding CS8). A zombie has
        exited by definition, so it is reported dead here; check() reaps it."""
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass          # exists, just not ours — fall through to the start-time check
        try:
            import psutil
            if psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
                return False
        except Exception:
            pass          # psutil absent / process vanished mid-check — trust os.kill
        if started_iso:
            try:
                import datetime as _dt
                import psutil
                job_start = _dt.datetime.fromisoformat(started_iso)
                if job_start.tzinfo is None:
                    job_start = job_start.replace(tzinfo=_dt.timezone.utc)
                proc_start = _dt.datetime.fromtimestamp(psutil.Process(pid).create_time(),
                                                        tz=_dt.timezone.utc)
                # a >5s-younger process on this PID is a recycled PID, not our job
                if proc_start > job_start + _dt.timedelta(seconds=5):
                    return False
            except Exception:
                pass      # psutil absent / process vanished mid-check — trust os.kill
        return True

    @staticmethod
    def _reap(pid: Optional[int]) -> Optional[int]:
        """Reap a finished DIRECT child and recover its real exit code, or None.

        None covers three different situations that all mean "nothing to record
        here": no pid, the process is still running, and the process is not our
        child (so the kernel has already reaped it and its code is gone). The
        caller distinguishes the last two with a liveness probe.
        """
        if not pid:
            return None
        try:
            got, wstatus = os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            return None
        if got != pid:
            return None
        return os.waitstatus_to_exitcode(wstatus)

    def _read_status(self, job_id: str) -> Optional[dict]:
        p = self._status_path(job_id)
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text())
        except Exception:
            return None

    def _write_status(self, job_id: str, status: dict) -> None:
        # Atomic write (tempfile + rename) so concurrent checks never see a half-written JSON.
        tmp = self._status_path(job_id).with_suffix(".tmp")
        tmp.write_text(json.dumps(status, indent=2))
        os.replace(tmp, self._status_path(job_id))
        # Backstop for the completion sentinel the job itself writes from its EXIT
        # trap (see _done_trap): a job killed by SIGKILL, or one that exec'd over
        # its shell, never runs that trap. Whoever observes the terminal state
        # first writes the sentinel; both spellings are a touch, so they agree.
        if status.get("state") != "running":
            done = self.done_path(job_id)
            if not done.exists():
                done.touch()

    def _log_size(self, job_id: str) -> int:
        p = self._log_path(job_id)
        return p.stat().st_size if p.exists() else 0

    def _read_log_tail(self, job_id: str, lines: int) -> str:
        p = self._log_path(job_id)
        if not p.exists():
            return ""
        # Read the last ~16 KB and split on lines.
        try:
            with open(p, "rb") as f:
                f.seek(0, 2)
                end = f.tell()
                start = max(0, end - 16384)
                f.seek(start)
                data = f.read().decode("utf-8", errors="replace")
        except Exception:
            return ""
        return "\n".join(data.splitlines()[-lines:])


def _iso(ts: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
