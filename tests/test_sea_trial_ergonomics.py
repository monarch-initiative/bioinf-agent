"""Sea-trial ergonomic legibility — machine-verify the two footguns the
unattended seqkit sea trial surfaced (SEA-TRIAL-1).

The sea trial (a fresh subagent driving start→install→freeze→seal on seqkit with
no hand-holding) PASSED the honesty contract cleanly: no false greens, and two
LEGITIMATE seal refusals (I6 then I4) that forced real fixes. But both refusals
were ILLEGIBLE — a full-auto agent had to read spec_writer.py source to learn why.
The fixes make the failures self-explanatory at the point of failure WITHOUT
weakening any check. These tests lock that legibility in so it can't regress:

  `run_pipeline_step` — rc=0 with nothing detected under watch_dir returns
  `output_detection_hint` pointing at watch_dir (the silent-empty trap; would
  otherwise fail I3 at seal with no explanation). The output-slot convention itself is
  pinned in tests/test_usage_slot_grammar.py.
"""
from __future__ import annotations

from pathlib import Path

import pytest



# ── 2. run_pipeline_step surfaces a hint on the silent-empty trap ───────────
def _patch_state(monkeypatch):
    import agent.mcp_server as ms
    monkeypatch.setattr(ms._env_mgr, "hash_outputs", lambda outs: {}, raising=False)
    monkeypatch.setattr(ms._pipeline_state, "add_step",
                        lambda pid, data, replace_step=0: 0, raising=False)
    monkeypatch.setattr(ms._pipeline_state, "add_validation",
                        lambda *a, **k: None, raising=False)


def test_run_pipeline_step_hints_when_output_undetected(monkeypatch):
    """rc=0, caller declared output_types, but nothing detected (written via `-o`
    outside watch_dir) → `output_detection_hint` pointing at watch_dir. Not an
    error; a self-correction cue that prevents a silent I3 failure at seal."""
    import agent.mcp_server as ms
    from agent.mcp_tools import run_tools as R
    _patch_state(monkeypatch)
    monkeypatch.setattr(ms._env_mgr, "run_in_env",
                        lambda *a, **k: {"returncode": 0, "detected_outputs": [], "inputs": [],
                                         "resource_usage": {"wall_seconds": 0.1, "peak_rss_mb": 1.0,
                                                            "max_cpu_percent": 1.0}},
                        raising=False)

    out = R.run_pipeline_step(env_name="x", command="seqkit stats -T -o /elsewhere/s.tsv in.fq",
                              pipeline_id="p", inputs=["in.fq"], output_types={".tsv": "tsv"})

    assert "output_detection_hint" in out
    assert "watch_dir" in out["output_detection_hint"]
    assert out.get("output_types_unmatched") == [".tsv"]


def test_run_pipeline_step_no_hint_when_output_detected(monkeypatch):
    """Negative control: a detected+validated output ⇒ no hint (the cue is scoped
    to the actual trap, not noise on every call)."""
    import agent.mcp_server as ms
    from agent.mcp_tools import run_tools as R
    _patch_state(monkeypatch)
    monkeypatch.setattr(ms._env_mgr, "run_in_env",
                        lambda *a, **k: {"returncode": 0,
                                         "detected_outputs": ["/w/s.tsv"], "inputs": [],
                                         "resource_usage": {"wall_seconds": 0.1, "peak_rss_mb": 1.0,
                                                            "max_cpu_percent": 1.0}},
                        raising=False)
    monkeypatch.setattr(ms._validator, "validate",
                        lambda *a, **k: {"passed": True, "validation_method": "tsv_parse"},
                        raising=False)

    out = R.run_pipeline_step(env_name="x", command="seqkit stats -T -o /w/s.tsv in.fq",
                              pipeline_id="p", inputs=["in.fq"],
                              output_types={"/w/s.tsv": "tsv"})

    assert "output_detection_hint" not in out
