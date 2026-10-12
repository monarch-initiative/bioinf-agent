"""
Drift-lint for the outcome-tag vocabulary (agent/skills/outcomes.py +
scripts/extract_outcomes.py). An untagged terminal anywhere in the swept
primitive modules is a BUILD FAILURE, so the decision surface the code can
reach never drifts from the vocabulary that classifies it — the honesty
contract's posture, turned on the architecture itself.
"""
from __future__ import annotations

import ast
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / "agent" / "mcp_tools" / "workflow_tools.py"
EXTRACTOR = ROOT / "scripts" / "extract_outcomes.py"
HELPERS = {"proven", "refused", "broke", "vanished", "degraded", "loop"}


def _fn(name: str) -> ast.FunctionDef:
    for n in ast.walk(ast.parse(WF.read_text())):
        if isinstance(n, ast.FunctionDef) and n.name == name:
            return n
    raise AssertionError(f"{name} not found in {WF}")


def _load_extractor():
    spec = importlib.util.spec_from_file_location("extract_outcomes", EXTRACTOR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


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


@pytest.mark.integration
def test_every_swept_file_is_fully_tagged():
    """The ratchet: a raw `return {…}` carrying an error/success key, anywhere in
    agent/mcp_tools, agent/skills or agent/validators, fails the build until it is
    classified through an outcome helper. No allow-list of files — a terminal that
    is not in the vocabulary is a terminal the caller cannot branch on and the
    reports cannot name."""
    mod = _load_extractor()
    untagged: dict[str, list[str]] = {}
    for e in mod.sweep():
        if not e["tagged"]:
            untagged.setdefault(e["where"].rsplit(":", 1)[0], []).append(
                f"{e['where']} ({e['func']})")
    assert not untagged, (
        "untagged terminals — wrap each `return {...}` in proven/refused/broke/"
        f"vanished/degraded/loop from agent.skills.outcomes: {untagged}")


def _snapshot(d: Path) -> dict[str, int]:
    return {str(p): p.stat().st_mtime_ns for p in d.rglob("*") if p.is_file()}


@pytest.mark.integration
def test_the_extractor_is_a_report_that_refuses_unknown_arguments():
    """A plain run prints the tally and exits 0; an unrecognised flag exits 2 with
    usage and does not fall through to the default action. Neither writes a byte
    under docs/."""
    before = _snapshot(ROOT / "docs")
    bad = subprocess.run([sys.executable, str(EXTRACTOR), "--bogus"],
                         capture_output=True, text=True, cwd=ROOT)
    assert bad.returncode == 2 and "usage:" in bad.stderr, (bad.returncode, bad.stderr)
    ok = subprocess.run([sys.executable, str(EXTRACTOR)],
                        capture_output=True, text=True, cwd=ROOT)
    assert ok.returncode == 0, ok.stderr
    assert "SYSTEM DECISION SURFACE" in ok.stdout
    assert _snapshot(ROOT / "docs") == before, "the report must not write under docs/"


@pytest.mark.integration
def test_rewrapping_a_tagged_result_does_not_double_tag():
    """The systemic double-tag guard. A boundary function routinely re-wraps an
    ALREADY-TAGGED inner result by spreading it — `broke("outer.code",
    **provider_result)` where provider_result itself carries `code`/`outcome`.
    `code` is POSITIONAL-ONLY in the helpers precisely so this can't raise
    `TypeError: got multiple values for argument 'code'` at call bind. This test
    fails if anyone drops the `/` from the helper signatures."""
    from agent.skills.outcomes import broke, proven, loop
    inner = broke("transfer.scp_upload_failed", success=False,
                  error="boom", returncode=1)
    # bare spread of a tagged dict — must NOT raise; OUTER code wins.
    out = broke("transfer.provider_failed", **inner)
    assert out["code"] == "transfer.provider_failed"
    assert out["outcome"] == "broke"
    assert out["error"] == "boom" and out["returncode"] == 1
    # dict-literal merge form (env_build style) — also safe.
    merged = broke("env_build.start_failed", **{**inner, "success": False,
                                                 "stage": "start"})
    assert merged["code"] == "env_build.start_failed"
    assert merged["stage"] == "start"
    # a proven re-wrap flips the class of a formerly-broke inner.
    up = proven("run.ok", **broke("inner.fail", x=1))
    assert up["outcome"] == "proven" and up["code"] == "run.ok" and up["x"] == 1
    # loop too (the poll-again re-wrap path).
    lp = loop("transfer.task_still_running", **inner, manifest="/m")
    assert lp["outcome"] == "loop" and lp["manifest"] == "/m"


@pytest.mark.integration
def test_outcome_helpers_keep_code_positional_only():
    """Guard the mechanism directly: every public helper must declare `code` as
    positional-only. If the `/` is removed, the double-tag TypeError class comes
    back — so assert the signature, not just the behavior above."""
    import inspect
    from agent.skills import outcomes
    for name in outcomes.HELPER_NAMES:
        sig = inspect.signature(getattr(outcomes, name))
        kinds = [p.kind for p in sig.parameters.values()]
        assert inspect.Parameter.POSITIONAL_ONLY in kinds, \
            f"{name}(): `code` must be POSITIONAL-ONLY (keep the `/`) to avoid " \
            f"the double-tag collision; got {kinds}"


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
