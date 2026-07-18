"""The gate corpus — does a typed RequestIntent reach the right OUTCOME and RAIL?

WHAT THIS MEASURES, AND HOW IT RELATES TO tests/live/test_intent_corpus.py.

    test_intent_corpus.py (live)  — the RESOLVE ride's decision: given a tool name,
        does resolve() pick the right tier / identity? It is LIVE because resolve() is
        deterministic code that hits real registries. It measures ONE ride.

    THIS FILE (fast)              — the FRONT GATE's decision: given the LLM's typed
        reading of a prompt, does the completeness gate route it to the right outcome
        (decline/ask/investigate/proceed) and rail? It is FAST and deterministic because
        the gate is pure code over a typed record — no network, no LLM.

They are complementary halves of the INPUT side, and the boundary between them is real
and worth stating: prompt → RequestIntent is the LLM's job (interpretation), so it CANNOT
be live-probed the way resolve() can — there is no code that turns a prompt into an intent.
What we CAN test deterministically is the half the design puts in code: the gate's routing
over a hand-authored intent. That is this corpus. (A future LLM-in-the-loop eval could
close the prompt → intent half; it would be a live test, and it is not this one.)

DISCIPLINE (inherited from the intent corpus, each rule bought with a real defect):
  - EXECUTABLE, NOT PROSE. This is a test, not a document. A map that isn't executable
    rots (tier 7 deleted 2,653 lines of a hand-authored decision tree that did).
  - DECLARE THE GAPS. Scenarios the single-rail gate does NOT handle (14, 15) are stated
    in `test_known_gaps_are_declared`, not silently absent — a corpus that shows only
    what it can measure reads as "this is everything," which is a lie.
  - PRINT THE METER. `test_the_meter_is_visible` states N/N rather than burying it.

Every row below is one row of docs/design_reverse_theme_park.md §7 (the acceptance
surface). The design is only real if every row routes as the table says.
"""

from __future__ import annotations

import pytest

from agent.skills.intent import (
    Kind,
    Outcome,
    Rail,
    gate,
    parse_intent,
    route,
)


# ---------------------------------------------------------------------------
# Authoring helper — states EVERY field explicitly (no hidden defaults; the
# no-fabricated-default rule is about the producer, and here the test is the
# producer, so it fills the whole canonical shape).
# ---------------------------------------------------------------------------

def _intent(
    *,
    kind: str,
    raw_prompt: str,
    tools=None,
    target_env=None,
    compute: str = "unspecified",
    data_ops=None,
    unknowns=None,
    out_of_scope_reason=None,
) -> dict:
    return {
        "kind": kind,
        "raw_prompt": raw_prompt,
        "tools": tools if tools is not None else [],
        "target_env": target_env,
        "compute": compute,
        "data_ops": data_ops,
        "unknowns": unknowns if unknowns is not None else [],
        "out_of_scope_reason": out_of_scope_reason,
    }


def _tool(name, version=None, source_hint=None, purpose=None) -> dict:
    return {"name": name, "version": version, "source_hint": source_hint, "purpose": purpose}


def _unknown(field, findable, reason) -> dict:
    return {"field": field, "findable": findable, "reason": reason}


# ---------------------------------------------------------------------------
# The corpus — one entry per §7 scenario (concrete rail-kinds; the deferred
# composite/ride-level scenarios 14-15 are asserted as declared gaps below).
# ---------------------------------------------------------------------------

# id, intent-dict, expected Outcome, expected Rail, note
ROWS: list[tuple[str, dict, Outcome, Rail, str]] = [
    (
        "s1-repo-and-version",
        _intent(
            kind="install_env", raw_prompt="Install talos from github.com/X/talos, v11",
            tools=[_tool("talos", "11", {"kind": "github_repo", "value": "github.com/X/talos"})],
        ),
        Outcome.proceed, Rail.INSTALL_ENV,
        "repo + version given → nothing to find; source_hint pins the repo, skips discovery",
    ),
    (
        "s2-multi-tool-one-env",
        _intent(
            kind="install_env", raw_prompt="I want samtools, bcftools, and bwa in one env",
            tools=[_tool("samtools"), _tool("bcftools"), _tool("bwa")],
        ),
        Outcome.proceed, Rail.INSTALL_ENV,
        "3 tools, one env → PROCEED; 3× RESOLVE/INSTALL → one FREEZE (already supported)",
    ),
    (
        "s3-near-miss-version",
        _intent(
            kind="install_env", raw_prompt="Install bwa 0.7",
            tools=[_tool("bwa", "0.7")],
            unknowns=[_unknown("bwa.version", True,
                               "0.7 is a clean prefix of exactly one real release (0.7.17); resolvable")],
        ),
        Outcome.investigate, Rail.INSTALL_ENV,
        "version imprecise but findable → INVESTIGATE; ride resolves 0.7→0.7.17 and records it",
    ),
    (
        "s3b-ambiguous-near-miss",
        _intent(
            kind="install_env", raw_prompt="Install X 3.4",
            tools=[_tool("X", "3.4")],
            unknowns=[_unknown("X.version", False,
                               "3.4 matches BOTH 3.4.1 and 3.4.2; cannot silently pick one")],
        ),
        Outcome.ask, Rail.INSTALL_ENV,
        "two candidates → the LLM marks it un-findable → ASK (won't silently choose)",
    ),
    (
        "s4-transposed-versions",
        _intent(
            kind="install_env", raw_prompt="Install bwa=1.21, samtools=0.7.17",
            tools=[_tool("bwa", "1.21"), _tool("samtools", "0.7.17")],
            unknowns=[
                _unknown("bwa.version", False,
                         "bwa has no 1.21 but samtools does; looks transposed — do not auto-correct"),
                _unknown("samtools.version", False,
                         "samtools has no 0.7.17 but bwa does; looks transposed — do not auto-correct"),
            ],
        ),
        Outcome.ask, Rail.INSTALL_ENV,
        "judgment over two fact-sets sees the swap; both un-findable → ASK 'did you swap these?'",
    ),
    (
        "s5a-step-of-pipeline-clear",
        _intent(
            kind="run_step", raw_prompt="Run step 2 (sort) of my rnaseq pipeline on env rnaseq_x",
            target_env="rnaseq_x",
        ),
        Outcome.proceed, Rail.RUN_STEP,
        "step + env both clear → PROCEED against a frozen env (the NEW rail)",
    ),
    (
        "s5b-step-of-pipeline-unclear",
        _intent(
            kind="run_step", raw_prompt="Just generate step 2 of my existing pipeline",
            target_env=None,
            unknowns=[_unknown("target_env", False,
                               "which pipeline / which frozen env? only the user knows")],
        ),
        Outcome.ask, Rail.RUN_STEP,
        "which artifact is un-findable → ASK; same rail",
    ),
    (
        "s6-transfer-specified",
        _intent(
            kind="transfer_data", raw_prompt="Move /data/run1/*.fastq.gz to the cluster scratch dir",
            data_ops=[{"direction": "upload", "source": "/data/run1/*.fastq.gz",
                       "dest": "cluster:scratch"}],
        ),
        Outcome.proceed, Rail.TRANSFER_DATA,
        "source + dest given → PROCEED; NO install spun up at all (tools=[])",
    ),
    (
        "s6b-transfer-dest-unknown",
        _intent(
            kind="transfer_data", raw_prompt="Move these FASTQs to the cluster",
            data_ops=[{"direction": "upload", "source": "these FASTQs", "dest": None}],
            unknowns=[_unknown("data_op.dest", False, "which cluster directory? only the user knows")],
        ),
        Outcome.ask, Rail.TRANSFER_DATA,
        "destination un-findable → ASK; still the transfer rail, still no install",
    ),
    (
        "s7-out-of-scope",
        _intent(
            kind="out_of_scope", raw_prompt="Write me a poem",
            out_of_scope_reason="Not a tool install / run / transfer / reproduce request",
        ),
        Outcome.decline, Rail.DECLINE,
        "out of scope → DECLINE; explain the boundary, do nothing",
    ),
    (
        "s8-no-tool-named",
        _intent(
            kind="ambiguous", raw_prompt="Install that aligner",
            unknowns=[_unknown("tool_name", False, "no tool named; cannot invent which aligner")],
        ),
        Outcome.ask, Rail.CLARIFY,
        "intent itself underspecified → ASK one targeted question",
    ),
    (
        "s9-name-collision",
        _intent(
            kind="install_env", raw_prompt="Install cellranger for scRNA cell calling",
            tools=[_tool("cellranger", purpose="scRNA cell calling")],
            unknowns=[_unknown("cellranger.identity", True,
                               "name collides with a CRAN range parser; purpose + RESOLVE facts disambiguate")],
        ),
        Outcome.investigate, Rail.INSTALL_ENV,
        "identity risk is findable (purpose + facts) → INVESTIGATE; ride confirms + records the anchor",
    ),
    (
        "s10-version-omitted",
        _intent(
            kind="install_env", raw_prompt="Install the latest scanpy",
            tools=[_tool("scanpy", version=None)],
            unknowns=[_unknown("scanpy.version", True, "user said 'latest'; resolve to newest release, record it")],
        ),
        Outcome.investigate, Rail.INSTALL_ENV,
        "version omitted but findable → INVESTIGATE (find latest, record it) — NOT a silent default",
    ),
    (
        "s11-reproduce",
        _intent(
            kind="reproduce", raw_prompt="Rebuild the env from last week's recipe",
            target_env="last_week_recipe",
        ),
        Outcome.proceed, Rail.REPRODUCE,
        "recipe named → PROCEED → verify_env_recipe digest-check",
    ),
    (
        "s12-add-to-env",
        _intent(
            kind="add_to_env", raw_prompt="Add multiqc to my talos env",
            tools=[_tool("multiqc")], target_env="talos",
        ),
        Outcome.proceed, Rail.ADD_TO_ENV,
        "tool + target env clear → PROCEED → re-freeze",
    ),
    (
        "s13-gated-license",
        _intent(
            kind="install_env", raw_prompt="Install [gated tool], I have a license",
            tools=[_tool("gated_tool")],
            unknowns=[_unknown("license.acceptance", True,
                               "tool is gated; confirm license posture before delivery (I13 gates the tarball)")],
        ),
        Outcome.investigate, Rail.INSTALL_ENV,
        "license posture is findable → INVESTIGATE; the DELIVERY gate (I13) is an EXIT concern, not the front gate",
    ),
]


# ---------------------------------------------------------------------------
# 1. the corpus — every §7 row routes as the acceptance table says
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_scenario_routes_to_its_intended_outcome_and_rail(row):
    """THE question: does this typed intent reach the outcome + rail §7 demands?"""
    rid, raw, want_outcome, want_rail, note = row
    intent = parse_intent(raw)  # also asserts the intent is a VALID record
    result = gate(intent)
    assert result.outcome is want_outcome, (
        f"{rid}: gate outcome is {result.outcome.value!r}, §7 wants {want_outcome.value!r}\n"
        f"  prompt: {raw['raw_prompt']!r}\n  why: {note}")
    assert result.rail is want_rail, (
        f"{rid}: routed to {result.rail.value!r}, §7 wants {want_rail.value!r}\n"
        f"  prompt: {raw['raw_prompt']!r}\n  why: {note}")


# ---------------------------------------------------------------------------
# 2. the seam — the intake refuses a malformed / fabricated intent
# ---------------------------------------------------------------------------

def test_seam_rejects_an_undeclared_key():
    """extra='forbid': a producer inventing a key is a loud parse-time error, not
    silent drift (the ShippedBinary lesson, applied to the intake)."""
    bad = ROWS[0][1] | {"sneaky_extra": "x"}
    with pytest.raises(Exception):
        parse_intent(bad)


def test_seam_rejects_a_missing_field_no_fabricated_default():
    """Every field is REQUIRED; a producer that FORGETS one fails here rather than
    having it defaulted into existence (the outputs=[] scar). 'compute' is not
    silently 'local'; 'version' is not silently 'latest'."""
    missing_compute = {k: v for k, v in ROWS[0][1].items() if k != "compute"}
    with pytest.raises(Exception):
        parse_intent(missing_compute)


def test_seam_enforces_the_decline_reason_invariant():
    """A DECLINE owes an explanation; an in-scope kind must not carry one. Shape
    invariant, enforced at parse time (RequestIntent.model_post_init)."""
    # out_of_scope with no reason → rejected
    with pytest.raises(Exception):
        parse_intent(_intent(kind="out_of_scope", raw_prompt="x", out_of_scope_reason=None))
    # in-scope kind carrying a reason → rejected
    with pytest.raises(Exception):
        parse_intent(_intent(kind="install_env", raw_prompt="x",
                             tools=[_tool("samtools", "1.21")],
                             out_of_scope_reason="shouldn't be here"))


# ---------------------------------------------------------------------------
# 3. coverage — the corpus exercises the whole vocabulary; routing is total
# ---------------------------------------------------------------------------

def test_route_is_total_over_kind():
    """Every Kind maps to a rail. A Kind added without a rail must be a loud KeyError,
    not a silent hole — this proves the map is complete today."""
    for k in Kind:
        # build a minimal valid intent per kind just to exercise route()
        raw = _intent(kind=k.value, raw_prompt="probe",
                      out_of_scope_reason=("out of scope" if k is Kind.out_of_scope else None))
        assert isinstance(route(parse_intent(raw)), Rail)


def test_corpus_covers_every_concrete_kind_and_every_outcome():
    """The corpus must exercise every concrete rail-kind and every gate outcome, or a
    whole route could regress invisibly. (out_of_scope IS covered — s7.)"""
    kinds_seen = {parse_intent(r[1]).kind for r in ROWS}
    for k in Kind:
        assert k in kinds_seen, f"no corpus row exercises kind {k.value!r}"
    outcomes_seen = {r[2] for r in ROWS}
    for o in Outcome:
        assert o in outcomes_seen, f"no corpus row expects outcome {o.value!r}"


# ---------------------------------------------------------------------------
# 4. declared gaps — what the SINGLE-RAIL gate does NOT yet handle
# ---------------------------------------------------------------------------

def test_known_gaps_are_declared_not_discovered_later():
    """Two §7 scenarios are deliberately NOT in the passing corpus, and saying so is
    the point — a corpus silent about its blind spots reads as 'this is everything'.

    - Scenario 14 (mid-rail runtime failure) is NOT a gate outcome at all: repair is
      the RIDE's freedom (retry, add a dep, switch tier) inside its I/O contract. The
      gate routes the ENTRY; it does not model in-ride recovery. So there is no gate
      row for it, by design.
    - Scenario 15 (the worst case: install x,y,z as separate envs + build a pipeline +
      run on cluster) is COMPOSITION. One RequestIntent carries one kind; a multi-rail
      request is an ExecutionPlan that decomposes one goal into a DAG of intents (§6).
      That is Phase 4 (plan.py), not the single-rail gate. Asserting it here would be
      faking a capability that does not exist yet.

    This test is the executable form of that disclosure: if someone adds a composite
    'kind' or a mid-rail outcome to fake these, the design invariants below fail loudly.
    """
    # 14: there is no Outcome for in-ride repair — the gate's outcomes are entry-only.
    assert {o.value for o in Outcome} == {"decline", "ask", "investigate", "proceed"}, (
        "the gate grew an outcome beyond the four entry verdicts — scenario 14's repair "
        "belongs INSIDE a ride, not as a gate outcome")
    # 15: there is no composite kind — composition is an ExecutionPlan (Phase 4), not a Kind.
    assert "composite" not in {k.value for k in Kind}, (
        "a 'composite' kind was added; the worst case is an ExecutionPlan that decomposes "
        "into single-kind intents (§6/Phase 4), not a new Kind that fakes multi-rail intake")


# ---------------------------------------------------------------------------
# 5. the meter — state the number, don't bury it
# ---------------------------------------------------------------------------

def test_the_meter_is_visible():
    """Print how many §7 rows route correctly. Deterministic, so today this is N/N —
    but stating it means a future regression shows as a number, not silence."""
    passed = 0
    for rid, raw, want_outcome, want_rail, _ in ROWS:
        result = gate(parse_intent(raw))
        if result.outcome is want_outcome and result.rail is want_rail:
            passed += 1
    print(f"\nGATE CORPUS: {passed}/{len(ROWS)} §7 scenarios route to the intended "
          f"outcome + rail (scenarios 14 ride-level, 15 composite → declared gaps, Phase 4)")
    assert passed == len(ROWS)
