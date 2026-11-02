"""The render proves its own pipeline: the directory as written is run once over the
sealed samples, the run's records land in the directory, the verdict in the record.

Pinned: the trace is read into tasks; the verdict is every expected task completed with
exit 0 (a non-zero Nextflow exit, a failed task, a missing task and a timeout each name
their reason); the preconditions refuse before a file is written (no runtime, an image
absent from the daemon, a sealed input missing); the tool writes the proof into the
record and the page on a pass, and takes the render back on a failure, keeping the run
for diagnosis; the proof runs the files the render wrote, in scratch, with the runtime
env's activation and the launcher's own nextflow line.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from pipeline_fixtures import fake_proof, sealed_rnaseq_spec

from agent.mcp_tools import pipeline_tools as PT
from agent.skills import pipeline_proof as PP
from agent.skills import pipeline_record as pr
from agent.skills import workspace
from agent.skills.pipeline_record import ProofTask
from agent.skills.spec_writer import write_workflow_spec

TRACE = ("task_id\tnative_id\tname\tstatus\texit\tsubmit\tstart\tcomplete\trealtime\t%cpu\tpeak_rss\tcontainer\tworkdir\n"
         "1\t100\tHISAT2 (SRR1039508)\tCOMPLETED\t0\ta\tb\tc\t1m 2s\t180.0%\t1.2 GB\tsha256:aa\t/w/ab\n"
         "2\t101\tSAMTOOLS (SRR1039508)\tCOMPLETED\t0\ta\tb\tc\t2s\t50.0%\t40 MB\tsha256:aa\t/w/bc\n"
         "3\t102\tHTSEQ_COUNT (SRR1039508)\tCOMPLETED\t0\ta\tb\tc\t30s\t99.0%\t300 MB\tsha256:aa\t/w/cd\n")


def _record():
    rt = pr.LocalRuntime(activate="/checkout/scripts/activate.sh", nextflow="/checkout/.conda_runtime/bin/nextflow")
    return pr.derive_pipeline_record(sealed_rnaseq_spec(["SRR1039508"]), name="p", local_runtime=rt)


def _task(name="X (s)", status="COMPLETED", exit="0"):
    return ProofTask(name=name, status=status, exit=exit, realtime="1s", peak_rss="1 MB", container="c")


def test_the_trace_is_read_into_tasks(tmp_path):
    p = tmp_path / "trace.txt"
    p.write_text(TRACE)
    tasks = PP.read_trace(p)
    assert [t.name for t in tasks] == ["HISAT2 (SRR1039508)", "SAMTOOLS (SRR1039508)", "HTSEQ_COUNT (SRR1039508)"]
    assert tasks[0].exit == "0" and tasks[0].peak_rss == "1.2 GB" and tasks[0].container == "sha256:aa"
    assert PP.read_trace(tmp_path / "absent.txt") == []


def test_the_verdict_is_every_expected_task_completed_with_exit_zero():
    ok = [_task(), _task("Y (s)")]
    assert PP._verdict(0, ok, 2, False)[0] == "passed"
    v, why = PP._verdict(1, ok, 2, False)
    assert v == "failed" and "Nextflow exited 1" in why and "workflow itself" in why
    v, why = PP._verdict(0, [_task(), _task("Y (s)", "FAILED", "137")], 2, False)
    assert v == "failed" and "Y (s) (FAILED, exit 137)" in why
    v, why = PP._verdict(0, ok, 3, False)
    assert v == "failed" and "finished with 2 task(s); 3 expected" in why
    v, why = PP._verdict(None, [_task()], 2, True)
    assert v == "timed_out" and "1 of 2 task(s)" in why


def test_expected_tasks_count_per_sample_stages_per_row_and_cohort_stages_once():
    rec = pr.derive_pipeline_record(sealed_rnaseq_spec(), name="p")   # 3 rows × 3 per-sample stages
    assert PP._expected_tasks(rec) == 9


class TestPreconditions:
    def test_no_runtime_refuses(self, monkeypatch):
        rec = _record()
        rec.local_runtime = None
        assert PP.check_preconditions(rec)["code"] == "pipeline.proof_no_runtime"

    def test_a_missing_image_refuses_naming_it_and_the_remedy(self, monkeypatch, tmp_path):
        from agent.skills import run_production as rp
        rec = _record()
        monkeypatch.setattr(rec, "local_runtime", pr.LocalRuntime(activate=str(tmp_path / "a.sh"), nextflow=str(tmp_path / "nf")))
        (tmp_path / "a.sh").write_text("")
        (tmp_path / "nf").write_text("")
        monkeypatch.setattr(rp, "_docker_image_present", lambda image: False)
        r = PP.check_preconditions(rec)
        assert r["code"] == "pipeline.proof_image_missing" and r["missing_images"] and "docker load" in r["remedy"]

    def test_a_missing_sealed_input_refuses_naming_it(self, monkeypatch, tmp_path):
        from agent.skills import run_production as rp
        rec = _record()
        monkeypatch.setattr(rec, "local_runtime", pr.LocalRuntime(activate=str(tmp_path / "a.sh"), nextflow=str(tmp_path / "nf")))
        (tmp_path / "a.sh").write_text("")
        (tmp_path / "nf").write_text("")
        monkeypatch.setattr(rp, "_docker_image_present", lambda image: True)
        r = PP.check_preconditions(rec)
        assert r["code"] == "pipeline.proof_inputs_missing"
        assert any(p.endswith("SRR1039508_10K_R1.fastq.gz") for p in r["missing_inputs"]), r


class TestRunProof:
    def _fake_run(self, monkeypatch, *, rc=0, trace=TRACE, timeout=False):
        seen = {}

        def run(cmd, cwd=None, env=None, capture_output=None, text=None, timeout=None):
            seen.update(cmd=cmd, cwd=Path(cwd), env=env)
            stamp = cmd[-1].rsplit(" ", 1)[-1].strip("'\"")
            if timeout_flag["hit"]:
                raise subprocess.TimeoutExpired(cmd, timeout)
            run_dir = Path(cwd) / "runs" / stamp
            run_dir.mkdir(parents=True)
            (run_dir / "trace.txt").write_text(trace)
            (run_dir / "nextflow.log").write_text("log")
            return subprocess.CompletedProcess(cmd, rc, stdout="out", stderr="err")
        timeout_flag = {"hit": timeout}
        monkeypatch.setattr(PP.subprocess, "run", run)
        return seen

    def test_the_files_the_render_wrote_run_in_scratch_and_the_records_move_into_the_directory(self, monkeypatch, tmp_path):
        rec = _record()
        pdir = tmp_path / "pipe"
        pdir.mkdir()
        (pdir / "main.nf").write_text("x")
        (pdir / "params.yaml").write_text("y")
        seen = self._fake_run(monkeypatch)
        proof = PP.run_proof(rec, pdir, files=["main.nf", "params.yaml"], timeout_s=10)
        assert proof.verdict == "passed", proof.reason
        assert seen["cwd"].is_relative_to(workspace.scratch_dir("pipeline_proofs"))
        assert (seen["cwd"] / "main.nf").read_text() == "x" and (seen["cwd"] / "params.yaml").read_text() == "y"
        assert seen["env"]["NXF_HOME"] == str(seen["cwd"] / ".nextflow_home")
        launch = seen["cmd"][-1]
        assert launch.startswith(f"source {rec.local_runtime.activate}") and "-profile local -params-file params.yaml" in launch
        assert f"--run_stamp {proof.stamp}" in launch and f"-log runs/{proof.stamp}/nextflow.log" in launch
        assert proof.run_record == f"runs/{proof.stamp}" and proof.samples == ["SRR1039508"]
        assert (pdir / proof.run_record / "trace.txt").is_file() and (pdir / proof.run_record / "nextflow.log").is_file()
        assert (pdir / proof.run_record / "proof.stdout").read_text() == "out"
        assert not (seen["cwd"] / proof.run_record).exists(), "the run's records moved, they were not copied"
        assert [t.name for t in proof.tasks] == ["HISAT2 (SRR1039508)", "SAMTOOLS (SRR1039508)", "HTSEQ_COUNT (SRR1039508)"]
        assert proof.tasks_expected == 3

    def test_a_failed_run_names_the_task_and_a_timeout_says_so(self, monkeypatch, tmp_path):
        rec = _record()
        pdir = tmp_path / "pipe"
        pdir.mkdir()
        bad = TRACE.replace("HTSEQ_COUNT (SRR1039508)\tCOMPLETED\t0", "HTSEQ_COUNT (SRR1039508)\tFAILED\t1")
        self._fake_run(monkeypatch, rc=1, trace=bad)
        proof = PP.run_proof(rec, pdir, files=[], timeout_s=10)
        assert proof.verdict == "failed" and "HTSEQ_COUNT (SRR1039508) (FAILED, exit 1)" in proof.reason
        assert proof.nextflow_exit == 1
        run = Path(proof.run_dir) / proof.run_record
        assert (run / "trace.txt").is_file() and (run / "proof.stderr").read_text() == "err", \
            "a failed run's records stay beside its work/, where the remedy points"
        assert not (pdir / "runs").exists(), "nothing of a failed proof lands in the pipeline directory"
        self._fake_run(monkeypatch, timeout=True)
        proof = PP.run_proof(rec, pdir, files=[], timeout_s=10)
        assert proof.verdict == "timed_out" and proof.nextflow_exit is None


class TestTheTool:
    @pytest.fixture
    def sealed(self) -> str:
        spec = sealed_rnaseq_spec()
        out = write_workflow_spec(spec.model_dump(), {}, env_name="rnaseq_cli")
        assert "workflow_spec_path" in out, out
        return spec.workflow_name

    def test_a_passed_proof_is_written_into_the_record_and_the_page(self, sealed, monkeypatch):
        calls = fake_proof(monkeypatch)
        out = PT.render_pipeline(sealed_workflow=sealed)
        assert out["outcome"] == "proven", out
        assert out["proof"]["verdict"] == "passed" and out["proof"]["tasks"] == 9 and out["proof"]["samples"][0] == "SRR1039508"
        assert calls[0]["files"] == out["files"]
        d = Path(out["dir"])
        rec = pr.load_pipeline_record(d / ".pipeline" / "pipeline.yaml")
        assert rec.proof is not None and rec.proof.verdict == "passed" and len(rec.proof.tasks) == 9
        page = (d / "pipeline.html").read_text()
        assert "ran over the 3 sealed samples" in page and "HISAT2 (SRR1039508)" in page
        assert (d / rec.proof.run_record / "trace.txt").is_file()
        from agent.skills.pipeline_render import parse_manifest
        manifest = parse_manifest((d / ".pipeline" / "MANIFEST.sha256").read_text())
        from agent.models.core_data import sha256_text
        assert manifest[".pipeline/pipeline.yaml"] == sha256_text((d / ".pipeline" / "pipeline.yaml").read_text())

    def test_a_failed_proof_takes_the_render_back_and_names_where_the_run_is(self, sealed, monkeypatch):
        fake_proof(monkeypatch, verdict="failed")
        out = PT.render_pipeline(sealed_workflow=sealed)
        assert out["outcome"] == "broke" and out["code"] == "pipeline.proof_failed", out
        assert out["proof"]["verdict"] == "failed" and "render again" in out["remedy"]
        assert not (workspace.pipelines_dir() / sealed).exists(), "a failed proof leaves no pipeline directory"
        run = Path(out["proof"]["run_dir"]) / out["proof"]["run_record"]
        assert (run / "trace.txt").is_file() and f"read {run}/" in out["remedy"], out["remedy"]

    def test_a_precondition_refuses_before_a_file_is_written(self, sealed, monkeypatch):
        from agent import mcp_server as _ms
        monkeypatch.setattr(_ms, "_check_docker_available", lambda: None)
        from agent.skills.outcomes import refused
        monkeypatch.setattr(PP, "check_preconditions",
                            lambda record: refused("pipeline.proof_image_missing", success=False, error="no image", remedy="load it"))
        out = PT.render_pipeline(sealed_workflow=sealed)
        assert out["outcome"] == "refused" and out["code"] == "pipeline.proof_image_missing"
        assert not (workspace.pipelines_dir() / sealed).exists()

    def test_the_tool_takes_background(self):
        from agent.skills.backgroundable import BACKGROUNDABLE
        assert BACKGROUNDABLE["render_pipeline"] == ("name", "sealed_workflow")
