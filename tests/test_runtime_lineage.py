"""I8 read at run time: a recorded step's inputs that trace to nothing the draft knows
are disclosed on the run result, with the seal's own remedy.

A cohort run paid four seal round trips for inputs that were never declared — the test
reads never selected, the sliced annotation and the aligner index never registered. The
seal's walk already knew each one the moment the step was recorded; it just was not
asked until the seal. The run tools now ask it, and say what the seal would say.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent import mcp_server as m
from agent.skills.spec_writer import untraced_step_inputs


@pytest.fixture
def _runner(monkeypatch, tmp_path):
    """run_pipeline_step with the run and the validation stubbed; the step record, the
    draft and the lineage walk are real."""
    pid = "p_lineage"
    m._pipeline_state.discard(pid)
    m._pipeline_state.start(pid, "lineage test")
    monkeypatch.setattr(type(m._validator), "validate",
                        lambda self, path, etype, env_name="": {"passed": True, "expected_type": etype, "path": path})

    def _run(inputs: list[str], out_name: str):
        outp = tmp_path / out_name
        outp.write_text("x")

        def _fake_run_in_env(self, env_name, command, working_dir=None, timeout=1800, inputs=None, watch_dir=None):
            return {"returncode": 0, "stdout": "", "stderr": "", "command": command, "env_name": env_name,
                    "detected_outputs": [str(outp)],
                    "resource_usage": {"wall_seconds": 0.01, "peak_rss_mb": 1.0, "max_cpu_percent": 1.0},
                    "inputs": inputs or []}
        monkeypatch.setattr(type(m._env_mgr), "run_in_env", _fake_run_in_env)
        return m.run_pipeline_step(env_name="e", command=f"tool {' '.join(inputs)} > {outp}", pipeline_id=pid,
                                   inputs=[{"path": p} for p in inputs], watch_dir=str(tmp_path)), str(outp)
    return pid, _run


def test_an_input_nothing_declared_is_disclosed_with_the_seals_remedy(_runner, tmp_path):
    pid, run = _runner
    reads = tmp_path / "SRR1039508_10K_R1.fastq.gz"
    reads.write_text("@r1")
    out, _ = run([str(reads)], "a.bam")
    assert out["pipeline_merge"]["status"] == "merged"
    assert [u["path"] for u in out["untraced_inputs"]] == [str(reads)]
    remedy = out["untraced_inputs"][0]["remedy"]
    assert "download_reference_database" in remedy and "re-seal" in remedy
    assert "1 input of this step traces to nothing" in out["lineage_hint"] and "I8" in out["lineage_hint"]


def test_an_input_a_prior_step_produced_is_not_disclosed(_runner, tmp_path):
    pid, run = _runner
    reads = tmp_path / "r.fq"
    reads.write_text("@r1")
    _, bam = run([str(reads)], "s.bam")                  # step 1: the reads are untraced
    out, _ = run([bam], "s.counts.tsv")                  # step 2 consumes step 1's output
    assert "untraced_inputs" not in out and "lineage_hint" not in out


def test_the_reading_is_the_seals_own_walk(_runner, tmp_path):
    """One walk decides what traces, at the run and at the seal."""
    pid, run = _runner
    reads = tmp_path / "r.fq"
    reads.write_text("@r1")
    run([str(reads)], "s.bam")
    draft = m._pipeline_state.get_draft(pid)
    step_n = draft["pipeline_steps"][-1]["step"]
    assert [u["path"] for u in untraced_step_inputs(draft, step_n)] == [str(reads)]
    assert untraced_step_inputs(draft, step_n + 99) == []
