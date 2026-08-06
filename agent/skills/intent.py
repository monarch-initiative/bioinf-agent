"""The front gate — typed intake + the completeness gate.

WHAT THIS IS. The one entrance to the park (docs/design_reverse_theme_park.md §2-3).
A raw prompt is turned into a VALIDATED `RequestIntent` — the LLM's reading of what
the user wants, as a typed record — and then the completeness gate routes it to exactly
one of four outcomes: DECLINE / ASK / INVESTIGATE / PROCEED, plus the rail it belongs to.

THE SPLIT THIS TURNS ON. Code owns what must be trustworthy — the SHAPE of the intake
and the ROUTING over it. The LLM owns the open-ended — the INTERPRETATION (which tool,
what version, what is missing, whether a gap is findable). Today the system makes both
mistakes at once: the LLM improvises the intake with no model, and the resolver's 74-word
`_DOMAIN_TERMS` list does identity judgment it is bad at. This module fixes the first
half (types the intake); Phase 2 fixes the second (deletes the word-list).

    raw prompt  ──[ LLM: free interpretation ]──▶  RequestIntent (typed, validated)
                                                          │
                                            gate() ── deterministic ──▶ Outcome + Rail

BEHAVIOUR-NEUTRAL (Phase 1). Nothing in the running system is forced through this yet;
`interpret_request` is advisory, exactly like `resolve_tool`. The value delivered now is
that the intake becomes a thing we can VALIDATE, LOG, TEST against a corpus, and ROUTE on
— instead of an interpretation re-improvised on every request. The hard gates stay where
they always were: at the EXIT (the honesty contract).

THE MODELS ARE A SEAM, NOT DOCUMENTATION. `RequestIntent` mirrors `core_data.ShippedBinary`
— `extra="forbid"` + NO fabricating defaults — for the same reason that model earned it:
a permissive model with defaults does not CATCH missing intent, it AUTHORS a fake one (the
`WorkflowSpec.outputs=[]` scar). "The user did not say" is a FACT the producer must state
(`version=None`, a gap in `unknowns[]`), never a value invented by a default (`"latest"`).
Read an intent in through `parse_intent()` — the input-side analog of
`core_data.shipped_binaries()` — so a malformed intake fails at the seam, loudly, rather
than routing a fabricated interpretation.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------------
# Controlled vocabulary
# ---------------------------------------------------------------------------

class Kind(str, Enum):
    """The rail selector — the single fact that picks a top-level route (§4).

    This is the COMPLETE surface. A request that fits none of the concrete kinds is
    `out_of_scope` (→ DECLINE); a request whose intent is underspecified enough that
    we cannot even pick a kind is `ambiguous` (→ ASK). There is no eighth kind: if a
    real request cannot be expressed here, that is a design finding, not a reason to
    reach for `extra`.
    """
    install_env   = "install_env"     # build a new env with 1..N tools, validate, freeze
    add_to_env    = "add_to_env"      # add a tool to an existing draft / re-freeze
    run_step      = "run_step"        # run one step against an existing frozen env
    transfer_data = "transfer_data"   # move bytes, no install
    reproduce     = "reproduce"       # rebuild from a recipe, digest-check
    out_of_scope  = "out_of_scope"    # not ours to serve
    ambiguous     = "ambiguous"       # intent itself underspecified


class Outcome(str, Enum):
    """The completeness gate's verdict (§3). [[feedback-earn-the-refusal]] made
    structural: the ASK/INVESTIGATE split is the whole point — never ask for what you
    can find yourself; always ask when the intent itself is underspecified."""
    decline     = "decline"       # out of scope; explain and stop
    ask         = "ask"           # underspecified OR a required detail is un-findable
    investigate = "investigate"   # intent clear, a mechanical detail missing AND findable
    proceed     = "proceed"       # every required field known


class Rail(str, Enum):
    """The seven top-level routes (§4). One per `Kind`, total."""
    INSTALL_ENV   = "INSTALL_ENV"
    ADD_TO_ENV    = "ADD_TO_ENV"
    RUN_STEP      = "RUN_STEP"
    TRANSFER_DATA = "TRANSFER_DATA"
    REPRODUCE     = "REPRODUCE"
    DECLINE       = "DECLINE"       # the gate's out-of-scope route
    CLARIFY       = "CLARIFY"       # the gate's ambiguous route


class Compute(str, Enum):
    """Where the user wants the work to run. `unspecified` is a STATED value — the
    user did not say — never silently defaulted to `local`."""
    local       = "local"
    cluster     = "cluster"
    unspecified = "unspecified"


# ---------------------------------------------------------------------------
# Sub-records
# ---------------------------------------------------------------------------

class SourceHint(BaseModel):
    """A concrete source the USER supplied — a repo, a release URL, a channel, an
    image, a Dockerfile path. Verbatim. RESOLVE consumes it to skip discovery
    (scenario 1: "install talos from github.com/X/talos" pins the repo)."""
    model_config = ConfigDict(extra="forbid")

    kind: Literal["github_repo", "release_url", "channel", "image", "recipe"]
    value: str = Field(min_length=1)


class Resolution(BaseModel):
    """WHAT THE RIDE BELIEVES THE USER MEANT, as a claim to be CHECKED — not a fact.

    This is the slot the ride's world knowledge lives in, and its absence was a real
    architectural hole. `resolve('gatk')` returned `gatk=3.8` — a 2017 release — because
    this field versions tools by RENAMING the package and GATK4 ships as `gatk4`. An LLM
    knows that instantly. The system did not, and could not: the intake recorded the user's
    raw word and forbade normalizing it, then handed that word to a mechanical resolver, so
    there was nowhere for the one participant who knew to say so. The user's question was
    exactly right — "why are you so easily able to tell that gatk4 is the latest, but the
    system shits itself when it tries to do it itself?" Because nothing ever asked.

    The rule that forbade it is still right and is UNCHANGED: `ToolRequest.name` stays the
    user's verbatim word. This is a SECOND field, so the two are never confused — the record
    carries "the user said `gatk`, we installed `gatk4`, because …", which is precisely what
    a human reviewing an ENV report needs in order to disagree with us.

    AND IT IS VERIFIED, NEVER TRUSTED. `resolve_tool(tool=…, user_said=…)` checks that the
    claimed package actually exists on a channel and belongs to the same name-family as the
    user's word, and REFUSES when it does not. That keeps this from being a licence to
    hallucinate a package name: it is the same posture the honesty contract takes to every
    other agent-authored field, applied to the one that carries the most leverage.

    `None` on ToolRequest.resolution is a STATED "I have no prior knowledge of this tool" —
    it routes to INVESTIGATE, not to a silent install of the raw name."""
    model_config = ConfigDict(extra="forbid")

    package: str = Field(min_length=1)
    """The canonical package/repo name the ride believes is meant — 'gatk4' for a request
    that said 'gatk', 'Seurat' for 'seurat'. May equal the user's word; usually does."""

    version: Optional[str]
    """The specific version the ride believes is meant, or `None` for "the latest of that
    package". `None` here is NOT the same as `ToolRequest.version is None`: that one means
    the user did not say, this one means the ride deliberately wants latest."""

    why: str = Field(min_length=1)
    """The REASONING, in a sentence, and it is required. It goes into the ENV report so a
    human can disagree with it — 'GATK4 is the current major line; bioconda ships it as
    `gatk4`, while the bare `gatk` package is GATK3 (3.8, 2017)'. A claim with no stated
    basis is indistinguishable from a guess, and this project does not ship those."""


class ToolRequest(BaseModel):
    """One tool the user asked for, VERBATIM. The producer (the LLM's reading of the
    prompt) fills every field; `None` is a value it must STATE, never a default —
    the same discipline `ShippedBinary` enforces one layer down.
    """
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    """The tool as the USER named it — 'cellranger', 'talos'. NEVER normalized to a
    package id here: identity is RESOLVE's job and the ride's judgment, not the
    intake's. Recording the raw name is what lets the ride later catch "green terminal,
    wrong tool" (scenario 9: 'cellranger' the 10x pipeline vs the CRAN parser).

    The ride's normalization lives in `resolution`, a SEPARATE field, so this one keeps
    meaning exactly what it always meant and the two can be shown side by side."""

    version: Optional[str]
    """Verbatim and UNRESOLVED. `None` means the user did not specify — it is NOT
    silently 'latest'. Filling it is INVESTIGATE's job (find latest; resolve a
    near-miss like 0.7 → 0.7.17), and the ride RECORDS how. A default of 'latest'
    here would be the `outputs=[]` scar: authoring an intent the user never stated."""

    source_hint: Optional[SourceHint]
    """A repo / release URL / channel the user gave, or None. Pins RESOLVE past
    discovery when present."""

    purpose: Optional[str]
    """Domain context — 'for scRNA cell calling'. The disambiguator that separates a
    name collision (scenario 9). None when the user gave no context — stated, not
    guessed."""

    resolution: Optional[Resolution] = None
    """The ride's judgment about which package this request actually means, with its
    reasoning — see `Resolution`. `None` is a STATED "I have no prior knowledge of this
    tool", and routes to INVESTIGATE so the family and self-descriptions come back before
    anything installs. Nothing is ever installed on a belief nobody wrote down.

    Defaulted rather than required ONLY because every RequestIntent authored before this
    field existed would otherwise fail to parse; the gate treats a missing one exactly as a
    stated `None`, so absence buys no silent pass."""


class DataOp(BaseModel):
    """One byte-movement the user asked for (the TRANSFER_DATA rail, scenario 6).
    No install is implied — moving FASTQs to the cluster is not an env build."""
    model_config = ConfigDict(extra="forbid")

    direction: Literal["upload", "download"]
    source: Optional[str]   # verbatim path/description, None if the user did not say
    dest: Optional[str]     # verbatim destination, None if the user did not say


class Unknown(BaseModel):
    """A gap the LLM could NOT fill from the prompt. THE gate's input.

    Every gap is stated here EXPLICITLY — this is the `outputs=[]` lesson applied to
    the intake: a missing field is a fact to write down, never a default to invent.
    The gate never guesses what is missing; it reads this list.
    """
    model_config = ConfigDict(extra="forbid")

    field: str = Field(min_length=1)
    """What is missing, dotted for precision: 'talos.version', 'target_env',
    'tool_name', 'data_op.dest'."""

    findable: bool
    """Can INVESTIGATION fill it — a registry probe, discovery, a near-miss resolve?
    This ONE bool is the ASK/INVESTIGATE discriminator: code routes on it, the LLM
    decides it. True → a mechanical detail we can look up ourselves (→ INVESTIGATE).
    False → the intent itself is underspecified and only the user can resolve it
    (→ ASK). Scenario 4's transposed versions are `findable=False`: judgment over two
    fact-sets saw the swap, and we must not silently 'correct' it."""

    reason: str = Field(min_length=1)
    """Why it is unknown, and why (not) findable — the audit trail for the routing
    decision, so an ASK or an INVESTIGATE is always explainable after the fact."""


# ---------------------------------------------------------------------------
# The intent record
# ---------------------------------------------------------------------------

class RequestIntent(BaseModel):
    """The LLM's reading of ONE user request, as a VALIDATED record — the first typed
    thing at the front door (§2).

    `extra="forbid"` + no fabricated defaults, mirroring `ShippedBinary`: the intake
    stops being an interpretation the agent improvises fresh every time and becomes a
    thing we validate, log, test against a corpus, and route on. That is the entire
    reason the front door stops being brittle.

    SINGLE-RAIL BY CONSTRUCTION (Phase 1). One `RequestIntent` carries one `kind`. A
    request that spans rails ("install x, y, z as separate envs AND run a pipeline",
    scenario 15) is COMPOSITION — an `ExecutionPlan` that decomposes one goal into a
    DAG of intents (§6, Phase 4). It is a declared, tested gap here (see
    tests/test_intent_gate.py::known_gaps), not something faked into a single record.
    """
    model_config = ConfigDict(extra="forbid")

    kind: Kind
    """The rail selector. Drives both routing and the gate."""

    raw_prompt: str = Field(min_length=1)
    """The user's words, VERBATIM. Always kept — it is the ground truth the typed
    interpretation is answerable to, and what a human reads to audit a mis-route."""

    tools: list[ToolRequest]
    """Tools for install / add. `[]` for transfer_data / reproduce / out_of_scope —
    a STATED empty list (there are genuinely no tools), which is different from the
    `outputs=[]` scar: there the empty list masqueraded as captured data; here empty
    IS the fact for a transfer request."""

    target_env: Optional[str]
    """An existing env this request acts on — for add_to_env / run_step / reproduce.
    `None` when the request builds a new env or moves data. A request-key or env name
    as the user referred to it; resolving it to a digest is the ride's job."""

    compute: Compute
    """local / cluster / unspecified — stated, never defaulted."""

    data_ops: Optional[list[DataOp]]
    """Byte-movements for the transfer_data rail. `None` for every other kind (no
    transfer implied), NOT `[]` — absence of a data plane vs an empty one are
    different facts."""

    unknowns: list[Unknown]
    """Every gap the LLM could not fill. THE gate's input. `[]` means the LLM claims
    the request is complete for its kind — which the gate takes at face value and
    routes to PROCEED. That claim is the LLM's judgment, recorded and auditable."""

    out_of_scope_reason: Optional[str]
    """Prose: why this is not ours to serve. REQUIRED to be non-None when
    `kind == out_of_scope` (enforced below) — a DECLINE with no reason is a shrug, and
    the user is owed an explanation of the scope boundary. `None` for every in-scope
    kind."""

    def model_post_init(self, __context) -> None:  # noqa: D401
        # A DECLINE must carry its reason; an in-scope kind must not invent one. This
        # is a shape invariant, not judgment — so it belongs on the record, enforced
        # at parse time, the way ShippedBinary enforces tool != "".
        if self.kind is Kind.out_of_scope and not (self.out_of_scope_reason or "").strip():
            raise ValueError(
                "kind=out_of_scope requires a non-empty out_of_scope_reason — a "
                "DECLINE owes the user an explanation of the scope boundary")
        if self.kind is not Kind.out_of_scope and self.out_of_scope_reason:
            raise ValueError(
                f"out_of_scope_reason is set on an in-scope kind ({self.kind.value}); "
                "it belongs only on out_of_scope")


class GateResult(BaseModel):
    """The gate's verdict — one outcome, one rail, and the gaps that drove it.

    `blocking` is the subset of `unknowns` responsible for an ASK or an INVESTIGATE
    (so the caller can render exactly what to ask or look up). Empty for PROCEED and
    DECLINE.
    """
    model_config = ConfigDict(extra="forbid")

    outcome: Outcome
    rail: Rail
    blocking: list[Unknown]


# ---------------------------------------------------------------------------
# The seam + the routing
# ---------------------------------------------------------------------------

def parse_intent(raw: dict) -> RequestIntent:
    """THE reader for a raw intent dict — the input-side analog of
    `core_data.shipped_binaries()`.

    Every path that consumes an intent goes through here, so an intake that does not
    conform fails at the seam (`pydantic.ValidationError`) rather than as a `None`
    deep in the gate. `extra="forbid"` turns a producer that invents a key into a
    loud parse-time error; the no-default fields turn a producer that FORGETS a field
    into the same. A fabricated interpretation never reaches the router.
    """
    return RequestIntent.model_validate(raw)


_RAIL_FOR_KIND: dict[Kind, Rail] = {
    Kind.install_env:   Rail.INSTALL_ENV,
    Kind.add_to_env:    Rail.ADD_TO_ENV,
    Kind.run_step:      Rail.RUN_STEP,
    Kind.transfer_data: Rail.TRANSFER_DATA,
    Kind.reproduce:     Rail.REPRODUCE,
    Kind.out_of_scope:  Rail.DECLINE,
    Kind.ambiguous:     Rail.CLARIFY,
}


def route(intent: RequestIntent) -> Rail:
    """`kind → rail`. Total over `Kind`, deterministic, no judgment. A `KeyError`
    here can only mean a `Kind` was added without a rail — which we WANT to be loud,
    so the map is not defensive."""
    return _RAIL_FOR_KIND[intent.kind]


def gate(intent: RequestIntent) -> GateResult:
    """The completeness gate — DECLINE / ASK / INVESTIGATE / PROCEED (§3).

    DETERMINISTIC over the typed intent. All judgment — what is unknown, whether a
    gap is findable — is the LLM's and is already baked into the `RequestIntent`; the
    gate only ROUTES on it. That is the design's load-bearing split: the LLM decides,
    the code dispatches, and the dispatch is simple enough to read in one sitting.

    The logic, in full:
      - kind == out_of_scope           → DECLINE  (the prompt is not ours to serve)
      - kind == ambiguous              → ASK      (no investigation can invent which
                                                    tool the user meant)
      - a concrete rail-kind:
          · any un-findable unknown    → ASK      (can't proceed, can't look it up —
                                                    the intent is underspecified)
          · else any unknown at all    → INVESTIGATE (findable; the ride fills it and
                                                    records how)
          · any tool with no stated
            `resolution`               → INVESTIGATE (the ride has not said WHICH package
                                                    this request means — look, then re-state)
          · no unknowns                → PROCEED   (the LLM claims completeness)
    """
    rail = route(intent)

    if intent.kind is Kind.out_of_scope:
        return GateResult(outcome=Outcome.decline, rail=rail, blocking=[])

    if intent.kind is Kind.ambiguous:
        # Everything the LLM flagged is what the clarifying question is about. If it
        # flagged nothing, the ambiguity itself is the block — surface an empty list
        # honestly rather than inventing one.
        return GateResult(outcome=Outcome.ask, rail=rail, blocking=list(intent.unknowns))

    unfindable = [u for u in intent.unknowns if not u.findable]
    if unfindable:
        return GateResult(outcome=Outcome.ask, rail=rail, blocking=unfindable)

    # A TOOL WITH NO STATED `resolution` IS AN UNKNOWN, whether or not the ride listed it.
    #
    # This is the gate half of the hole `Resolution` fills. Before it, the intake recorded
    # the user's raw word, forbade normalizing it, and PROCEEDED — so `gatk` went to the
    # resolver as `gatk` and came back as GATK3 from 2017, with every mechanical check
    # green. The ride knew better and was never asked.
    #
    # Synthesised here rather than trusted from `unknowns[]` on purpose: this is the ONE
    # gap the code can see for itself (the field is either filled or it is not), so making
    # the ride declare it would just be another chance to forget. Everything else in this
    # function still routes purely on the ride's own declarations.
    # ...EXCEPT when the user pinned the source themselves. "install talos from
    # github.com/populationgenomics/talos" leaves nothing for the ride to normalize: the
    # user named the artifact, and this codebase already treats a user-supplied repo as
    # authoritative intent everywhere else (the cross-namespace collision guard, the
    # fork-anchor reconciliation). Demanding a resolution there would be asking the ride to
    # restate a decision the user already made, which is the "always ask taxes the 70%"
    # failure in a new place.
    unstated = [Unknown(field=f"{t.name}.resolution", findable=True,
                        reason=("no canonical package stated for this request. Say WHICH "
                                "package it means and WHY — this field renames packages "
                                "across major versions (a request for 'gatk' resolves to "
                                "GATK3), so the user's word is not reliably the package. "
                                "If you do not know the tool, resolve_tool/discovery first "
                                "and read the family + self-descriptions, then re-interpret."))
                for t in intent.tools if t.resolution is None and t.source_hint is None]

    blocking = list(intent.unknowns) + unstated
    if blocking:
        return GateResult(outcome=Outcome.investigate, rail=rail, blocking=blocking)
    return GateResult(outcome=Outcome.proceed, rail=rail, blocking=[])
