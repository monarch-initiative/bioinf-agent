"""
Drift-lint for the outcome-tag system model (agent/skills/outcomes.py +
scripts/extract_outcomes.py). These make an untagged terminal a BUILD FAILURE,
so the harvested decision-surface model can't silently drift from the code —
the same posture as the honesty contract, turned on the architecture itself.

Prototype scope: the seal subsystem (workflow_tools.seal_workflow). Widen
`_TAGGED_FUNCS` as tagging rolls out to more primitives.
"""
from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / "agent" / "mcp_tools" / "workflow_tools.py"
HELPERS = {"proven", "refused", "broke", "vanished", "degraded", "loop"}
_TAGGED_FUNCS = {"seal_workflow"}


def _fn(name: str) -> ast.FunctionDef:
    for n in ast.walk(ast.parse(WF.read_text())):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} not found in {WF}")


@pytest.mark.integration
def test_no_untagged_error_returns_in_tagged_funcs():
    """Every terminal in a tagged function goes through an outcome helper — a raw
    `return {...}` (a new, un-classified terminal) fails this test. Drift becomes
    a build break, not a silent hole."""
    offenders = []
    for name in _TAGGED_FUNCS:
        for n in ast.walk(_fn(name)):
            if isinstance(n, ast.Return) and isinstance(n.value, ast.Dict):
                offenders.append(f"{name}:{n.lineno}")
    assert not offenders, (
        "raw dict returns (must use an outcome helper from agent.skills.outcomes): "
        f"{offenders}")


@pytest.mark.integration
def test_seal_codes_unique_namespaced_and_valid():
    fn = _fn("seal_workflow")
    codes = []
    for n in ast.walk(fn):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in HELPERS:
            assert n.args and isinstance(n.args[0], ast.Constant) \
                and isinstance(n.args[0].value, str), \
                f"outcome helper at line {n.lineno} needs a string code as its first arg"
            code = n.args[0].value
            assert code.startswith("seal."), f"seal code must be namespaced 'seal.*': {code!r}"
            codes.append(code)
    assert codes, "seal_workflow has no tagged terminals — tagging regressed"
    assert len(codes) == len(set(codes)), f"duplicate seal codes: {sorted(codes)}"


def _load_extractor():
    spec = importlib.util.spec_from_file_location(
        "extract_outcomes", ROOT / "scripts" / "extract_outcomes.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Files where the outcome-tag rollout is COMPLETE — every dict-literal
# error/success terminal is tagged. Add a file here once the extractor reports
# zero untagged terminals for it; the ratchet below then keeps it that way.
FULLY_TAGGED = [
    "agent/mcp_tools/run_tools.py",
    "agent/mcp_tools/env_tools.py",
    "agent/mcp_tools/freeze_tools.py",
    "agent/mcp_tools/data_tools.py",
    "agent/mcp_tools/service_tools.py",
    "agent/mcp_tools/observability_tools.py",
    "agent/skills/run_cluster_step.py",
    "agent/skills/stage_apptainer.py",
    "agent/skills/cluster_jobs.py",
    "agent/skills/cluster_modules.py",
    "agent/skills/snapshot.py",
    "agent/skills/agent_status.py",
    "agent/skills/env_freeze.py",
    "agent/validators/output_validator.py",
]


@pytest.mark.integration
def test_fully_tagged_files_stay_fully_tagged():
    """The ratchet: a NEW raw `return {…}` error/success terminal in any
    already-rolled-out file fails the build, forcing it to be classified. This is
    what keeps the harvested system model from drifting as the code grows."""
    mod = _load_extractor()
    offenders = {}
    for rel in FULLY_TAGGED:
        untagged = [e["where"] for e in mod.harvest(ROOT / rel) if not e["tagged"]]
        if untagged:
            offenders[rel] = untagged
    assert not offenders, f"new untagged terminals in fully-tagged files: {offenders}"


@pytest.mark.integration
def test_extractor_round_trips_the_seal_terminals():
    """The extractor DERIVES the seal decision surface from source — proves the
    model is a projection of the code, not a hand-drawn thing that can drift."""
    mod = _load_extractor()
    codes = {e["code"]: e["outcome"] for e in mod.harvest(WF)}
    expected = {
        "seal.unknown_pipeline_id":    "refused",
        "seal.no_frozen_env":          "refused",
        "seal.workflow_invariants":    "refused",
        "seal.usage_self_test_failed": "refused",
        "seal.self_verify_failed":     "refused",
        "seal.spec_write_failed":      "broke",
        "seal.sealed":                 "proven",
    }
    for code, outcome in expected.items():
        assert codes.get(code) == outcome, \
            f"extractor missed/misclassified {code}: got {codes.get(code)!r}, want {outcome!r}"
