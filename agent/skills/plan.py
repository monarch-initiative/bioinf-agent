"""Composition — the itinerary through the park (docs/design_reverse_theme_park.md §6).

WHAT THIS IS. Most real requests are not one rail. `intent.py` types ONE user
request as ONE `RequestIntent` (single-rail by construction). This module types the
COMPOSITION of many: a multi-rail goal decomposed into a typed `ExecutionPlan` — an
ordered DAG whose nodes are rail/ride invocations, the output of an earlier node
feeding the input of a later one. The LLM authors the itinerary (free inside the PLAN
ride — first-principles reasoning about what has to happen); once authored it is a
validated, on-rails structure whose gate is Layer-2's I8 lifted to authoring time.

    goal + steps  ──[ LLM: free decomposition ]──▶  ExecutionPlan (typed, validated)
                                                            │
                                          gate_plan() ── deterministic ──▶ PlanGateResult

THE SPLIT, UNCHANGED. Code owns what must be trustworthy — the SHAPE of the plan and
the GATE over it (is it a real DAG? does every input trace to a source? does it
terminate at the goal?). The LLM owns the open-ended — the decomposition itself, and
walking each node by calling the existing single-purpose primitives. This module
DISPATCHES NOTHING (Phase 4 is advisory, like `interpret_request`): "rails are
deterministic ordering + state + gates, not an auto-pilot; the agent WALKS the rides."
Auto-dispatch is a later phase; building an `execute_plan` that loops the DAG calling
freeze/run/seal internally would be the forbidden composite primitive AND would bury
the per-seam honesty gates inside one opaque call.

THREE DISCIPLINES CARRIED FROM intent.py / core_data.ShippedBinary, each bought with a
real defect in this repo:

  1. NO SECOND TRUTH. A plan node's `action` (which Rail), `produces` (which artifact),
     and `depends_on` are all DERIVED, never stored — a node cannot carry two copies of
     one fact that drift. `Rail` is imported from intent.py by reference; the ONLY new
     vocabulary is `PlanRide` = {SEAL} (the terminal ride that is a plan node but not an
     entry rail — it produces a `workflow_spec`, which no rail does). The external-source
     set and the I8 sentence are imported from spec_writer, so plan-time and seal-time
     read one truth. Authoring the pipeline is NOT a node: you author by RUNNING-and-
     validating steps (the free inside of the RUN ride), and the ExecutionPlan itself IS
     the authored pipeline — an "author" node inside it would be self-referential.

  2. NO FABRICATED DEFAULTS (`extra="forbid"`, every field required). "The author
     consumes nothing" is a STATED `consumes=[]` on a source node, never a default; a
     consume states EXACTLY ONE of from_step / external, never neither.

  3. SAME LAW, LIFTED — NOT RE-IMPLEMENTED. `gate_plan`'s composition check IS
     spec_writer's I8 (`_check_composition_coherence`) at a higher altitude: the runtime
     walks CONCRETE absolute paths at seal (authoritative, sole authority); the plan
     walks SYMBOLIC edges + external CLASSES at authoring. They share no code because the
     runtime body is path normalization that has zero meaning before any path exists.
     The plan gate is NECESSARY-NOT-SUFFICIENT: it catches a structurally-dangling
     itinerary before anything runs, but it cannot see an UNDECLARED input (a command
     that secretly reads an un-listed file passes here, caught only by concrete-path seal
     I8). A GREEN plan is a fast-fail preview, never an authorization — seal re-checks on
     real evidence and remains the authority. IDs live in one family: plan emits
     `I8_authoring.*`, seal emits `I8.composition_coherence`.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from agent.skills.intent import (
    Outcome,
    Rail,
    RequestIntent,
    gate as intent_gate,
    route,
)
from agent.skills.spec_writer import EXTERNAL_SOURCE_KINDS, I8_STATEMENT


# ---------------------------------------------------------------------------
# Controlled vocabulary — the 1-element delta over intent.Rail
# ---------------------------------------------------------------------------

class PlanRide(str, Enum):
    """A composition-only step that is a plan NODE but not an entry RAIL.

    The genuine delta composition needs is exactly ONE: SEAL. Every user request
    routes to one of intent.Rail's seven; none routes to "seal" — sealing is the
    terminal ride (§5) that turns a validated run into a `WorkflowSpec`, and it
    produces a `workflow_spec`, the one artifact no rail emits (INSTALL_ENV → an env,
    RUN_STEP → a file set). A plan that terminates at a sealed pipeline NEEDS a node
    that produces that, so SEAL is a node. It is disjoint from Rail by construction
    (asserted in a test), so a grep for any rail name still lands on intent.Rail alone.

    AUTHOR_PIPELINE is deliberately NOT here (the design's §6 prose named it, but our
    system has no author primitive — you author by running-and-validating steps, and
    the ExecutionPlan already IS the authored pipeline). Folding it in keeps the delta
    at one element.
    """
    SEAL = "SEAL"


class ProducesKind(str, Enum):
    """What KIND of artifact a node yields — used only to check the plan terminates at
    the goal. Aligned with the lifecycle (`pipeline_state`): env_digest ↔ ENV_FROZEN,
    workflow_spec ↔ SEALED. This is a coarse artifact-class, NOT a typed handle: no
    node carries a concrete digest/path at authoring time (that would be a pre-declared
    schema for the unbuilt auto-executor)."""
    env_digest    = "env_digest"      # a frozen env (INSTALL_ENV / ADD_TO_ENV / REPRODUCE)
    file_set      = "file_set"        # validated output files (RUN_STEP)
    transfer      = "transfer"        # bytes moved to/from a destination (TRANSFER_DATA)
    workflow_spec = "workflow_spec"   # a sealed WorkflowSpec (SEAL)


# The ONLY place stating what each dispatchable action yields — a new single truth
# (nothing else in the codebase states this). Total over the five buildable rails +
# the one PlanRide. Rail.DECLINE / Rail.CLARIFY are deliberately ABSENT: they are the
# gate's non-buildable routes, and a PlanStep whose intent routes to either is rejected
# at parse, so they can never be a node's action.
_PRODUCES_FOR_ACTION: dict = {
    Rail.INSTALL_ENV:   ProducesKind.env_digest,
    Rail.ADD_TO_ENV:    ProducesKind.env_digest,
    Rail.REPRODUCE:     ProducesKind.env_digest,
    Rail.RUN_STEP:      ProducesKind.file_set,
    Rail.TRANSFER_DATA: ProducesKind.transfer,
    PlanRide.SEAL:      ProducesKind.workflow_spec,
}

# Actions that legitimately consume NOTHING from the plan — a source node with an
# empty `consumes` is honest, not an orphan. INSTALL_ENV starts from a tool name;
# REPRODUCE rebuilds from a recipe reference; TRANSFER_DATA moves external user files.
# Everything else (ADD_TO_ENV needs an env, RUN_STEP needs inputs, SEAL needs a run)
# must trace its input to a prior node's produces, a declared external source, or an
# existing frozen env (intent.target_env).
_SOURCE_ACTIONS = frozenset({Rail.INSTALL_ENV, Rail.REPRODUCE, Rail.TRANSFER_DATA})


# ---------------------------------------------------------------------------
# Sub-records
# ---------------------------------------------------------------------------

class ExternalSource(BaseModel):
    """A plan input that comes from OUTSIDE the plan — the authoring-time analog of
    Layer-2 I8's external universe (test_data / reference_databases / runtime_configs /
    authored_artifacts). `kind` is validated in the GATE against
    `spec_writer.EXTERNAL_SOURCE_KINDS` (not a parse-time Literal) so the one set lives
    in spec_writer and a 5th category flows to plan-time automatically. `ref` is a
    SYMBOLIC handle, never a resolved path — no concrete paths exist at authoring time
    ('core:chr22_actb', 'project:/proj/xyz/reads', 'refdb:gnomad-4.0'). Cluster project
    data is expressed here through the existing kinds; the project directory + compute
    resource are authorized via projects_access.yaml and resolved at walk time."""
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(min_length=1)
    ref: str = Field(min_length=1)


class Consumes(BaseModel):
    """One input a step consumes. Mirrors runtime I8's disjunction VERBATIM: a prior
    step's output (`from_step`, an intra-plan edge) OR an external source. EXACTLY ONE
    is stated — never both, never neither (the outputs=[] discipline: an unstated
    consume is a shape error, not a silent default)."""
    model_config = ConfigDict(extra="forbid")

    from_step: Optional[str]
    external: Optional[ExternalSource]

    def model_post_init(self, __context) -> None:  # noqa: D401
        stated = [x for x in (self.from_step, self.external) if x is not None]
        if len(stated) != 1:
            raise ValueError(
                "a consume states EXACTLY ONE of from_step (an intra-plan edge) or "
                "external (a declared source) — never both, never neither")


class Goal(BaseModel):
    """The end the user asked for. `statement` is their words (verbatim, the ground
    truth an audit reads); `produces` is what KIND of artifact reaching the goal MEANS
    — STATED, never defaulted. The gate confirms some sink node produces it."""
    model_config = ConfigDict(extra="forbid")

    statement: str = Field(min_length=1)
    produces: ProducesKind


# ---------------------------------------------------------------------------
# The plan node
# ---------------------------------------------------------------------------

class PlanStep(BaseModel):
    """One node of the itinerary. A RAIL node carries an `intent` (a full RequestIntent
    — the sub-goal this node satisfies); a SEAM node carries a `seam` (a PlanRide).
    EXACTLY ONE is stated. `action`, `produces`, and `depends_on` are all DERIVED
    properties, never stored fields — a node cannot carry two truths that drift, and an
    author CANNOT lie about what a node emits (sending a `produces` key is an
    extra="forbid" error)."""
    model_config = ConfigDict(extra="forbid")

    id: str = Field(min_length=1)
    intent: Optional[RequestIntent]
    seam: Optional[PlanRide]
    consumes: list[Consumes]

    def model_post_init(self, __context) -> None:  # noqa: D401
        stated = [x for x in (self.intent, self.seam) if x is not None]
        if len(stated) != 1:
            raise ValueError(
                f"PlanStep {self.id!r} states EXACTLY ONE of intent (a rail node) or "
                "seam (a composition ride) — never both, never neither")
        if self.intent is not None:
            r = route(self.intent)
            if r in (Rail.DECLINE, Rail.CLARIFY):
                raise ValueError(
                    f"PlanStep {self.id!r} routes to {r.value} — a gate route, not a "
                    "buildable action; a plan is composed only of PROCEED-able rails")

    @property
    def action(self) -> "Rail | PlanRide":
        """The single truth of what this node IS: the rail its intent routes to, or its
        seam. Rail-node actions are COMPUTED via intent.route() — structurally incapable
        of drifting against Rail."""
        return route(self.intent) if self.intent is not None else self.seam

    @property
    def produces(self) -> ProducesKind:
        """Derived from action — an author cannot state it. A missing action→produces
        mapping is a loud KeyError (guarded by a totality test), never a silent hole."""
        return _PRODUCES_FOR_ACTION[self.action]

    @property
    def depends_on(self) -> list[str]:
        """Derived from `consumes` — the intra-plan edges. Storing this alongside
        `consumes` would be the exact two-truths disease."""
        return [c.from_step for c in self.consumes if c.from_step is not None]


class ExecutionPlan(BaseModel):
    """The typed itinerary — a goal + a DAG of steps. Single-rail by nothing: a 1-node
    plan is as legal as a 6-node one (a 6-step plan is exactly as trustworthy as a
    1-step one — that is the payoff of gating every seam)."""
    model_config = ConfigDict(extra="forbid")

    goal: Goal
    steps: list[PlanStep]


# ---------------------------------------------------------------------------
# The gate's verdict
# ---------------------------------------------------------------------------

class PlanViolation(BaseModel):
    """One authoring-time failure. `invariant` names the clause (I8_authoring.* for the
    lifted composition law; PLAN.* for structural: cycle / duplicate_id / sub_intent).
    `node_id` is the offending step, or None for a whole-plan property (cycle / goal)."""
    model_config = ConfigDict(extra="forbid")

    invariant: str
    message: str
    node_id: Optional[str]


class PlanGateResult(BaseModel):
    """The gate's answer. `plan_ready` is the single verdict; `topo_order` is the walk
    order (empty on a cycle); `ready_frontier` is where to START (nodes with no unmet
    in-plan deps — the parallelizable sources); `pending_investigation` are rail nodes
    whose sub-intent has a FINDABLE gap the ride fills while walking (non-blocking);
    `goal_reached` is whether some sink produces the goal artifact."""
    model_config = ConfigDict(extra="forbid")

    plan_ready: bool
    violations: list[PlanViolation]
    topo_order: list[str]
    ready_frontier: list[str]
    pending_investigation: list[dict]
    goal_reached: bool


# ---------------------------------------------------------------------------
# The seam + the gate
# ---------------------------------------------------------------------------

def parse_plan(raw: dict) -> ExecutionPlan:
    """THE reader for a raw plan dict — the input-side analog of `parse_intent`.

    Every path that consumes a plan goes through here, so a malformed itinerary
    (bad shape, a step with both intent and seam, a consume with neither leg, a
    DECLINE/CLARIFY rail node, a `produces` key an author tried to fake) fails at the
    seam with a loud `pydantic.ValidationError`, not as a `None` deep in the gate.
    """
    return ExecutionPlan.model_validate(raw)


def _topo_order(steps: list[PlanStep]) -> tuple[list[str], list[str]]:
    """Kahn topological sort over the derived depends_on edges. Returns
    (order, cycle_nodes): a non-empty cycle_nodes means the plan is not a DAG and
    `order` is the partial prefix that could be sequenced. Input order is preserved
    among ready nodes so the walk is deterministic."""
    ids = [s.id for s in steps]
    id_set = set(ids)
    deps = {s.id: {d for d in s.depends_on if d in id_set} for s in steps}
    indeg = {i: len(deps[i]) for i in ids}
    consumers: dict[str, list[str]] = {i: [] for i in ids}
    for i in ids:
        for d in deps[i]:
            consumers[d].append(i)

    ready = [i for i in ids if indeg[i] == 0]
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for c in consumers[n]:
            indeg[c] -= 1
            if indeg[c] == 0:
                ready.append(c)

    if len(order) == len(ids):
        return order, []
    emitted = set(order)
    return order, [i for i in ids if i not in emitted]


def gate_plan(plan: ExecutionPlan) -> PlanGateResult:
    """The completeness gate over a typed plan — Layer-2's I8 lifted to authoring time.

    The checks, in order (all deterministic over the typed plan; the LLM's judgment is
    already baked into the RequestIntents and the DAG shape — the gate only ROUTES):

      1. unique ids                — no two steps share an id (PLAN.duplicate_id).
      2. DAG integrity             — every from_step names an existing step
                                     (I8_authoring.dangling_dependency).
      3. cycle-free                — Kahn topo-sort; a residual cycle is not a DAG
                                     (PLAN.cycle). topo_order returned (empty on cycle).
      4. external kinds recognized — every external.kind ∈ EXTERNAL_SOURCE_KINDS
                                     (I8_authoring.external_unknown_kind).
      5. orphan input              — a non-source node with empty consumes AND no
                                     target_env consumes nothing (I8_authoring.orphan_input,
                                     the lifted silent-empty trap).
      6. sub-intent completeness   — every rail node's sub-intent, via intent.gate(),
                                     must not need the user: ASK blocks
                                     (PLAN.sub_intent_blocked); INVESTIGATE rides along
                                     in pending_investigation; PROCEED is clean.
      7. terminates at goal        — some SINK node produces the goal artifact
                                     (I8_authoring.goal_unreached).

    Input-satisfaction (every input traces to a prior produce or an external source) is
    established by checks 2+4+5 together and evidenced by topo_order — there is no
    separate walk to write. The gate is NECESSARY-NOT-SUFFICIENT: seal re-checks on
    concrete paths and stays the authority.
    """
    steps = plan.steps
    ids = [s.id for s in steps]
    id_set = set(ids)
    by_id = {s.id: s for s in steps}
    violations: list[PlanViolation] = []

    # 1. unique ids
    seen: set[str] = set()
    for i in ids:
        if i in seen:
            violations.append(PlanViolation(
                invariant="PLAN.duplicate_id",
                message=f"step id {i!r} is used by more than one PlanStep; ids must be unique",
                node_id=i))
        seen.add(i)

    # 2. DAG integrity — every from_step resolves to a real step
    for s in steps:
        for c in s.consumes:
            if c.from_step is not None and c.from_step not in id_set:
                violations.append(PlanViolation(
                    invariant="I8_authoring.dangling_dependency",
                    message=f"step {s.id!r} consumes from_step {c.from_step!r}, which is not a "
                            f"step in this plan; known ids are {sorted(id_set)}",
                    node_id=s.id))

    # 3. cycle-free
    topo, cycle_nodes = _topo_order(steps)
    if cycle_nodes:
        violations.append(PlanViolation(
            invariant="PLAN.cycle",
            message=f"the plan has a dependency cycle among {sorted(cycle_nodes)}; a plan is a DAG",
            node_id=None))

    # 4. external kinds recognized (the single-sourced spec_writer vocabulary)
    for s in steps:
        for c in s.consumes:
            if c.external is not None and c.external.kind not in EXTERNAL_SOURCE_KINDS:
                violations.append(PlanViolation(
                    invariant="I8_authoring.external_unknown_kind",
                    message=f"step {s.id!r} consumes external kind {c.external.kind!r}, which is not "
                            f"a recognized external source; {I8_STATEMENT}; recognized kinds are "
                            f"{', '.join(sorted(EXTERNAL_SOURCE_KINDS))}",
                    node_id=s.id))

    # 5. orphan input — a non-source node that traces to nothing
    for s in steps:
        if s.action in _SOURCE_ACTIONS:
            continue
        if s.consumes:
            continue
        if s.intent is not None and (s.intent.target_env or "").strip():
            continue  # binds an existing frozen env — a valid input source, not an orphan
        violations.append(PlanViolation(
            invariant="I8_authoring.orphan_input",
            message=f"step {s.id!r} ({s.action.value}) consumes nothing and binds no target_env — "
                    f"{I8_STATEMENT}; a non-source node's input must trace to a prior node's "
                    f"produces or a declared external source",
            node_id=s.id))

    # 6. sub-intent completeness — reuse intent.gate() as the one proceed-ability truth
    pending: list[dict] = []
    for s in steps:
        if s.intent is None:
            continue
        gr = intent_gate(s.intent)
        if gr.outcome is Outcome.investigate:
            pending.append({"node_id": s.id, "unknowns": [u.model_dump() for u in gr.blocking]})
        elif gr.outcome is not Outcome.proceed:
            violations.append(PlanViolation(
                invariant="PLAN.sub_intent_blocked",
                message=f"step {s.id!r} sub-intent gates to {gr.outcome.value} (not buildable "
                        f"without the user); blocking gaps: {[u.field for u in gr.blocking]}",
                node_id=s.id))

    # 7. terminates at goal — a sink produces the goal artifact
    depended_on = {d for s in steps for d in s.depends_on}
    sinks = [s for s in steps if s.id not in depended_on]
    goal_reached = any(s.produces == plan.goal.produces for s in sinks)
    if not goal_reached:
        sink_kinds = sorted({s.produces.value for s in sinks})
        violations.append(PlanViolation(
            invariant="I8_authoring.goal_unreached",
            message=f"no sink node produces the goal artifact {plan.goal.produces.value!r}; sink "
                    f"nodes produce {sink_kinds or ['(none)']} — the itinerary does not terminate "
                    f"at the requested outcome",
            node_id=None))

    # where to start: nodes with no unmet in-plan dependency (the parallelizable frontier)
    frontier = [s.id for s in steps if not [d for d in s.depends_on if d in id_set]]

    plan_ready = (not violations) and goal_reached
    return PlanGateResult(
        plan_ready=plan_ready,
        violations=violations,
        topo_order=topo,
        ready_frontier=frontier,
        pending_investigation=pending,
        goal_reached=goal_reached,
    )
