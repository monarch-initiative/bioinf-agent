"""RUN_STEP-of-a-sealed-workflow — the typed read-back seam (Phase 5, scenario 5).

WHAT THIS MEASURES. Re-running one recorded step of an existing SEALED pipeline
(docs/design_reverse_theme_park.md §7 scenario 5: "just generate step 2 of my
existing pipeline") has to source the command we are about to EXECUTE in a shipped
image from a VALIDATED record — never a scraped dict. This file proves the two seam
functions and the advisory tool built for it:

    spec_writer.load_workflow_spec(path)   — the typed reader (inverse of
        write_workflow_spec; the WorkflowSpec analog of parse_intent / parse_plan).
    spec_writer.select_pipeline_step(spec, n) — pick one recorded step by number.
    workflow_tools.describe_sealed_step(name, step) — the advisory MCP surface: returns
        the runnable facts (command / inputs / freeze_request_key / pinned digest /
        input-existence preconditions). DISPATCHES NOTHING — run_step_in_container stays
        the sole executor with its honesty gate.

DISCIPLINE (inherited from test_intent_gate / test_plan_gate):
  - EXECUTABLE, NOT PROSE.  A test, not a document.
  - THE SEAM MUST REFUSE.   A malformed sealed spec fails at load_workflow_spec, loudly
                            — that is the entire reason we read back through the model
                            instead of scraping (the bcftools-1.23.1 lesson).
  - NO SILENT EMPTY.        A bad step number self-corrects against the real roster; it
                            never selects nothing.
"""
from __future__ import annotations

import pytest
import yaml

from agent import mcp_server as ms
from agent.mcp_tools import sealed_tools as ST
from agent.models.core_data import WorkflowSpec
from agent.skills.spec_writer import load_workflow_spec, select_pipeline_step


# ---------------------------------------------------------------------------
# authoring helpers — build a REAL WorkflowSpec the way seal writes it
# ---------------------------------------------------------------------------

def _spec_dict(name: str = "talos_wf", steps=None) -> dict:
    """A minimal but VALID WorkflowSpec dict — every required, no-default field
    present (workflow_name/description/created_at/env_*/pipeline_status). This is the
    shape write_workflow_spec validates and dumps, so a round-trip is authentic."""
    if steps is None:
        steps = [
            {"step": 1, "tool": "bcftools",
             "command": "bcftools view /data/in.vcf -o /data/step1.vcf",
             "inputs": [{"path": "/data/in.vcf", "references": []}],
             "returncode": 0, "validation_status": "passed"},
            {"step": 2, "tool": "echtvar",
             "command": "echtvar anno /data/step1.vcf /data/step2.vcf",
             "inputs": [{"path": "/data/step1.vcf"}],   # a str-coercible / dict input
             "returncode": 0, "validation_status": "passed"},
        ]
    return {
        "workflow_name":      name,
        "description":        "a two-step test workflow",
        "created_at":         "2026-07-18",
        "env_request_key":    "req_talos_abc123",
        "env_content_digest": "sha256:deadbeefcafe",
        "env_image":          "bioinf_talos@sha256:deadbeefcafe",
        "pipeline_status":    "fully_validated",
        "pipeline_steps":     steps,
    }


def _write_spec(dir_, spec_dict: dict) -> str:
    """Write a spec exactly as write_workflow_spec does: validate → model_dump →
    yaml → {name}.workflow.yaml. Returns the path."""
    wf = WorkflowSpec.model_validate(spec_dict)
    path = dir_ / f"{wf.workflow_name}.workflow.yaml"
    path.write_text(yaml.dump(wf.model_dump(exclude_none=True),
                              default_flow_style=False, sort_keys=False))
    return str(path)


@pytest.fixture()
def reports_dir(tmp_path, monkeypatch):
    """Point the tool's reports dir at a hermetic tmp dir. describe_sealed_step reads
    `project_root / config['paths']['pipelines_dir']`; an ABSOLUTE pipelines_dir wins
    the pathlib join, so this fully redirects the read off the live env_reports/."""
    d = tmp_path / "env_reports"
    d.mkdir()
    monkeypatch.setitem(ms.config["paths"], "pipelines_dir", str(d))
    return d


# ---------------------------------------------------------------------------
# 1. the typed reader — round-trips, and REFUSES a malformed artifact
# ---------------------------------------------------------------------------

def test_load_workflow_spec_round_trips_a_written_spec(tmp_path):
    path = _write_spec(tmp_path, _spec_dict())
    spec = load_workflow_spec(path)
    # It is a TYPED WorkflowSpec, not a dict — the anti-scrape property.
    assert isinstance(spec, WorkflowSpec)
    assert spec.workflow_name == "talos_wf"
    assert [s.step for s in spec.pipeline_steps] == [1, 2]
    # A command is reached as a typed attribute, never a dict .get()
    assert spec.pipeline_steps[1].command == "echtvar anno /data/step1.vcf /data/step2.vcf"
    assert spec.env_request_key == "req_talos_abc123"


def test_load_workflow_spec_preserves_runtime_extra_keys(tmp_path):
    """WorkflowSpec is extra='allow'; the runtime-authored extras a real seal writes
    onto a step (detected_outputs, container_image_digest) must ride back untouched —
    the reader validates the declared fields without dropping the rest."""
    sd = _spec_dict()
    sd["pipeline_steps"][0]["detected_outputs"] = ["/data/step1.vcf"]
    sd["pipeline_steps"][0]["container_image_digest"] = "sha256:deadbeefcafe"
    path = _write_spec(tmp_path, sd)
    spec = load_workflow_spec(path)
    st = spec.pipeline_steps[0]
    assert st.detected_outputs == ["/data/step1.vcf"]          # extra, preserved
    assert st.container_image_digest == "sha256:deadbeefcafe"  # extra, preserved


def test_load_workflow_spec_refuses_a_spec_missing_a_required_field(tmp_path):
    """THE seam property: a sealed spec that no longer satisfies the model fails HERE
    (pydantic), not as a bad command deep in a container run. pipeline_status is
    required, no default (the outputs=[] discipline)."""
    sd = _spec_dict()
    del sd["pipeline_status"]
    path = tmp_path / "broken.workflow.yaml"
    path.write_text(yaml.dump(sd))
    with pytest.raises(Exception):   # pydantic.ValidationError
        load_workflow_spec(path)


def test_load_workflow_spec_refuses_a_non_mapping_document(tmp_path):
    """A YAML list (or scalar) is not a WorkflowSpec — refuse with a clear ValueError,
    never model_validate a non-dict into a confusing error."""
    path = tmp_path / "list.workflow.yaml"
    path.write_text(yaml.dump(["not", "a", "spec"]))
    with pytest.raises(ValueError):
        load_workflow_spec(path)


# ---------------------------------------------------------------------------
# 2. the selector — pick by number; a miss self-corrects, never selects nothing
# ---------------------------------------------------------------------------

def test_select_pipeline_step_picks_the_right_step(tmp_path):
    spec = load_workflow_spec(_write_spec(tmp_path, _spec_dict()))
    st = select_pipeline_step(spec, 2)
    assert st.step == 2 and st.tool == "echtvar"


def test_select_pipeline_step_out_of_range_names_the_roster(tmp_path):
    spec = load_workflow_spec(_write_spec(tmp_path, _spec_dict()))
    with pytest.raises(ValueError) as ei:
        select_pipeline_step(spec, 99)
    # the error names the AVAILABLE numbers — a wrong guess corrects itself
    assert "[1, 2]" in str(ei.value)


# ---------------------------------------------------------------------------
# 3. the advisory tool — the runnable facts, dispatching nothing
# ---------------------------------------------------------------------------

def test_describe_sealed_step_returns_runnable_facts(reports_dir):
    _write_spec(reports_dir, _spec_dict())
    r = ST.describe_sealed_step("talos_wf", 2)
    assert r["ok"] is True
    assert r["run_with"] == "run_step_in_container"
    assert r["freeze_request_key"] == "req_talos_abc123"     # what run_step_in_container consumes
    assert r["command"] == "echtvar anno /data/step1.vcf /data/step2.vcf"
    assert r["inputs"] == ["/data/step1.vcf"]
    assert r["tool"] == "echtvar"
    assert r["step"] == 2
    # the roster is always present for context
    assert [s["step"] for s in r["steps"]] == [1, 2]
    # off the honesty namespace — a read, not an outcome (no proven/refused tag key)
    assert "outcome" not in r


def test_describe_sealed_step_preconditions_are_honest_about_missing_inputs(reports_dir, tmp_path):
    """NO AUTO-MATERIALIZE: the tool reports which recorded inputs exist RIGHT NOW; a
    missing upstream output is loud here, before a container run is spent on it."""
    present = tmp_path / "present_input.vcf"
    present.write_text("##fileformat=VCFv4.2\n")
    steps = [
        {"step": 1, "tool": "bcftools",
         "command": f"bcftools view {present} /data/missing.vcf",
         "inputs": [{"path": str(present)}, {"path": "/data/does_not_exist.vcf"}],
         "returncode": 0, "validation_status": "passed"},
    ]
    _write_spec(reports_dir, _spec_dict(steps=steps))
    r = ST.describe_sealed_step("talos_wf", 1)
    assert r["ok"] is True
    by_path = {p["path"]: p["exists"] for p in r["preconditions"]}
    assert by_path[str(present)] is True
    assert by_path["/data/does_not_exist.vcf"] is False
    assert r["all_inputs_present"] is False


def test_describe_sealed_step_discloses_the_pinned_env_state(reports_dir):
    """The env this step needs is looked up the SAME way run_step_in_container will
    (lookup_verified). A req key not in the live cache → available False, so a
    stale/evicted env is visible before any run is attempted."""
    _write_spec(reports_dir, _spec_dict())
    r = ST.describe_sealed_step("talos_wf", 1)
    assert r["pinned_env"]["request_key"] == "req_talos_abc123"
    assert r["pinned_env"]["available"] is False        # not a real frozen env
    assert r["pinned_env"]["contract_ok"] is False


def test_describe_sealed_step_unknown_workflow_lists_the_alternatives(reports_dir):
    _write_spec(reports_dir, _spec_dict(name="talos_wf"))
    _write_spec(reports_dir, _spec_dict(name="samtools_wf"))
    r = ST.describe_sealed_step("no_such_zzz", 1)
    assert r["ok"] is False
    assert set(r["available_workflows"]) == {"talos_wf", "samtools_wf"}


def test_describe_sealed_step_out_of_range_step_lists_the_roster(reports_dir):
    _write_spec(reports_dir, _spec_dict())
    r = ST.describe_sealed_step("talos_wf", 99)
    assert r["ok"] is False
    assert r["available_steps"] == [1, 2]
    assert [s["step"] for s in r["steps"]] == [1, 2]


def test_describe_sealed_step_refuses_a_corrupt_spec_instead_of_executing_past_it(reports_dir):
    """A malformed artifact on disk must fail at the typed seam — the tool surfaces the
    validation error, it never scrapes a command out of a spec that didn't parse."""
    (reports_dir / "corrupt.workflow.yaml").write_text(
        yaml.dump({"workflow_name": "corrupt", "pipeline_steps": [{"step": 1}]}))
    r = ST.describe_sealed_step("corrupt", 1)
    assert r["ok"] is False
    assert "not a valid WorkflowSpec" in r["error"]


# ---------------------------------------------------------------------------
# 4. the meter — state the number, don't bury it
# ---------------------------------------------------------------------------

def test_the_meter_is_visible():
    # count the seam+tool behaviours pinned in THIS file (kept in sync by hand — a
    # deliberate low-tech tally, like the sibling corpora)
    pinned = 12
    print(f"\nSEALED-STEP READER: {pinned} behaviours pinned "
          f"(typed reader round-trip + refusal · selector · describe_sealed_step "
          f"facts/preconditions/pinned-env/miss-paths) — RUN_STEP-of-a-sealed-workflow, "
          f"scenario 5")
    assert pinned == 12
