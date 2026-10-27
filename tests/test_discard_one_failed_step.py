"""A failed step can be dropped from a draft without discarding the draft.

A recorded step that exited non-zero makes the draft seal as a failed run; until now the
only ways out were to re-run it under the same number or to discard the whole draft. A
step that ran clean is evidence and stays.
"""
from __future__ import annotations

from agent.skills.pipeline_state import PipelineState


def _state_with(steps):
    ps = PipelineState({})
    ps.start("drop_test", "t")
    for rc in steps:
        ps.add_step("drop_test", {"tool": "t", "command": "t", "returncode": rc, "inputs": [],
                                  "detected_outputs": [],
                                  "resource_usage": {"wall_seconds": 0.1, "peak_rss_mb": 1.0,
                                                     "max_cpu_percent": 1.0}})
    return ps


def test_a_failed_step_is_dropped_and_the_rest_renumbered():
    ps = _state_with([0, 1, 0])
    assert ps.remove_step("drop_test", 2) == {"ok": True, "remaining": 2}
    steps = ps.get_draft("drop_test")["pipeline_steps"]
    assert [(s["step"], s["returncode"]) for s in steps] == [(1, 0), (2, 0)]
    ps.discard("drop_test")


def test_a_clean_step_is_evidence_and_stays():
    ps = _state_with([0, 1])
    r = ps.remove_step("drop_test", 1)
    assert r["ok"] is False and "step=1" in r["reason"] and "evidence" in r["reason"]
    assert len(ps.get_draft("drop_test")["pipeline_steps"]) == 2
    r = ps.remove_step("drop_test", 5)
    assert r["ok"] is False and "no step 5" in r["reason"]
    assert ps.remove_step("no_such_draft", 1)["ok"] is False
    ps.discard("drop_test")


def test_the_tool_drops_one_step_or_refuses_naming_why(monkeypatch):
    from agent import mcp_server as m
    from agent.mcp_tools import workflow_tools as WT
    ps = _state_with([1, 0])
    monkeypatch.setattr(m, "_pipeline_state", ps)
    kept = WT.discard_pipeline_draft("drop_test", step=2)
    assert kept["outcome"] == "refused" and kept["code"] == "discard_draft.step_kept"
    dropped = WT.discard_pipeline_draft("drop_test", step=1)
    assert dropped["code"] == "discard_draft.step_dropped" and dropped["remaining_steps"] == 1
    assert ps.get_draft("drop_test")["pipeline_steps"][0]["step"] == 1
    ps.discard("drop_test")
