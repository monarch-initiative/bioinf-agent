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
    assert cj._pipeline_verdict([], manager_done) == "not_started"


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
    assert len(called) == 1            # the sacct query ran; the run-dir read never did
