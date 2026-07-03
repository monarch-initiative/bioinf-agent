"""Sea-trial ergonomic legibility — machine-verify the two footguns the
unattended seqkit sea trial surfaced (SEA-TRIAL-1).

The sea trial (a fresh subagent driving start→install→freeze→seal on seqkit with
no hand-holding) PASSED the honesty contract cleanly: no false greens, and two
LEGITIMATE seal refusals (I6 then I4) that forced real fixes. But both refusals
were ILLEGIBLE — a full-auto agent had to read spec_writer.py source to learn why.
The fixes make the failures self-explanatory at the point of failure WITHOUT
weakening any check. These tests lock that legibility in so it can't regress:

  1. `_is_output_slot` — the (previously duplicated, now single-source) I4 output
     placeholder convention. `{OUT_TSV}` is NOT an output slot → the trap.
  2. `_run_one_trial` — when an output goes through an unrecognized slot (produced
     nothing in the scratch dir), the refusal now carries `hint` +
     `recognized_output_slots` naming the convention and the fix.
  3. `run_pipeline_step` — rc=0 with an expected-but-undetected output now returns
     `output_detection_hint` pointing at watch_dir (the silent-empty trap; would
     otherwise fail I3 at seal with no explanation).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.skills import spec_writer
from agent.skills.spec_writer import _is_output_slot, _run_one_trial


# ── 1. the output-slot convention, in one tested place ──────────────────────
@pytest.mark.parametrize("slot,expected", [
    ("OUTPUT_DIR", True),
    ("OUT_DIR", True),
    ("MY_OUTPUT", True),        # contains 'output'
    ("output_path", True),
    ("OUT_TSV", False),         # the sea-trial trap — reads like output, isn't recognized
    ("OUTFILE", False),         # 'out' alone is not the convention
    ("FASTQ", False),
    ("REF", False),
])
def test_is_output_slot_convention(slot, expected):
    assert _is_output_slot(slot) is expected


class _FakeEM:
    """A stand-in env_manager: 'runs' the command by optionally writing a file
    INTO watch_dir (the scratch dir), then returns rc=0."""
    def __init__(self, write_rel: str | None = None):
        self.write_rel = write_rel

    def run_in_env(self, env_name, command, timeout=None, watch_dir=None, **kw):
        if self.write_rel and watch_dir:
            (Path(watch_dir) / self.write_rel).write_text("c1\tc2\n1\t2\n")
        return {"returncode": 0, "stdout": "", "stderr": ""}


# ── 2. the I4 refusal is now legible for the unrecognized-slot trap ─────────
def test_i4_unrecognized_output_slot_refusal_is_legible():
    """`-o {OUT_TSV}` → the file lands outside the scratch dir → produced_files
    empty. The refusal must now DISCLOSE the convention (hint) + which slots were
    recognized (none), not just an opaque `produced_files: []`."""
    plan = {"name": "t1", "substitutions": {
        "FASTQ": "/data/in.fq", "OUT_TSV": "/data/outside/stats.tsv"}}
    template = "seqkit stats -T -o {OUT_TSV} {FASTQ}"
    placeholders = {"FASTQ", "OUT_TSV"}
    outputs_spec = [{"name": "OUT_TSV", "files": ["*.tsv"], "type": "tsv"}]

    res = _run_one_trial(plan, template, placeholders, outputs_spec,
                         _FakeEM(write_rel=None), "x", {"pipeline_name": "st"}, None)

    assert res["ok"] is False
    assert res["reason"] == "command ran but expected outputs missing"
    assert res["produced_files"] == []
    assert res["recognized_output_slots"] == []           # the smoking gun, surfaced
    assert "hint" in res
    assert "OUTPUT_DIR" in res["hint"]                     # names the fix
    assert "OUT_TSV" in res["hint"]                        # names the trap concretely


def test_i4_recognized_output_dir_slot_passes():
    """Positive control: the documented `{OUTPUT_DIR}/stats.tsv` idiom writes into
    the scratch dir and the trial passes — proving the fix path works, so the hint
    above points somewhere real."""
    plan = {"name": "t1", "substitutions": {"FASTQ": "/data/in.fq"}}
    template = "seqkit stats -T -o {OUTPUT_DIR}/stats.tsv {FASTQ}"
    placeholders = {"FASTQ", "OUTPUT_DIR"}
    outputs_spec = [{"name": "OUTPUT_DIR", "files": ["*.tsv"], "type": "tsv"}]

    res = _run_one_trial(plan, template, placeholders, outputs_spec,
                         _FakeEM(write_rel="stats.tsv"), "x", {"pipeline_name": "st"}, None)

    assert res["ok"] is True, res


# ── 3. run_pipeline_step surfaces a hint on the silent-empty trap ───────────
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
                        lambda *a, **k: {"returncode": 0, "detected_outputs": [], "inputs": []},
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
                                         "detected_outputs": ["/w/s.tsv"], "inputs": []},
                        raising=False)
    monkeypatch.setattr(ms._validator, "validate",
                        lambda *a, **k: {"passed": True, "validation_method": "tsv_parse"},
                        raising=False)

    out = R.run_pipeline_step(env_name="x", command="seqkit stats -T -o /w/s.tsv in.fq",
                              pipeline_id="p", inputs=["in.fq"],
                              output_types={"/w/s.tsv": "tsv"})

    assert "output_detection_hint" not in out
