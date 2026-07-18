"""The plan corpus — does a typed ExecutionPlan gate to the right VERDICT?

WHAT THIS MEASURES, AND HOW IT RELATES TO tests/test_intent_gate.py.

    test_intent_gate.py  — the FRONT GATE: does the completeness gate route ONE typed
        RequestIntent to the right outcome + rail? Single-rail.

    THIS FILE            — the PLAN GATE: does the composition gate accept/reject a
        typed ExecutionPlan (a DAG of rail/ride invocations)? It is FAST and
        deterministic because gate_plan is pure code over a typed record — no network,
        no LLM. It is Layer-2's I8 lifted to authoring time.

Every row is docs/design_reverse_theme_park.md §6/§7 made executable. The design's
"worst case" (scenario 15) is the headline row; it must gate GREEN as a 5-node DAG
(3× INSTALL_ENV → RUN_STEP → SEAL — install_env ⊕ run_step + the seal terminal).

DISCIPLINE (inherited from the intent corpus, each rule bought with a real defect):
  - EXECUTABLE, NOT PROSE.        A test, not a document.
  - DECLARE THE GAPS.            What the ADVISORY plan gate does NOT do (dispatch,
                                 type-compatibility, undeclared-input detection) is
                                 asserted, not silently absent.
  - PRINT THE METER.            N/N stated, not buried.
  - ONE TRUTH.                  PlanRide ∩ Rail = ∅; the external vocabulary and the
                                produces-map are single-sourced (asserted).
"""

from __future__ import annotations

import pytest

from agent.skills.intent import Rail
from agent.skills.plan import (
    PlanRide,
    ProducesKind,
    _PRODUCES_FOR_ACTION,
    _SOURCE_ACTIONS,
    gate_plan,
    parse_plan,
)
from agent.skills.spec_writer import EXTERNAL_SOURCE_KINDS, I8_STATEMENT


# ---------------------------------------------------------------------------
# Authoring helpers — state every field explicitly (the test is the producer, so it
# fills the whole canonical shape; no hidden defaults).
# ---------------------------------------------------------------------------

def _tool(name, version=None, source_hint=None, purpose=None) -> dict:
    return {"name": name, "version": version, "source_hint": source_hint, "purpose": purpose}


def _unknown(field, findable, reason) -> dict:
    return {"field": field, "findable": findable, "reason": reason}


def _intent(*, kind, raw_prompt, tools=None, target_env=None, compute="unspecified",
            data_ops=None, unknowns=None, out_of_scope_reason=None) -> dict:
    return {
        "kind": kind, "raw_prompt": raw_prompt,
        "tools": tools if tools is not None else [],
        "target_env": target_env, "compute": compute, "data_ops": data_ops,
        "unknowns": unknowns if unknowns is not None else [],
        "out_of_scope_reason": out_of_scope_reason,
    }


def _rail_step(step_id, intent_dict, consumes=None) -> dict:
    return {"id": step_id, "intent": intent_dict, "seam": None,
            "consumes": consumes if consumes is not None else []}


def _seam_step(step_id, ride, consumes=None) -> dict:
    return {"id": step_id, "intent": None, "seam": ride,
            "consumes": consumes if consumes is not None else []}


def _from(step_id) -> dict:
    return {"from_step": step_id, "external": None}


def _ext(kind, ref) -> dict:
    return {"from_step": None, "external": {"kind": kind, "ref": ref}}


def _plan(*, statement, produces, steps) -> dict:
    return {"goal": {"statement": statement, "produces": produces}, "steps": steps}


def _install(step_id, name, version) -> dict:
    return _rail_step(step_id, _intent(
        kind="install_env", raw_prompt=f"install {name} {version}",
        tools=[_tool(name, version)]))


# ---------------------------------------------------------------------------
# The worst case (§6 / §7 scenario 15) — 5 nodes, the headline acceptance row.
# ---------------------------------------------------------------------------

def _worst_case() -> dict:
    return _plan(
        statement="xyz results from a sealed x->y->z pipeline on cluster xyz over project data",
        produces="workflow_spec",
        steps=[
            _install("n1", "toolx", "1.0"),
            _install("n2", "tooly", "2.0"),
            _install("n3", "toolz", "3.0"),
            _rail_step("n4", _intent(kind="run_step",
                                     raw_prompt="run the x->y->z pipeline on the cluster",
                                     compute="cluster"),
                       consumes=[_from("n1"), _from("n2"), _from("n3"),
                                 _ext("test_data", "project:/proj/xyz/reads")]),
            _seam_step("n5", "SEAL", consumes=[_from("n4")]),
        ],
    )


# ---------------------------------------------------------------------------
# The corpus — one row per §6/§7 composite scenario.
# id, plan-dict, expected plan_ready, expected topo (or None to skip), note
# ---------------------------------------------------------------------------

ROWS: list[tuple[str, dict, bool, object, str]] = [
    (
        "worst-case-5-nodes-green",
        _worst_case(),
        True, ["n1", "n2", "n3", "n4", "n5"],
        "§6 worst case: install x,y,z as 3 envs → run pipeline on cluster+project data → SEAL; "
        "gates GREEN; n1..n3 are the parallel frontier; sole sink n5 produces the goal workflow_spec",
    ),
    (
        "per-step-chain-green",
        _plan(
            statement="chained x->y->z, each step in its own env, sealed",
            produces="workflow_spec",
            steps=[
                _install("ex", "toolx", "1.0"),
                _install("ey", "tooly", "2.0"),
                _install("ez", "toolz", "3.0"),
                _rail_step("rx", _intent(kind="run_step", raw_prompt="run x"),
                           consumes=[_from("ex"), _ext("test_data", "core:chr22_actb")]),
                _rail_step("ry", _intent(kind="run_step", raw_prompt="run y on x's output"),
                           consumes=[_from("ey"), _from("rx")]),
                _rail_step("rz", _intent(kind="run_step", raw_prompt="run z on y's output"),
                           consumes=[_from("ez"), _from("ry")]),
                _seam_step("seal", "SEAL", consumes=[_from("rz")]),
            ],
        ),
        True, None,
        "the model handles fine-grained per-tool RUN nodes chaining x→y→z (each consuming the "
        "prior run's file_set) — the real DAG, not just the coarse single-RUN abstraction",
    ),
    (
        "single-rail-plan-is-legal",
        _plan(statement="just install samtools 1.21", produces="env_digest",
              steps=[_install("n1", "samtools", "1.21")]),
        True, ["n1"],
        "a 1-node plan is exactly as legal as a 6-node one (a 6-step plan is as trustworthy as a "
        "1-step one); guards against a gate wrongly demanding ≥2 nodes or a mandatory seal",
    ),
    (
        "run-against-existing-env-green",
        _plan(statement="run sort against my frozen rnaseq env", produces="file_set",
              steps=[_rail_step("n1", _intent(kind="run_step",
                                              raw_prompt="run step 2 (sort) on env rnaseq_x",
                                              target_env="rnaseq_x"))]),
        True, ["n1"],
        "a RUN node with no consumes but a target_env is NOT an orphan — the existing frozen env "
        "is a valid input source; proves env-binding via target_env, no new external kind",
    ),
    (
        "reject-cycle",
        _plan(statement="a depends on b depends on a", produces="workflow_spec",
              steps=[
                  _rail_step("a", _intent(kind="run_step", raw_prompt="a", target_env="e"),
                             consumes=[_from("b")]),
                  _seam_step("b", "SEAL", consumes=[_from("a")]),
              ]),
        False, [],
        "a dependency cycle is not a DAG → PLAN.cycle; topo_order empty",
    ),
    (
        "reject-dangling-input",
        _plan(statement="seal consumes a step that doesn't exist", produces="workflow_spec",
              steps=[_seam_step("n5", "SEAL", consumes=[_from("ghost")])]),
        False, None,
        "a from_step naming no real node → I8_authoring.dangling_dependency",
    ),
    (
        "reject-orphan-input",
        _plan(statement="a run step that consumes nothing and binds no env", produces="file_set",
              steps=[_rail_step("n1", _intent(kind="run_step", raw_prompt="run something"))]),
        False, None,
        "a non-source node (RUN_STEP) with empty consumes AND no target_env → "
        "I8_authoring.orphan_input (the lifted silent-empty trap)",
    ),
    (
        "reject-external-unknown-kind",
        _plan(statement="a run consuming an unrecognized external kind", produces="file_set",
              steps=[_rail_step("n1", _intent(kind="run_step", raw_prompt="run"),
                                consumes=[_ext("scratch_junk", "whatever")])]),
        False, None,
        "an external.kind not in the single-sourced EXTERNAL_SOURCE_KINDS → "
        "I8_authoring.external_unknown_kind",
    ),
    (
        "reject-sub-intent-ask",
        _plan(statement="install a tool with an unfindable version", produces="env_digest",
              steps=[_rail_step("n1", _intent(
                  kind="install_env", raw_prompt="install X 3.4",
                  tools=[_tool("X", "3.4")],
                  unknowns=[_unknown("X.version", False, "3.4 matches both 3.4.1 and 3.4.2")]))]),
        False, None,
        "a sub-intent that gate()s to ASK (unfindable gap) → PLAN.sub_intent_blocked; the plan "
        "reuses intent.gate() as the one proceed-ability truth",
    ),
    (
        "allow-sub-intent-investigate",
        _plan(statement="install a tool whose version is findable", produces="env_digest",
              steps=[_rail_step("n1", _intent(
                  kind="install_env", raw_prompt="install the latest scanpy",
                  tools=[_tool("scanpy")],
                  unknowns=[_unknown("scanpy.version", True, "resolve to newest, record it")]))]),
        True, ["n1"],
        "a sub-intent that gate()s to INVESTIGATE (findable gap) does NOT block — the ride fills "
        "it while walking; the plan is authorable while versions still resolve",
    ),
    (
        "reject-goal-unreached",
        _plan(statement="install a tool but claim the goal is a sealed workflow", produces="workflow_spec",
              steps=[_install("n1", "samtools", "1.21")]),
        False, None,
        "the sole sink produces env_digest but goal.produces is workflow_spec → "
        "I8_authoring.goal_unreached (the itinerary doesn't terminate at the requested outcome)",
    ),
]


# ---------------------------------------------------------------------------
# 1. the corpus — every §6/§7 row gates as the acceptance table says
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_plan_gates_to_its_intended_verdict(row):
    rid, raw, want_ready, want_topo, note = row
    plan = parse_plan(raw)  # also asserts the plan is a VALID record
    result = gate_plan(plan)
    assert result.plan_ready is want_ready, (
        f"{rid}: plan_ready is {result.plan_ready}, wanted {want_ready}\n"
        f"  violations: {[(v.invariant, v.node_id) for v in result.violations]}\n  why: {note}")
    if want_topo is not None:
        assert result.topo_order == want_topo, (
            f"{rid}: topo_order {result.topo_order}, wanted {want_topo}\n  why: {note}")


def test_worst_case_frontier_and_pending_are_exactly_right():
    """The headline row deserves more than a green: the parallel frontier is the 3
    independent installs, nothing pends, and the sole sink reaches the goal."""
    result = gate_plan(parse_plan(_worst_case()))
    assert result.plan_ready is True
    assert result.ready_frontier == ["n1", "n2", "n3"]
    assert result.pending_investigation == []
    assert result.goal_reached is True
    assert result.violations == []


def test_investigate_row_surfaces_the_node_non_blocking():
    """The allow-INVESTIGATE ruling is load-bearing: the findable gap must appear in
    pending_investigation with its node + unknowns, and NOT as a violation."""
    result = gate_plan(parse_plan(ROWS[9][1]))  # allow-sub-intent-investigate
    assert result.plan_ready is True
    assert len(result.pending_investigation) == 1
    p = result.pending_investigation[0]
    assert p["node_id"] == "n1"
    assert p["unknowns"] and p["unknowns"][0]["field"] == "scanpy.version"


# ---------------------------------------------------------------------------
# 2. the seam — the intake refuses a malformed / fabricated / two-truth plan
# ---------------------------------------------------------------------------

def test_seam_rejects_step_with_both_intent_and_seam():
    bad = _plan(statement="g", produces="workflow_spec",
                steps=[{"id": "n1",
                        "intent": _intent(kind="run_step", raw_prompt="x", target_env="e"),
                        "seam": "SEAL", "consumes": []}])
    with pytest.raises(Exception):
        parse_plan(bad)


def test_seam_rejects_step_with_neither_intent_nor_seam():
    bad = _plan(statement="g", produces="workflow_spec",
                steps=[{"id": "n1", "intent": None, "seam": None, "consumes": []}])
    with pytest.raises(Exception):
        parse_plan(bad)


def test_seam_rejects_a_decline_or_clarify_rail_node():
    """out_of_scope / ambiguous route to the gate's non-buildable rails — a plan node
    can never carry one (the whole plan is PROCEED-able rails + the SEAL ride)."""
    for kind, extra in (("out_of_scope", {"out_of_scope_reason": "not ours"}), ("ambiguous", {})):
        bad = _plan(statement="g", produces="workflow_spec",
                    steps=[_rail_step("n1", _intent(kind=kind, raw_prompt="x", **extra))])
        with pytest.raises(Exception):
            parse_plan(bad)


def test_seam_rejects_a_consume_with_both_or_neither_leg():
    both = _plan(statement="g", produces="workflow_spec",
                 steps=[_seam_step("n1", "SEAL",
                                   consumes=[{"from_step": "a", "external": {"kind": "test_data", "ref": "r"}}])])
    neither = _plan(statement="g", produces="workflow_spec",
                    steps=[_seam_step("n1", "SEAL", consumes=[{"from_step": None, "external": None}])])
    for bad in (both, neither):
        with pytest.raises(Exception):
            parse_plan(bad)


def test_seam_rejects_a_bogus_ride():
    bad = _plan(statement="g", produces="workflow_spec",
                steps=[{"id": "n1", "intent": None, "seam": "FROBNICATE", "consumes": []}])
    with pytest.raises(Exception):
        parse_plan(bad)


def test_seam_rejects_an_undeclared_key():
    bad = _worst_case()
    bad["steps"][0]["sneaky_extra"] = "x"
    with pytest.raises(Exception):
        parse_plan(bad)


# ---------------------------------------------------------------------------
# 3. derivation — produces / depends_on / action are DERIVED, never stored
# ---------------------------------------------------------------------------

def test_produces_is_derived_not_stored():
    """An author cannot lie about what a node emits — sending a `produces` key is an
    extra='forbid' error (produces is a computed property, not a field)."""
    bad = _plan(statement="g", produces="workflow_spec",
                steps=[{"id": "n1", "intent": None, "seam": "SEAL",
                        "consumes": [], "produces": "env_digest"}])
    with pytest.raises(Exception):
        parse_plan(bad)


def test_depends_on_is_derived_not_stored():
    bad = _plan(statement="g", produces="workflow_spec",
                steps=[{"id": "n1", "intent": None, "seam": "SEAL",
                        "consumes": [], "depends_on": ["x"]}])
    with pytest.raises(Exception):
        parse_plan(bad)


def test_derived_properties_match_the_consumes_and_action():
    plan = parse_plan(_worst_case())
    n4 = next(s for s in plan.steps if s.id == "n4")
    n5 = next(s for s in plan.steps if s.id == "n5")
    assert n4.action is Rail.RUN_STEP and n4.produces is ProducesKind.file_set
    assert sorted(n4.depends_on) == ["n1", "n2", "n3"]
    assert n5.action is PlanRide.SEAL and n5.produces is ProducesKind.workflow_spec
    assert n5.depends_on == ["n4"]


# ---------------------------------------------------------------------------
# 4. one truth — the anti-drift asserts
# ---------------------------------------------------------------------------

def test_planride_is_disjoint_from_rail():
    """PlanRide is the delta over Rail, not a re-listing — a grep for a rail name must
    still land on intent.Rail alone."""
    assert {r.value for r in Rail} & {r.value for r in PlanRide} == set()


def test_produces_map_is_total_over_buildable_actions():
    """Every buildable Rail (all but the two gate routes) + every PlanRide has a
    produces. A new rail/ride without one is a loud KeyError, never a silent hole."""
    for rail in Rail:
        if rail in (Rail.DECLINE, Rail.CLARIFY):
            assert rail not in _PRODUCES_FOR_ACTION, "gate routes are not buildable actions"
            continue
        assert rail in _PRODUCES_FOR_ACTION, f"rail {rail.value} has no produces mapping"
    for ride in PlanRide:
        assert ride in _PRODUCES_FOR_ACTION, f"ride {ride.value} has no produces mapping"


def test_external_vocabulary_is_single_sourced_with_spec_writer():
    """The plan gate reads the SAME external-source set the runtime seal-time I8 does —
    each recognized kind is accepted, an unrecognized one is rejected. Proves plan-time
    and seal-time cannot drift on what counts as an external source."""
    for kind in EXTERNAL_SOURCE_KINDS:
        plan = parse_plan(_plan(
            statement="g", produces="file_set",
            steps=[_rail_step("n1", _intent(kind="run_step", raw_prompt="x"),
                              consumes=[_ext(kind, "ref")])]))
        result = gate_plan(plan)
        assert not any(v.invariant == "I8_authoring.external_unknown_kind" for v in result.violations), \
            f"{kind} is in EXTERNAL_SOURCE_KINDS but the plan gate rejected it"
    # and the I8 sentence is the shared one
    assert I8_STATEMENT and "external source" in I8_STATEMENT


def test_source_actions_are_the_no_input_rails():
    """A source action legitimately consumes nothing; every other buildable action
    must trace its input. This encodes which rails START a plan."""
    assert Rail.INSTALL_ENV in _SOURCE_ACTIONS
    assert Rail.RUN_STEP not in _SOURCE_ACTIONS      # needs inputs
    assert Rail.ADD_TO_ENV not in _SOURCE_ACTIONS    # needs an env
    assert PlanRide.SEAL not in _SOURCE_ACTIONS      # needs a run


# ---------------------------------------------------------------------------
# 5. declared gaps — what the ADVISORY plan gate deliberately does NOT do
# ---------------------------------------------------------------------------

def test_known_gaps_are_declared_not_discovered_later():
    """A gate silent about its blind spots reads as 'this is everything.' State them.

    - NO DISPATCH. Phase 4 authors + gates; it never runs a node. There is no
      execute/dispatch symbol — building one (a loop that calls freeze/run/seal
      internally) is the forbidden composite primitive, deferred to a later phase.
    - NO EXECUTOR SCHEMA. ProducesKind is a coarse artifact-CLASS, not a concrete
      digest/path handle — no pre-declared schema for the unbuilt auto-executor.
    - NECESSARY-NOT-SUFFICIENT. A GREEN plan is a fast-fail preview, never an
      authorization: the gate cannot see an UNDECLARED input (a command that secretly
      reads an un-listed file passes here, caught only by concrete-path seal I8).
      spec_writer.I8 stays the authority.
    """
    import agent.skills.plan as plan_mod
    exported = set(dir(plan_mod))
    for forbidden in ("execute_plan", "dispatch_plan", "run_plan", "walk_plan"):
        assert forbidden not in exported, (
            f"plan.py exports {forbidden!r} — Phase 4 authors + gates, it does not execute; "
            "an auto-dispatcher is the forbidden composite primitive (later phase)")
    # ProducesKind carries no concrete-handle slot (it is an enum of artifact classes)
    assert {p.value for p in ProducesKind} == {"env_digest", "file_set", "transfer", "workflow_spec"}, (
        "ProducesKind grew a concrete digest/path member — that is executor scaffolding, "
        "not an authoring-time artifact class")


def test_type_compatibility_is_a_declared_gap_not_a_silent_green():
    """PROVENANCE-NOT-TYPE, made executable. The gate checks that every input TRACES to
    something (a prior node or a recognized external kind); it deliberately does NOT
    check that the input is the right KIND for the action — because runtime I8
    (spec_writer._check_composition_coherence) is itself pure provenance, and a
    per-action type matrix would be a second description of ride I/O that drifts.

    Consequence (found by adversarial review): a SEAL node consuming an INSTALL_ENV's
    env_digest DIRECTLY — instead of a run's file_set — gates GREEN here. That is a
    semantically-incoherent itinerary, but it is INSIDE the declared necessary-not-
    sufficient boundary: seal_workflow (concrete-evidence I8) is the authority and
    rejects it at walk time (you cannot seal an env as if it were a validated run).

    This test PINS that boundary: if someone later adds a per-action requires_kind check
    (a legitimate scope decision, not made here), this assertion fails and forces them to
    update the declared gap — the type gap can never become a silent green OR a silent
    tightening."""
    seal_eats_env = _plan(
        statement="seal consuming an env directly instead of a run", produces="workflow_spec",
        steps=[
            _install("n1", "toolx", "1.0"),
            _seam_step("n2", "SEAL", consumes=[_from("n1")]),  # SEAL <- env_digest, not a file_set
        ])
    result = gate_plan(parse_plan(seal_eats_env))
    assert result.plan_ready is True, (
        "the plan gate is provenance-not-type BY DESIGN — a SEAL-consumes-env plan gates "
        "green at authoring time; seal_workflow is the authority that rejects it. If this "
        "now reds, a per-action type check was added — update this declared gap deliberately")


# ---------------------------------------------------------------------------
# 6. the meter — state the number, don't bury it
# ---------------------------------------------------------------------------

def test_the_meter_is_visible():
    passed = 0
    for rid, raw, want_ready, want_topo, _ in ROWS:
        result = gate_plan(parse_plan(raw))
        ok = result.plan_ready is want_ready
        if want_topo is not None:
            ok = ok and result.topo_order == want_topo
        if ok:
            passed += 1
    print(f"\nPLAN GATE CORPUS: {passed}/{len(ROWS)} §6/§7 composite scenarios gate as intended "
          f"(worst case = 5-node DAG: install_env ⊕ run_step + SEAL)")
    assert passed == len(ROWS)
