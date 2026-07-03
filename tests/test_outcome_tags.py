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


def _load_dashboard():
    spec = importlib.util.spec_from_file_location(
        "render_outcomes_dashboard", ROOT / "scripts" / "render_outcomes_dashboard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.integration
def test_dashboard_renders_every_terminal_deterministically():
    """The health panel (docs/outcomes_dashboard.html) must be a faithful,
    self-contained projection of the ledger: every terminal present, no CDN, no
    leftover format braces, byte-identical on a re-render, in BOTH the
    coverage-measured and grep-fallback modes."""
    import json, re
    rd = _load_dashboard()
    ledger = json.loads((ROOT / "docs" / "outcomes_ledger.json").read_text())
    for overlay in (None, {e["where"]: (i % 3 != 0) for i, e in enumerate(ledger)}):
        html1 = rd.render(ledger, overlay)
        html2 = rd.render(ledger, overlay)
        assert html1 == html2, "dashboard render is not deterministic"
        assert html1.count("<li class=") == len(ledger), \
            "dashboard dropped or duplicated terminals vs the ledger"
        assert "src=" not in html1.lower() or "http" not in html1.lower(), \
            "dashboard must be self-contained (no external/CDN resources)"
        assert not re.search(r"\{[a-z_]+\}", html1), \
            "unfilled Python format placeholder leaked into the HTML"
        for e in ledger:
            if e.get("code"):
                assert e["code"] in html1, f"terminal {e['code']} missing from dashboard"
    # grep-fallback mode must SAY it's unmeasured; measured mode must not.
    assert "NOT measured" in rd.render(ledger, None)
    assert "NOT measured" not in rd.render(ledger, {e["where"]: True for e in ledger})


@pytest.mark.integration
def test_seaworthy_scope_is_well_defined_and_measurable():
    """The Seaworthy milestone rests on a machine-derived load-bearing surface
    (scripts/seaworthy_scope.py). Guard its definition so the goalposts can't
    silently move: load-bearing = every proven ∪ every invariant ∪ named
    firewalls; the surface splits local/HPC; and the meter is a clean fraction."""
    import json, importlib.util
    spec = importlib.util.spec_from_file_location(
        "seaworthy_scope", ROOT / "scripts" / "seaworthy_scope.py")
    sc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sc)
    ledger = json.loads((ROOT / "docs" / "outcomes_ledger.json").read_text())

    lb = [e for e in ledger if sc.is_load_bearing(e)]
    # every proven and every invariant record must be in scope (the trust surface)
    for e in ledger:
        if e.get("outcome") == "proven" or e.get("source") == "invariant":
            assert sc.is_load_bearing(e), f"{e.get('code')} must be load-bearing"
    # named firewalls too
    for code in sc.FIREWALL_CODES:
        hits = [e for e in ledger if e.get("code") == code]
        for e in hits:
            assert sc.is_load_bearing(e), f"firewall {code} must be load-bearing"
    # every load-bearing terminal is in exactly one arena
    for e in lb:
        assert sc.arena(e) in ("local", "hpc")
    # the meter is a clean fraction, and the summary is internally consistent
    overlay = json.loads((ROOT / "docs" / "terminal_coverage.json").read_text())["by_where"]
    m = sc.summarize(ledger, overlay)
    assert m["local_total"] + m["hpc_total"] == len(lb)
    assert 0 <= m["local_certified"] <= m["local_total"]
    assert m["seaworthy_v1"] == (m["local_certified"] == m["local_total"] and m["local_total"] > 0)
    # regression: the sha256 firewall we certified must be recognised as
    # load-bearing AND certified (CERT-1). If this breaks, the milestone lost a rivet.
    sha = next(e for e in ledger if e.get("code") == "env_manager.binary_sha256_mismatch")
    assert sc.is_load_bearing(sha) and sc.is_certified(sha, overlay), \
        "the certified sha256 firewall must count toward Seaworthy"


@pytest.mark.integration
def test_measure_executed_expands_to_enclosing_stmt_not_fuzz():
    """measure_terminal_coverage._executed expands a terminal's span DOWN to its
    enclosing simple-statement start — because coverage attributes a multi-line
    `return wrapper(proven(...))` to the statement's first line, below the inner
    node the extractor anchors on. This must NOT become adjacent-line fuzz: a
    SIBLING statement running does not count."""
    import importlib.util
    from pathlib import Path as _P
    _root = _P(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "measure_terminal_coverage", _root / "scripts" / "measure_terminal_coverage.py")
    mtc = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mtc)

    # lines 10..12 belong to a statement that STARTS at line 8 (a multi-line
    # `return _summarize(proven(...))`); the terminal is anchored at 10.
    mtc._STMT_START_CACHE["fake.py"] = {8: 8, 9: 8, 10: 8, 11: 8, 12: 8, 7: 7}
    term = {"where": "fake.py:10", "end_line": 12}
    # coverage recorded only the statement-start line 8 → executed (fixes undercount).
    assert mtc._executed(term, {"fake.py": {8}}) is True
    # coverage recorded only line 7 (an ADJACENT sibling statement) → NOT executed
    # (proves enclosing-stmt, not fuzz — line 7 is a different statement).
    assert mtc._executed(term, {"fake.py": {7}}) is False
    # file not measured at all → None.
    assert mtc._executed(term, {}) is None


def test_coverage_state_is_exact_span_not_fuzzy():
    """The coverage classifier must use EXACT [line, end_line] span matching —
    the fuzz variant we rejected marked error branches 'covered' by their
    adjacent happy-path lines. Assert verified/exercised/dark are computed from
    the overlay's executed flag + named_in_test, with no line fuzzing."""
    rd = _load_dashboard()
    e_named_run = {"where": "f.py:10", "code": "x.a", "outcome": "broke",
                   "tagged": True, "named_in_test": True}
    e_run_only = {"where": "f.py:20", "code": "x.b", "outcome": "broke",
                  "tagged": True, "named_in_test": False}
    e_dark = {"where": "f.py:30", "code": "x.c", "outcome": "broke",
              "tagged": True, "named_in_test": True}
    overlay = {"f.py:10": True, "f.py:20": True, "f.py:30": False}
    assert rd._cover_state(e_named_run, overlay) == "verified"
    assert rd._cover_state(e_run_only, overlay) == "exercised"
    assert rd._cover_state(e_dark, overlay) == "dark"       # executed False → dark
    assert rd._cover_state(e_dark, None) == "unknown"        # no overlay at all
    # a terminal absent from a measured overlay counts as dark, not covered.
    assert rd._cover_state({"where": "f.py:99", "code": "x.d", "named_in_test": True},
                           overlay) == "dark"


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
    # Wave 2 (2026-07-02) — the deferred plumbing, now fully tagged.
    "agent/skills/env_manager.py",
    "agent/skills/transfer_providers.py",
    "agent/skills/transfer.py",
    "agent/skills/container_build.py",
    "agent/skills/env_build.py",
    "agent/skills/docker_builder.py",
    "agent/skills/env_recipe.py",
    "agent/skills/env_vendor.py",
    "agent/mcp_tools/workflow_tools.py",
    "agent/skills/core_test_data.py",
    "agent/skills/test_runner.py",
    "agent/skills/job_manager.py",
    "agent/skills/pipeline_state.py",
    "agent/skills/submit_workflow.py",
    "agent/skills/package_search.py",
    "agent/skills/spec_writer.py",
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
def test_rewrapping_a_tagged_result_does_not_double_tag():
    """The systemic double-tag guard (2026-07-02 wave 2). A boundary function
    routinely re-wraps an ALREADY-TAGGED inner result by spreading it —
    `broke("outer.code", **provider_result)` where provider_result itself carries
    `code`/`outcome`. `code` is POSITIONAL-ONLY in the helpers precisely so this
    can't raise `TypeError: got multiple values for argument 'code'` at call
    bind (a landmine that used to lurk on untested Docker/cluster paths). This
    test fails if anyone drops the `/` from the helper signatures."""
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
    # loop too (async submit re-wrap path).
    lp = loop("transfer.submitted_async", **inner, manifest="/m")
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
