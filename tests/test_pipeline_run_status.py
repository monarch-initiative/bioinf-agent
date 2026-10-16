"""cluster_job_status reads a rendered pipeline's run from its own records.

Under a manager model the SLURM row the caller holds is the manager's — Nextflow
submitting a job per stage and sample and doing none of the work. The verdict over the
run has to come from the tasks, which the run writes to `runs/<stamp>/trace.txt`. These
pin the trace parse, the task view, the verdict rule, and that the remote script is
built only from validated tokens.
"""
from __future__ import annotations

from agent.skills import cluster_jobs as cj

TRACE = "\n".join([
    "task_id\tnative_id\tname\tstatus\texit\tsubmit\tstart\tcomplete\trealtime\t%cpu\tpeak_rss\tcontainer\tworkdir\tscript",
    "1\t62046819\tHISAT2 (SRR1039508)\tCOMPLETED\t0\t2026-10-09 10:00:00\t2026-10-09 10:00:05\t2026-10-09 10:01:05\t1m\t380.0%\t1.2 GB\t/work/c.sif\t/work/run1/work/ab/cd\thisat2 -p 4 …",
    "2\t62046820\tHISAT2 (SRR1039509)\tFAILED\t137\t2026-10-09 10:00:00\t2026-10-09 10:00:06\t2026-10-09 10:03:00\t3m\t100.0%\t4 GB\t/work/c.sif\t/work/run1/work/ef/01\thisat2 -p 4 …",
    "3\t62046821\tSAMTOOLS (SRR1039508)\tRUNNING\t-\t2026-10-09 10:01:10\t2026-10-09 10:01:12\t-\t-\t-\t-\t/work/c.sif\t/work/run1/work/22/33\tsamtools index …",
])


def test_the_trace_is_parsed_by_its_header():
    rows = cj._parse_trace(TRACE)
    assert [r["name"] for r in rows] == ["HISAT2 (SRR1039508)", "HISAT2 (SRR1039509)", "SAMTOOLS (SRR1039508)"]
    assert rows[1]["native_id"] == "62046820" and rows[1]["exit"] == "137"
    assert cj._parse_trace("") == [] and cj._parse_trace("header only\n") == []


MULTILINE_TRACE = "\n".join([
    "task_id\tnative_id\tname\tstatus\texit\tsubmit\tstart\tcomplete\trealtime\t%cpu\tpeak_rss\tcontainer\tworkdir\tscript",
    "3\t4430692\tHISAT2 (S12)\tCOMPLETED\t0\t2026-10-09 19:33:26\t2026-10-09 19:34:34\t2026-10-09 19:34:35\t821ms\t84.5%\t11.1 MB\t/w/c.sif\t/w/run1/work/d4/04\t",
    "    hisat2 -p 4 -x chr22 -U s12.fastq.gz | samtools sort -o aligned.bam",
    "    ",
    "4\t4430693\tHISAT2 (S13)\tCOMPLETED\t0\t2026-10-09 19:33:26\t2026-10-09 19:34:34\t2026-10-09 19:34:35\t835ms\t86.6%\t10.6 MB\t/w/c.sif\t/w/run1/work/55/ba\t",
    "    hisat2 -p 4 -x chr22 -U s13.fastq.gz | samtools sort -o aligned.bam",
    "    ",
])


def test_a_field_written_over_several_lines_continues_its_record():
    """Nextflow writes `script` over several lines: the command on its own indented line,
    then a blank one, the record line ending in the tab that opens the field. Read line by line, a finished 14-task run came back as 14 tasks and
    14 blank ones, with the verdict `running`. The record is the line that opens with a
    task_id; what follows belongs to it."""
    rows = cj._parse_trace(MULTILINE_TRACE)
    assert [r["name"] for r in rows] == ["HISAT2 (S12)", "HISAT2 (S13)"]
    assert all(r["status"] == "COMPLETED" for r in rows)
    assert rows[0]["script"].startswith("hisat2 -p 4 -x chr22 -U s12.fastq.gz")
    tasks = [cj._task_view(r) for r in rows]
    assert cj._pipeline_verdict(tasks, [{"verdict": cj.SUCCEEDED}]) == "succeeded"


def test_a_task_view_carries_the_work_dir_only_when_it_did_not_complete():
    done, failed, live = (cj._task_view(r) for r in cj._parse_trace(TRACE))
    assert set(done) == {"name", "native_id", "status", "exit", "realtime", "peak_rss"}
    assert failed["workdir"] == "/work/run1/work/ef/01"
    assert live["workdir"] == "/work/run1/work/22/33"
    assert "script" not in failed and "container" not in failed


def test_the_verdict_is_over_the_tasks_never_the_manager():
    rows = cj._parse_trace(TRACE)
    manager_running = [{"verdict": cj.RUNNING}]
    manager_done = [{"verdict": cj.SUCCEEDED}]
    assert cj._pipeline_verdict([cj._task_view(r) for r in rows], manager_running) == "failed"
    ok = [cj._task_view(r) for r in rows if r["status"] == "COMPLETED"]
    assert cj._pipeline_verdict(ok, manager_running) == "running"       # tasks done so far, manager still going
    assert cj._pipeline_verdict(ok, manager_done) == "succeeded"
    assert cj._pipeline_verdict([], manager_running) == "running"
    assert cj._pipeline_verdict([], [{"verdict": cj.DIED}]) == "failed"
    assert cj._pipeline_verdict([], [{"verdict": cj.RUNNING, "state": "PENDING"}]) == "not_started"
    # a manager that ended with no task on record did not "not start": nothing says what it did
    assert cj._pipeline_verdict([], manager_done) == "unobserved"


def test_a_run_with_no_manager_log_reads_the_managers_own_row(monkeypatch):
    """No `<pipeline>-<id>.out` in run_dir is what a queued job looks like, and also
    what a job cancelled in the queue, a node that failed to boot, or a wrong run_dir
    look like. The manager's SLURM row tells them apart."""
    import subprocess
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout="NOOUT\n", stderr=""))
    env = {"type": "ssh", "host": "h", "user": "u"}
    queued = cj._read_pipeline_run(env, "/w/run1", "4242",
                                   [{"verdict": cj.RUNNING, "state": "PENDING"}], timeout=5)
    died = cj._read_pipeline_run(env, "/w/run1", "4242",
                                 [{"verdict": cj.DIED, "state": "CANCELLED by 1234"}], timeout=5)
    ended = cj._read_pipeline_run(env, "/w/run1", "4242", [{"verdict": cj.SUCCEEDED,
                                                            "state": "COMPLETED"}], timeout=5)
    assert queued["verdict"] == "not_started" and "has not started" in queued["note"]
    assert died["verdict"] == "failed"
    assert ended["verdict"] == "unobserved" and "check that run_dir" in ended["note"]


def test_the_remote_script_is_built_from_validated_tokens_only():
    cmd = cj._build_pipeline_run_cmd("/work/pipelines/run1", "4242")
    assert cmd.startswith("bash -lc ")
    assert "/work/pipelines/run1/*-4242.out" in cmd and "trace.txt" in cmd
    assert "runs/[0-9_]*" in cmd


def test_status_refuses_an_unsafe_run_dir_before_ssh(tmp_path):
    import yaml
    ap = tmp_path / "a.yaml"
    ap.write_text(yaml.safe_dump({
        "compute_envs": [{"name": "hpc", "type": "ssh", "host": "h", "user": "u"}],
        "projects": [{"name": "demo", "compute_envs": ["hpc"], "directories": []}]}))
    called = []
    import subprocess
    orig = subprocess.run

    def run(argv, **kw):
        called.append(argv)
        class R: returncode = 0; stdout = ""; stderr = ""
        return R()
    subprocess.run = run
    try:
        r = cj.cluster_job_status("demo", "hpc", "4242", run_dir="/work/run1; rm -rf /", access_path=str(ap))
    finally:
        subprocess.run = orig
    assert r["code"] == "cluster.unsafe_run_dir"
    assert called == []                # refused before any ssh, the sacct query included


# ---------------------------------------------------------------------------
# Tasks in flight, and a wait that returns on a change
# ---------------------------------------------------------------------------

def _access(tmp_path):
    import yaml
    ap = tmp_path / "a.yaml"
    ap.write_text(yaml.safe_dump({
        "compute_envs": [{"name": "hpc", "type": "ssh", "host": "h", "user": "u"}],
        "projects": [{"name": "demo", "compute_envs": ["hpc"],
                      "directories": [{"env": "hpc", "path": "/w", "permissions": ["download"]}]}]}))
    return str(ap)


def test_a_run_dir_the_project_does_not_grant_is_never_read(tmp_path, monkeypatch):
    """The run's log and trace come back through this call: reading them is a download,
    from a directory the project grants, like any other."""
    import subprocess
    called = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: called.append(a) or None)
    r = cj.cluster_job_status("demo", "hpc", "4242", run_dir="/home/someone_else/proj",
                              access_path=_access(tmp_path))
    assert r["outcome"] == "refused" and "PermissionDenied" in r["error"], r
    assert called == [], "refused before any ssh"


def _sacct(*rows):
    """sacct -P rows: JobID|State|Elapsed|ExitCode|NodeList|Reason|Start|End."""
    return "\n".join("|".join(r) for r in rows) + "\n"


_HEADER = "task_id\tnative_id\tname\tstatus\texit\tsubmit\tstart\tcomplete\trealtime\t%cpu\tpeak_rss\tcontainer\tworkdir"
_DONE = "1\t501\tBUILD (a)\tCOMPLETED\t0\t-\t-\t-\t1m\t100%\t1 GB\t/c.sif\t/w/run1/work/aa/01"
_LOG = ["Oct-15 03:05:41.953 [Task submitter] DEBUG nextflow.executor.GridTaskHandler - [SLURM] "
        f"submitted process {name} > jobId: {jid}; workDir: /w/run1/work/{wd}"
        for name, jid, wd in (("BUILD (a)", 501, "aa/01"), ("TRIALS (a)", 502, "bb/02"), ("TRIALS (b)", 503, "cc/03"))]


class _Cluster:
    """Answers the three remote commands: the manager's sacct row, the run records
    (manager log, trace, Nextflow's submitted lines) and the in-flight tasks' sacct rows."""
    def __init__(self, trace_rows, inflight_rows, *, inflight_rc=0, manager="RUNNING"):
        self.trace_rows, self.inflight_rows = list(trace_rows), inflight_rows
        self.inflight_rc, self.manager = inflight_rc, manager
        self.calls = []

    def __call__(self, argv, **kw):
        cmd = argv[-1]
        self.calls.append(cmd)
        class R: returncode = 0; stderr = ""
        if "TRACE_BEGIN" in cmd:
            submitted = [line.split("] ", 1)[1].split(" - [SLURM] ", 1)[-1] for line in _LOG]
            R.stdout = "\n".join(["OUT=/w/run1/phenologs-4242.out", "STAMP=runs/20261015_030500",
                                  "TRACE_BEGIN", _HEADER, *self.trace_rows, "TRACE_END",
                                  "SUBMITTED_BEGIN", *submitted, "SUBMITTED_END"]) + "\n"
        elif "sacct -j 4242 " in cmd:
            R.stdout = _sacct(("4242", self.manager, "01:00:00", "0:0", "c1", "None", "-", "-"))
        else:
            R.returncode = self.inflight_rc
            R.stdout = _sacct(*self.inflight_rows) if self.inflight_rc == 0 else ""
        return R()


def test_a_task_submitted_and_not_finished_is_reported_with_the_schedulers_state(tmp_path, monkeypatch):
    """trace.txt lists a task once it FINISHES: a run whose tasks compute for hours used
    to read `tasks: []` throughout. Nextflow's log names each task's SLURM job the
    moment it submits it, and sacct says what that job is doing."""
    import subprocess
    cluster = _Cluster([_DONE], [("502", "RUNNING", "02:10:00", "0:0", "c0412", "None", "-", "-"),
                                 ("503", "PENDING", "00:00:00", "0:0", "None assigned", "Resources", "-", "-")])
    monkeypatch.setattr(subprocess, "run", cluster)
    r = cj.cluster_job_status("demo", "hpc", "4242", run_dir="/w/run1", access_path=_access(tmp_path))
    tasks = {t["native_id"]: t for t in r["pipeline"]["tasks"]}
    assert tasks["501"]["status"] == "COMPLETED"
    assert tasks["502"] == {"name": "TRIALS (a)", "native_id": "502", "status": "RUNNING",
                            "elapsed": "02:10:00", "node": "c0412", "workdir": "/w/run1/work/bb/02"}
    assert tasks["503"]["status"] == "PENDING" and tasks["503"]["reason"] == "Resources"
    assert r["pipeline"]["task_summary"] == {"COMPLETED": 1, "RUNNING": 1, "PENDING": 1}
    assert r["pipeline"]["verdict"] == "running"
    assert "sacct -j 502,503 " in cluster.calls[-1], "one sacct call for the tasks in flight only"


def test_a_task_the_scheduler_cannot_be_asked_about_says_submitted(tmp_path, monkeypatch):
    import subprocess
    monkeypatch.setattr(subprocess, "run", _Cluster([_DONE], [], inflight_rc=1))
    r = cj.cluster_job_status("demo", "hpc", "4242", run_dir="/w/run1", access_path=_access(tmp_path))
    assert [t["status"] for t in r["pipeline"]["tasks"]] == ["COMPLETED", "SUBMITTED", "SUBMITTED"]


class _Clock:
    def __init__(self):
        self.now = 1000.0
    def monotonic(self):
        return self.now
    def sleep(self, s):
        self.now += s


def test_a_wait_returns_when_a_task_changes(tmp_path, monkeypatch):
    import subprocess
    clock = _Clock()
    monkeypatch.setattr(cj.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cj.time, "sleep", clock.sleep)
    cluster = _Cluster([_DONE], [("502", "PENDING", "0", "0:0", "", "Priority", "-", "-"),
                                 ("503", "PENDING", "0", "0:0", "", "Priority", "-", "-")])
    monkeypatch.setattr(subprocess, "run", cluster)
    looks = {"n": 0}
    orig_observe = cj._observe

    def observe(*a, **kw):
        looks["n"] += 1
        if looks["n"] == 3:             # the third look: task 502 has started
            cluster.inflight_rows[0] = ("502", "RUNNING", "00:00:05", "0:0", "c7", "None", "-", "-")
        return orig_observe(*a, **kw)
    monkeypatch.setattr(cj, "_observe", observe)
    r = cj.cluster_job_status("demo", "hpc", "4242", run_dir="/w/run1", wait_s=300,
                              access_path=_access(tmp_path))
    assert looks["n"] == 3 and r["waited_s"] == 60
    assert {t["native_id"]: t["status"] for t in r["pipeline"]["tasks"]}["502"] == "RUNNING"


def test_a_wait_never_runs_past_its_cap_and_a_settled_run_returns_at_once(tmp_path, monkeypatch):
    import subprocess
    clock = _Clock()
    monkeypatch.setattr(cj.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cj.time, "sleep", clock.sleep)
    monkeypatch.setattr(subprocess, "run", _Cluster([_DONE], [("502", "PENDING", "0", "0:0", "", "Priority", "-", "-"),
                                                              ("503", "PENDING", "0", "0:0", "", "Priority", "-", "-")]))
    r = cj.cluster_job_status("demo", "hpc", "4242", run_dir="/w/run1", wait_s=10_000,
                              access_path=_access(tmp_path))
    assert r["waited_s"] == 540, "capped under the stream watchdog"

    done = [_DONE.replace("\t501\t", f"\t{j}\t").replace("BUILD (a)", n) for j, n in
            (("501", "BUILD (a)"), ("502", "TRIALS (a)"), ("503", "TRIALS (b)"))]
    monkeypatch.setattr(subprocess, "run", _Cluster(done, [], manager="COMPLETED"))
    start = clock.now
    r = cj.cluster_job_status("demo", "hpc", "4242", run_dir="/w/run1", wait_s=300,
                              access_path=_access(tmp_path))
    assert r["pipeline"]["verdict"] == "succeeded" and clock.now == start and r["waited_s"] == 0


def test_a_resumed_task_is_done_and_a_dead_manager_never_reads_as_success():
    cached = [{"status": "CACHED"}, {"status": "COMPLETED"}]
    assert cj._pipeline_verdict(cached, [{"verdict": cj.SUCCEEDED}]) == "succeeded"
    assert cj._pipeline_verdict(cached, [{"verdict": cj.DIED}]) == "failed", \
        "a manager killed mid-run cannot submit what is left"
    assert "workdir" not in cj._task_view({"status": "CACHED", "workdir": "/w/x"})


def test_a_wait_returns_at_once_when_the_manager_is_over(tmp_path, monkeypatch):
    """A cancelled manager, or a resumed run whose tasks all came from the cache, has
    nothing left to change: the wait does not sit out its time on it."""
    import subprocess
    clock = _Clock()
    monkeypatch.setattr(cj.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cj.time, "sleep", clock.sleep)
    monkeypatch.setattr(subprocess, "run", _Cluster([_DONE], [], manager="CANCELLED"))
    r = cj.cluster_job_status("demo", "hpc", "4242", run_dir="/w/run1", wait_s=300,
                              access_path=_access(tmp_path))
    assert r["waited_s"] == 0 and r["pipeline"]["verdict"] == "failed"


def test_a_slow_look_never_carries_a_wait_past_its_cap(tmp_path, monkeypatch):
    import subprocess
    clock = _Clock()
    monkeypatch.setattr(cj.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(cj.time, "sleep", clock.sleep)
    monkeypatch.setattr(subprocess, "run", _Cluster([_DONE], [("502", "PENDING", "0", "0:0", "", "Priority", "-", "-"),
                                                              ("503", "PENDING", "0", "0:0", "", "Priority", "-", "-")]))
    orig = cj._observe

    def slow(*a, **kw):
        clock.now += 55                 # every look takes 55 s of ssh
        return orig(*a, **kw)
    monkeypatch.setattr(cj, "_observe", slow)
    r = cj.cluster_job_status("demo", "hpc", "4242", run_dir="/w/run1", wait_s=10_000,
                              access_path=_access(tmp_path))
    assert r["waited_s"] <= 540


def test_the_schedulers_word_is_kept_apart_from_nextflows(tmp_path, monkeypatch):
    """sacct can call a job over before Nextflow has checked the task's outputs; the
    task is still Nextflow's SUBMITTED, with the scheduler's state beside it."""
    import subprocess
    monkeypatch.setattr(subprocess, "run", _Cluster([_DONE], [("502", "COMPLETED", "01:00:00", "0:0", "c1", "None", "-", "-"),
                                                              ("503", "FAILED", "00:01:00", "1:0", "c2", "None", "-", "-")]))
    r = cj.cluster_job_status("demo", "hpc", "4242", run_dir="/w/run1", access_path=_access(tmp_path))
    flight = {t["native_id"]: t for t in r["pipeline"]["tasks"] if t["native_id"] != "501"}
    assert {j: (t["status"], t["scheduler_state"]) for j, t in flight.items()} == {
        "502": ("SUBMITTED", "COMPLETED"), "503": ("SUBMITTED", "FAILED")}
    assert r["pipeline"]["verdict"] == "running", "Nextflow has not called these tasks yet"

    monkeypatch.setattr(subprocess, "run", _Cluster([_DONE], [], inflight_rc=1))
    r = cj.cluster_job_status("demo", "hpc", "4242", run_dir="/w/run1", access_path=_access(tmp_path))
    assert "sacct for the tasks in flight failed" in r["pipeline"]["scheduler_unread"]
