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


class JobManager:
    def __init__(self, config: dict):
        self.config = config
        self.project_root = Path(__file__).parent.parent.parent.resolve()
        self.jobs_dir = self.project_root / "data" / "jobs"
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
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
    ) -> dict[str, Any]:
        """Spawn `command` as a background process. Returns immediately.

        env_name: if non-empty, command runs inside that conda env via
                  `conda run --prefix ...`. Same activation semantics as
                  run_in_env, just without blocking on completion.
        job_id:   caller-supplied identifier (must be unique). If empty,
                  a 12-char hex ID is auto-generated.
        working_dir: subprocess cwd. Default: project root.
        """
        jid = job_id or self._auto_id()
        if self._is_active(jid):
            return {"error": f"job_id '{jid}' is already running", "job_id": jid}

        status_path = self._status_path(jid)
        log_path    = self._log_path(jid)
        # Wipe prior log on re-use of a non-running job_id.
        log_path.write_text("")

        # Build the actual subprocess argv. We always exec through bash -c so
        # callers can use pipes/redirects/etc. Conda activation adds the
        # `conda run --prefix` prefix and inherits env vars.
        if env_name:
            env_path = self._env_mgr.envs_dir / env_name
            argv = [
                self._env_mgr._conda_exe, "run", "--prefix", str(env_path),
                "--no-capture-output", "/bin/bash", "-c", command,
            ]
        else:
            argv = ["/bin/bash", "-c", command]

        cwd = working_dir or str(self.project_root)

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
            return {"error": f"failed to spawn subprocess: {e}", "job_id": jid}
        # Parent never writes to log_fh again — close our handle so the child
        # holds it exclusively. The child inherits an open file descriptor.
        log_fh.close()
        # Keep the Popen so check() can poll().
        self._procs[jid] = proc

        status = {
            "job_id":          jid,
            "state":           "running",
            "command":         command,
            "env_name":        env_name,
            "working_dir":     cwd,
            "pid":             proc.pid,
            "pgid":            os.getpgid(proc.pid),
            "returncode":      None,
            "start_time":      time.time(),
            "start_time_iso":  _iso(time.time()),
            "end_time":        None,
            "elapsed_seconds": 0.0,
            "log_path":        str(log_path),
            "status_path":     str(status_path),
        }
        self._write_status(jid, status)
        return {
            "job_id":      jid,
            "status_path": str(status_path),
            "log_path":    str(log_path),
            "pid":         proc.pid,
            "state":       "running",
        }

    def check(self, job_id: str, log_tail_lines: int = 30) -> dict[str, Any]:
        """Return current status. Reads disk + polls the process; does NOT block."""
        status = self._read_status(job_id)
        if not status:
            return {"error": f"unknown job_id: {job_id}", "job_id": job_id}

        if status["state"] == "running":
            proc = self._procs.get(job_id)
            if proc is not None:
                # We own the Popen — poll() is authoritative (returns None if
                # alive, exit code if exited; the OS won't leak a zombie since
                # poll() reaps).
                rc = proc.poll()
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
                # No Popen in memory (server restarted after spawn). Best-effort:
                # check the PID. If the process is gone we can't recover its exit
                # code — record state="exited", returncode=None, and let the log
                # tail carry the actual outcome.
                pid = status.get("pid")
                if pid and self._is_pid_alive(pid):
                    status["elapsed_seconds"] = round(time.time() - status["start_time"], 2)
                    status["bytes_logged"]    = self._log_size(job_id)
                else:
                    status["state"]           = "exited"
                    status["end_time"]        = time.time()
                    status["end_time_iso"]    = _iso(time.time())
                    status["elapsed_seconds"] = round(status["end_time"] - status["start_time"], 2)
                    status["bytes_logged"]    = self._log_size(job_id)
                    status["note"]            = "exit code unrecoverable: server restart between spawn and check"
                self._write_status(job_id, status)

        # Always include a log tail so the caller has *something* recent to look at.
        status["log_tail"] = self._read_log_tail(job_id, log_tail_lines)
        return status

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
            return {"error": f"unknown job_id: {job_id}", "job_id": job_id}
        if status["state"] != "running":
            return {"state": status["state"], "job_id": job_id, "note": "not running, nothing to cancel"}

        pgid = status.get("pgid")
        if not pgid:
            return {"error": "no pgid recorded — cannot cancel safely", "job_id": job_id}

        proc = self._procs.get(job_id)
        sig = signal.SIGKILL if force else signal.SIGTERM
        try:
            os.killpg(pgid, sig)
        except ProcessLookupError:
            pass   # already dead; fall through to state update
        except PermissionError as e:
            return {"error": f"could not signal pgid {pgid}: {e}", "job_id": job_id}

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
        """Enumerate every job_id with a status file on disk."""
        out = []
        for f in sorted(self.jobs_dir.glob("*.status.json")):
            try:
                status = json.loads(f.read_text())
            except Exception:
                continue
            if not include_terminated and status.get("state") != "running":
                continue
            out.append({
                "job_id":          status.get("job_id"),
                "state":           status.get("state"),
                "command":         (status.get("command") or "")[:80],
                "env_name":        status.get("env_name", ""),
                "start_time_iso":  status.get("start_time_iso"),
                "elapsed_seconds": status.get("elapsed_seconds", 0.0),
            })
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _auto_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def _status_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.status.json"

    def _done_path(self, job_id: str) -> Path:
        """The completion sentinel file. Created ONLY when the job has exited
        (atomic by touch). Polling shell loops can `until [ -f X.done ]; do
        sleep; done` and get correct semantics — pre-N6, the only on-disk
        signal was status.json, but that file exists from t=0 (created with
        state='running' before any work happens), so file-existence polls
        misfired immediately. The status.json content remains authoritative
        for the actual state; .done is the atomic 'is it over' signal."""
        return self.jobs_dir / f"{job_id}.done"

    def _log_path(self, job_id: str) -> Path:
        return self.jobs_dir / f"{job_id}.log"

    def _is_active(self, job_id: str) -> bool:
        s = self._read_status(job_id)
        if not s or s.get("state") != "running":
            return False
        pid = s.get("pid")
        return bool(pid and self._is_pid_alive(pid))

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True   # exists, just not ours
        return True

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
        # N6 fix (batch-3): drop the completion sentinel `.done` file once
        # we've observed a terminal state. Pre-fix the only on-disk signal
        # was status.json which exists from t=0; shell loops checking file-
        # existence fired immediately. Status.json content stays the truth;
        # .done is the atomic 'is it over' signal a polling loop can rely on.
        if status.get("state") != "running":
            done = self._done_path(job_id)
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
