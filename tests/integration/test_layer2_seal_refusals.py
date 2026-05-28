"""
Layer-2 (workflow) seal contract: seal_workflow refuses to write a
WorkflowSpec on any of:

  I0  shape sanity — top-level lists hold only dicts
  I3  validated outputs — every rc=0 step has detected_outputs AND no
                          validation uses expected_type='any'
  I6  paths/placeholders — every input/output path is absolute AND every
                           {PLACEHOLDER} in usage.command_template is
                           declared in usage.inputs (or OUTPUT_DIR/OUT_DIR)
  I7  resource_usage — every rc=0 step has wall_seconds + peak_rss_mb
  I8  composition coherence — every step input traces to a prior step's
                              outputs OR an external source (test_data,
                              reference_databases, runtime_configs,
                              authored_artifacts)

Every clause was either added because of a real failure mode the contract
needed to catch, or carried verbatim from the host-writer days. They are
the gates that let `validated == shipped` stay an honest claim.

Why integration, not unit: each invariant clause is independently
testable as a function (and is, in test_invariants.py), but the
INTEGRATION value is that one constructed spec exercises the whole
check_workflow_invariants surface. If a regression silently drops a
clause from the workflow tier, the unit tests still pass but
seal_workflow lets a violating spec through. These tests pin which
invariants fire on which violations from a single chained call.
"""
from __future__ import annotations

import pytest

from agent.skills.spec_writer import check_workflow_invariants


def _minimal_passing_spec() -> dict:
    """A spec that passes all five workflow invariants — the baseline
    each test mutates one field of."""
    return {
        "pipeline_name": "test",
        "packages": [{"name": "samtools", "verify_output": "v1.21"}],
        "install_steps": [{"step": 1, "returncode": 0}],
        "test_data": {"bam": "/abs/path/in.bam"},
        "pipeline_steps": [{
            "step": 1, "tool": "samtools",
            "command": "samtools view /abs/path/in.bam > /abs/path/out.txt",
            "returncode": 0,
            "inputs": [{"path": "/abs/path/in.bam"}],
            "detected_outputs": ["/abs/path/out.txt"],
            "validation": {"out.txt": {"valid": True, "expected_type": "txt"}},
            "resource_usage": {"wall_seconds": 1.0, "peak_rss_mb": 10.0,
                               "max_cpu_percent": 20.0},
        }],
    }


def _violations(spec: dict, invariant_prefix: str) -> list[dict]:
    return [v for v in check_workflow_invariants(spec)
            if v.get("invariant", "").startswith(invariant_prefix)]


@pytest.mark.integration
def test_baseline_spec_passes_every_workflow_invariant():
    """The fixture itself must be clean — otherwise the negative tests
    below are testing something else by accident."""
    assert check_workflow_invariants(_minimal_passing_spec()) == []


@pytest.mark.integration
def test_i3_step_with_no_outputs_refused():
    """A rc=0 step with empty detected_outputs (and no mark_step_validated)
    is the silent-empty-success trap. I3 catches it."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["detected_outputs"] = []
    spec["pipeline_steps"][0].pop("validation", None)
    v = _violations(spec, "I3.")
    assert any(x["invariant"] == "I3.pipeline_step_has_outputs" for x in v), \
        f"silent-empty-success not refused: {v}"


@pytest.mark.integration
def test_i3_expected_type_any_refused():
    """The amendment: every validation must declare a real type so the
    validator dispatches to a type-aware checker. `touch foo.bar`
    creating a non-empty file passes type='any' but fails the contract."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["validation"] = {
        "out.txt": {"valid": True, "expected_type": "any"},
    }
    v = _violations(spec, "I3.")
    assert any(x["invariant"] == "I3.declared_output_type" for x in v), \
        f"expected_type=any was not refused: {v}"


@pytest.mark.integration
def test_i6_relative_input_path_refused():
    """Relative paths are reproducibility landmines (they depend on the
    agent's CWD at finalize time)."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["inputs"] = [{"path": "data/relative/in.bam"}]
    v = _violations(spec, "I6.")
    assert any(x["invariant"] == "I6.absolute_paths" for x in v), \
        f"relative input path was not refused: {v}"


@pytest.mark.integration
def test_i6_undeclared_template_placeholder_refused():
    """`{OUPUT_DIR}` (typo for OUTPUT_DIR) silently passes the static
    path check (starts with `{`). I6 placeholder-declared check catches
    it before a trial run does."""
    spec = _minimal_passing_spec()
    spec["usage"] = {
        "command_template": "samtools view {INPUT_BAM} > {OUPUT_DIR}/out.txt",
        "inputs": [{"name": "INPUT_BAM", "format": "bam"}],
    }
    v = _violations(spec, "I6.")
    assert any(x["invariant"] == "I6.template_placeholders_declared" for x in v), \
        f"undeclared placeholder {{OUPUT_DIR}} was not refused: {v}"


@pytest.mark.integration
def test_i7_missing_resource_usage_refused():
    """psutil's wall/peak_rss observations are the proof that the runtime
    actually saw the step run. Without them an agent could synthesize a
    pipeline_step record without ever running it."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0].pop("resource_usage")
    v = _violations(spec, "I7.")
    assert any(x["invariant"] == "I7.resource_usage_recorded" for x in v), \
        f"missing resource_usage was not refused: {v}"


@pytest.mark.integration
def test_i8_orphan_input_refused():
    """An input that traces to neither external sources NOR a prior step's
    outputs is an orphan — the pipeline doesn't actually compose."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["inputs"] = [{"path": "/abs/path/orphan_input.bam"}]
    # No matching test_data, no prior step output. Should fire I8.
    v = _violations(spec, "I8.")
    assert any(x["invariant"] == "I8.composition_coherence" for x in v), \
        f"orphan input was not refused: {v}"


@pytest.mark.integration
def test_i8_authored_artifact_satisfies_provenance():
    """authored_artifacts is a legitimate provenance source — a step that
    consumes a hand-staged BAM (recorded via stage_authored_artifact)
    must NOT trip I8 just because the path isn't in test_data."""
    spec = _minimal_passing_spec()
    spec["pipeline_steps"][0]["inputs"] = [{"path": "/abs/path/authored.bam"}]
    spec["authored_artifacts"] = [
        {"path": "/abs/path/authored.bam", "role": "fixture",
         "description": "hand-staged test BAM", "sha256": "deadbeef"},
    ]
    v = _violations(spec, "I8.")
    assert v == [], f"authored_artifact path should satisfy I8: {v}"
