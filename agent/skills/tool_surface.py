"""tool_surface — the ONE declaration of where each MCP tool sits on the agent's menu.

WHY THIS EXISTS. The design premise of this codebase is stated in CLAUDE.md as
"Primitives — the only tools the agent needs … The agent picks the right primitive; the
primitive enforces its category's invariants internally." That premise has a load-bearing
precondition nobody was checking: **the agent has to be able to SEE the menu.**

Measured 2026-08-07, BEFORE this module existed: the server registered **67** tools. The
routing table named **34**. Nine more were named elsewhere in CLAUDE.md (the numbered
protocol, the async pattern). **Twenty-four were named nowhere at all** — live, callable,
fully described to the model by their own docstrings, and absent from the one map the
agent plans against.

(Today: 66 registered — `download_resource` was deleted, its documented branch having
raised AttributeError since April — positioned 47 PRIMITIVE / 8 PROTOCOL / 11 LOW_LEVEL.
Read the split off `positioned()`, never off this paragraph.)

That is not a documentation defect. It is the same class of defect as a stale invariant
roster, and it has already cost real trust:

  * `run_install_command` mutates a conda prefix and appends a HAND-TYPED install_step.
    Every install primitive instead writes a typed `installed_packages[].install_method`,
    and `freeze.non_conda_installs` reads exactly that field to decide whether it may
    ADOPT a prebuilt BioContainer or must build the env itself. An install routed through
    `run_install_command` is INVISIBLE to that decision — so freeze adopted a container
    that did not contain the tool, and the honesty contract passed it, because every
    clause it checks was true of the container it adopted.
  * The tool's own docstring promised the opposite: that `installed_packages` become
    records "with an install_method derived from `channel`". The dual-write that did that
    was deleted; the sentence was not. A conscientious caller was bypassed exactly like a
    careless one.

The fix for a bad menu is not more prose. **A tool that must not be reached for casually
has to say so IN THE DESCRIPTION THE MODEL ACTUALLY READS** — the docstring is loaded on
every session, and CLAUDE.md's table is one grep away from being stale.

So this module is DATA, and `agent/skills/tool_surface.py` is its only home:

  * `REGISTRY` positions every registered tool as PRIMITIVE / PROTOCOL / LOW_LEVEL.
  * `guardrail()` COMPOSES the routing sentence for a LOW_LEVEL tool from short factual
    fields. The prose is generated. Nobody hand-writes a paragraph per tool, because
    hand-written prose is what lied in both cases above.
  * `apply_to(app)` appends that generated guardrail to the tool's SERVED description at
    import time. There is no second copy on disk to drift — the docstring stays about
    what the tool DOES, and the routing note is composed onto it.
  * `tests/test_tool_surface.py` fails the build when a registered tool has no position,
    when the registry advertises a tool that no longer exists, when a PRIMITIVE is missing
    from CLAUDE.md's routing table (or a table row names a ghost), when a `prefer` target
    routes sideways into another low-level tool, or when a served description lost its
    guardrail.

WHAT THIS IS NOT. It is not a second description of what each tool does — that is the
docstring's job and the docstring stays authoritative. Every field here is one clause,
and `prefer` names the tool that supersedes this one so a reader who doubts the line is
one grep from the code rather than one paragraph from a paraphrase of it.
"""
from __future__ import annotations

from dataclasses import dataclass

#: In CLAUDE.md's routing table. The agent's first-class menu — what it picks from.
PRIMITIVE = "primitive"
#: Named in CLAUDE.md outside the table: the numbered protocol, the async pattern, the
#: schema cheatsheet. Documented where it is used, so it needs no routing note.
PROTOCOL = "protocol"
#: Below the table. Reachable, sometimes correct, and easy to reach for WRONGLY — so it
#: carries a generated guardrail in its own served description.
LOW_LEVEL = "low_level"

POSITIONS = (PRIMITIVE, PROTOCOL, LOW_LEVEL)


@dataclass(frozen=True)
class ToolPosition:
    tool: str
    position: str
    """PRIMITIVE, PROTOCOL or LOW_LEVEL."""

    mutates: str = ""
    """ONE clause naming what this changes outside itself — a conda prefix, a draft, the
    filesystem, a remote host. EMPTY MEANS QUERY-ONLY, and the guardrail says so: a
    query-only tool cannot bypass a gate, which is most of why one is safe to reach for."""

    prefer: tuple[str, ...] = ()
    """The tool(s) that do this job and record more. Each must be PRIMITIVE or PROTOCOL —
    somewhere the agent can actually find. Pointing at another LOW_LEVEL tool routes in a
    circle, from one thing it was told not to reach for to another."""

    records_more: str = ""
    """THE DELTA: what `prefer` records that this does not, and which check reads it.
    Required whenever `prefer` is set. "Prefer X" with no reason is the hand-wavy prose
    this module exists to avoid — the reason is the whole content of the guardrail."""

    caveat: str = ""
    """A standing hazard that is not about a superseding tool: what silently goes
    unrecorded, or which claim this output cannot support."""

    direct_use: str = ""
    """When reaching for this directly IS the right call. A guardrail that only forbids
    teaches the agent to route around it; naming the legitimate case is what makes the
    rest of the sentence credible."""

    note: str = ""
    """For maintainers. Never rendered into the served description."""


def _t(**kw) -> ToolPosition:
    return ToolPosition(**kw)


REGISTRY: dict[str, ToolPosition] = {t.tool: t for t in [

    # ---- PRIMITIVE — the routing table in CLAUDE.md --------------------------------
    # Position only. What each one is for lives in the table row and its docstring; a
    # third copy here would be the drift this module exists to prevent.
    _t(tool="resolve_tool", position=PRIMITIVE),
    _t(tool="list_installed_pipelines", position=PRIMITIVE),
    _t(tool="install_conda_packages", position=PRIMITIVE),
    _t(tool="install_r_package", position=PRIMITIVE),
    _t(tool="install_pip_package", position=PRIMITIVE),
    _t(tool="install_jar_tool", position=PRIMITIVE),
    _t(tool="install_git_repo", position=PRIMITIVE),
    _t(tool="install_release_binary", position=PRIMITIVE),
    _t(tool="install_perl_package", position=PRIMITIVE),
    _t(tool="install_cargo_tool", position=PRIMITIVE),
    _t(tool="install_go_tool", position=PRIMITIVE),
    _t(tool="freeze", position=PRIMITIVE),
    _t(tool="freeze_from_image", position=PRIMITIVE),
    _t(tool="build_env_from_authors_recipe", position=PRIMITIVE),
    _t(tool="seal_workflow", position=PRIMITIVE),
    _t(tool="generate_user_guide", position=PRIMITIVE),
    _t(tool="download_reference_database", position=PRIMITIVE),
    _t(tool="run_pipeline_step", position=PRIMITIVE),
    _t(tool="run_step_in_container", position=PRIMITIVE),
    _t(tool="stage_authored_artifact", position=PRIMITIVE),
    _t(tool="start_service", position=PRIMITIVE),
    _t(tool="verify_service_dependency", position=PRIMITIVE),
    _t(tool="stop_service", position=PRIMITIVE),
    _t(tool="phenopacket_to_vcf", position=PRIMITIVE),
    _t(tool="snapshot_project", position=PRIMITIVE),
    _t(tool="cluster_module_avail", position=PRIMITIVE),
    _t(tool="upload", position=PRIMITIVE),
    _t(tool="download", position=PRIMITIVE),
    _t(tool="globus_task_status", position=PRIMITIVE),
    _t(tool="cluster_job_status", position=PRIMITIVE),
    _t(tool="stage_apptainer_image", position=PRIMITIVE),
    _t(tool="submit_workflow_job", position=PRIMITIVE),
    _t(tool="run_production_pipeline", position=PRIMITIVE),
    _t(tool="run_step_on_cluster", position=PRIMITIVE),

    # ---- PRIMITIVE — promoted 2026-08-07 -------------------------------------------
    # Each was live, callable and named NOWHERE in CLAUDE.md. Promoted rather than
    # guard-railed because each is either query-only (so it cannot bypass a gate — pure
    # navigation, and cheap to make visible) or is the ONLY tool for its job, which makes
    # a "prefer X instead" note impossible to write truthfully.
    _t(tool="agent_status", position=PRIMITIVE,
       note="query-only. Reads drafts, EnvCache, env_reports, data manifests, "
            "projects_access.yaml, ssh ControlMaster sockets and JobManager status, plus "
            "read-only git. The 'where am I' tool — the one an agent resuming a session "
            "most needs and could not previously find."),
    _t(tool="interpret_request", position=PRIMITIVE,
       note="query-only. The typed front door: RequestIntent -> completeness gate -> rail."),
    _t(tool="plan_request", position=PRIMITIVE,
       note="query-only. ExecutionPlan -> I8 lifted to authoring time -> topo order."),
    _t(tool="describe_sealed_step", position=PRIMITIVE,
       note="query-only typed read of a sealed WorkflowSpec. Re-running one recorded step "
            "of a sealed pipeline has no other entry point."),
    _t(tool="synth_fetch", position=PRIMITIVE,
       note="network-only fetch into a temp dir; writes no draft record. Paired with "
            "synth_build as the reconstruct-from-the-authors'-repo path."),
    _t(tool="synth_build", position=PRIMITIVE,
       note="mutates the prefix and records install_method type='synthesized', which "
            "freeze replays verbatim into the shipped image. Typed, so it is on the "
            "honest side of the run_install_command split."),
    _t(tool="verify_env_recipe", position=PRIMITIVE,
       note="the instrument behind one of the three artifacts that ARE the v1 acceptance "
            "criterion — it answers 'can a human actually rebuild from this recipe'. "
            "Naming it nowhere made that claim uncheckable in practice."),
    _t(tool="show_pipeline_draft", position=PRIMITIVE,
       note="query-only; its own docstring says 'Does not modify or finalize anything'. "
            "The draft is what seal_workflow will judge, so being able to LOOK at it "
            "before sealing is not a debugging convenience — it is how an agent finds a "
            "missing usage block before a refusal does."),
    _t(tool="list_available_resources", position=PRIMITIVE,
       note="query-only. The data-side sibling of list_installed_pipelines: what test data "
            "and genomes are already on disk. Asking BEFORE downloading is the same "
            "instinct the table already teaches for envs."),
    _t(tool="add_core_test_data", position=PRIMITIVE,
       note="downloads + registers a sequencing dataset into the core manifest. Promoted "
            "for consistency: download_reference_database and phenopacket_to_vcf are both "
            "in the table, and these three are the rest of that same acquisition surface."),
    _t(tool="add_core_pod5_data", position=PRIMITIVE,
       note="the long-read half of the same surface — nanopore pod5 signal data."),
    _t(tool="add_phenopacket", position=PRIMITIVE,
       note="registers a GA4GH phenopacket; every field is extracted from the JSON, "
            "nothing is supplied by hand. Feeds phenopacket_to_vcf, which is already in "
            "the table — the producer was missing while its consumer was listed."),
    _t(tool="acquire_reference_via_recipe", position=PRIMITIVE,
       note="runs the tool's OWN gather script on a compute node, sha256-sidecars every "
            "produced file and patches reference_databases with a cluster-locus entry — "
            "the use-the-authors'-own-machinery path for reference data."),

    # ---- PROTOCOL — named in CLAUDE.md outside the table ----------------------------
    _t(tool="start_pipeline", position=PROTOCOL, note="protocol step 1."),
    _t(tool="select_test_data", position=PROTOCOL, note="protocol step 3; the only "
       "producer of test_data.content_anchors, which I8 re-verifies at seal."),
    _t(tool="patch_pipeline", position=PROTOCOL, note="protocol step 5; its allowed-key "
       "list is spelled out in CLAUDE.md and enforced in code."),
    _t(tool="mark_step_validated", position=PROTOCOL, note="the narrow agent-asserted "
       "override described in the I3 row."),
    _t(tool="run_in_background", position=PROTOCOL, note="the async pattern section."),
    _t(tool="check_job", position=PROTOCOL, note="the async pattern section; carries a "
       "backgrounded tool's real return value inline under `result`."),
    _t(tool="list_jobs", position=PROTOCOL, note="the async pattern section. Answers 'what "
       "did I background', which is what a RESUMED session needs and check_job cannot give "
       "it — check_job needs a job_id you have to already hold."),
    _t(tool="install_pipeline_brief", position=PROTOCOL,
       note="POSITIONED, NOT SETTLED. This is one of the 'three front doors' the project "
            "intends to collapse to one, alongside interpret_request/plan_request and the "
            "bare protocol — which is a DESIGN decision scheduled for the front-door pass, "
            "not something to settle as a side effect of drawing the menu. Positioned "
            "PROTOCOL so the lint has an answer and the tool is not invisible; expect this "
            "entry to change when the front door is decided. Its Layer-2 invariant roster "
            "is already derived from agent/skills/invariants.py rather than hand-listed, "
            "so it cannot go stale in the meantime."),

    # ---- LOW_LEVEL — below the table, guardrail composed onto the description -------
    _t(tool="run_install_command", position=LOW_LEVEL,
       mutates="runs arbitrary shell inside the conda prefix and appends a hand-typed "
               "install_step",
       prefer=("install_conda_packages", "install_pip_package", "install_r_package",
               "install_jar_tool", "install_git_repo", "install_release_binary",
               "install_perl_package", "install_cargo_tool", "install_go_tool"),
       records_more="a TYPED `installed_packages[].install_method`. `freeze` reads exactly "
                    "that field to decide whether it may ADOPT a prebuilt BioContainer or "
                    "must build the env itself, so an untyped step is invisible to the "
                    "decision — this is how freeze once adopted a container that did not "
                    "contain the tool",
       direct_use="no install primitive fits the ecosystem, and you have read what freeze "
                  "will conclude from the step you leave behind",
       note="the defect that motivated this whole module. Two doors into it, both now "
            "closed at the freeze end by freeze.unaccounted_install_steps."),

    _t(tool="run_in_env", position=LOW_LEVEL,
       mutates="runs arbitrary shell inside a conda env",
       prefer=("run_pipeline_step", "run_step_in_container"),
       records_more="a `pipeline_step` carrying detected outputs, per-file validation and "
                    "resource_usage — the evidence I3 and I7 read at seal. A command run "
                    "here produced nothing, as far as the sealed record is concerned",
       direct_use="you want an exploratory command whose output is not a workflow "
                  "artifact"),

    _t(tool="validate_output", position=LOW_LEVEL,
       prefer=("run_pipeline_step", "run_step_in_container"),
       records_more="the same type-aware validation, run automatically over every detected "
                    "output and ATTACHED to the step — which is what makes it count for I3",
       direct_use="checking a file that was produced outside the step surface"),

    _t(tool="verify_installation", position=LOW_LEVEL,
       mutates="runs the verify command inside the env",
       prefer=("install_conda_packages", "install_r_package", "install_pip_package"),
       records_more="the same check as each primitive's `functional_check`, recorded "
                    "against the install it belongs to. Freeze re-runs recorded evidence "
                    "INSIDE the shipped image; a check with no install to hang on is not "
                    "re-run there",
       direct_use="probing an env you did not build through the install primitives"),

    _t(tool="create_conda_env", position=LOW_LEVEL,
       mutates="creates a conda prefix under envs/ and, with a pipeline_id, sets "
               "`conda_env` + `python_version` and appends a bootstrap install_step",
       prefer=("install_conda_packages",),
       records_more="nothing this one misses — it auto-creates the env when absent via the "
                    "same code path, so this call is OPTIONAL rather than a way around "
                    "anything",
       direct_use="you want an empty prefix at a specific python version or subdir before "
                  "any package has been chosen",
       note="measured: NO gate bypass. Its bootstrap step is deliberately excluded from "
            "freeze.requested_conda_specs. Low-level because it is redundant, not because "
            "it is dangerous."),

    _t(tool="search_package", position=LOW_LEVEL,
       mutates="writes `search_cache` on the draft when given a pipeline_id",
       prefer=("resolve_tool",),
       records_more="a tier ranking, a concrete `install_call` and an identity check. This "
                    "answers only 'which channels carry this NAME' — so an agent that "
                    "starts here can install a same-named different tool",
       direct_use="you already know the tier and only want to confirm a channel or version "
                  "exists"),

    _t(tool="fetch_r_package_deps", position=LOW_LEVEL,
       prefer=("install_r_package",),
       records_more="the install itself, with a typed `install_method`. This only reads "
                    "DESCRIPTION over the network",
       caveat="its `install_strategy` hands back literal `BiocManager::install(...)` and "
              "`remotes::install_github(...)` lines. Running those through "
              "`run_install_command` is the untyped path — pass them to "
              "`install_r_package(source=...)` instead",
       direct_use="you need the dependency list BEFORE deciding how to install"),

    _t(tool="check_service_health", position=LOW_LEVEL,
       mutates="runs the health command inside the conda env",
       prefer=("verify_service_dependency", "start_service"),
       records_more="a probe appended to the dependency's `health_check_log` — the "
                    "evidence I10 reads at seal. This tool records nothing, so a service "
                    "checked only here is an undeclared runtime prerequisite",
       direct_use="you want an ad-hoc liveness check on a service you are not going "
                  "to declare"),

    _t(tool="check_gpu", position=LOW_LEVEL,
       caveat="it probes the HOST, not the shipped image, and returns none of the keys I12 "
              "requires of a captured `runtime_probe` (`command`, `returncode`, `locus`) — "
              "so its output can never substantiate an `accelerator` claim, however it is "
              "reformatted",
       direct_use="deciding whether this machine has a GPU at all, before choosing an "
                  "accelerator claim to make"),

    _t(tool="cancel_job", position=LOW_LEVEL,
       mutates="SIGTERM then SIGKILL to the job's whole process group, then rewrites its "
               "status file to `cancelled`",
       caveat="cancelling a backgrounded PRIMITIVE kills the child before it merges its "
              "install_step or pipeline_step, so whatever it already wrote to the conda "
              "prefix or the image layers survives on disk with NO draft record",
       direct_use="a backgrounded job is genuinely stuck and you accept that what it "
                  "half-wrote is unrecorded"),

    _t(tool="discard_pipeline_draft", position=LOW_LEVEL,
       mutates="unlinks the draft yaml and its lock sentinel, irreversibly",
       prefer=("start_pipeline",),
       records_more="nothing — but a FRESH `pipeline_name` gets you a clean draft without "
                    "destroying anything, and this deletes every pipeline_step, "
                    "install_step, verification and test_data content anchor accumulated "
                    "so far",
       direct_use="you deliberately want the SAME pipeline_name with no resume semantics"),
]}


# ---------------------------------------------------------------------------------
# The generated guardrail. Composed from the fields above — never hand-written.
# ---------------------------------------------------------------------------------

MARKER = "ROUTING —"
"""What the lint greps for to prove a served description still carries its guardrail."""


def _join(names: tuple[str, ...]) -> str:
    quoted = [f"`{n}`" for n in names]
    if len(quoted) == 1:
        return quoted[0]
    if len(quoted) == 2:
        return f"{quoted[0]} or {quoted[1]}"
    return ", ".join(quoted[:-1]) + f" or {quoted[-1]}"


def guardrail(tp: ToolPosition) -> str:
    """The routing note appended to a LOW_LEVEL tool's served description.

    Composed, so the 11 guardrails cannot disagree with each other about what a guardrail
    says, and so a new one costs a few clauses rather than a paragraph of fresh prose.
    """
    if tp.position != LOW_LEVEL:
        return ""
    parts = [
        f"{MARKER} this is a LOW-LEVEL tool. It sits BELOW the primitive table in "
        f"CLAUDE.md; that table is the menu you should normally pick from."
    ]
    parts.append(
        f"It {tp.mutates}." if tp.mutates
        else "It is QUERY-ONLY — it changes nothing, so it cannot get around a check."
    )
    if tp.prefer:
        parts.append(f"Prefer {_join(tp.prefer)}, which also record {tp.records_more}.")
    if tp.caveat:
        parts.append(f"Note: {tp.caveat}.")
    if tp.direct_use:
        parts.append(f"Reaching for this directly is correct when {tp.direct_use}.")
    return " ".join(parts)


def apply_to(app) -> list[str]:
    """Append each LOW_LEVEL tool's generated guardrail to its SERVED description.

    Called once, at the bottom of `agent.mcp_tools`, after every submodule has registered.
    Composing at runtime rather than editing 11 docstrings means there is no second copy
    on disk: the docstring stays about what the tool DOES, and the routing note is
    derived from the registry every time the server starts.

    Returns the tools it annotated, so a caller (and the lint) can tell an annotation that
    silently reached nothing from one that landed.
    """
    registered = _registered_tools(app)
    touched: list[str] = []
    for name, tp in REGISTRY.items():
        note = guardrail(tp)
        tool = registered.get(name)
        if not note or tool is None:
            continue
        desc = tool.description or ""
        if MARKER in desc:
            continue
        tool.description = f"{desc.rstrip()}\n\n{note}"
        touched.append(name)
    return touched


def _registered_tools(app) -> dict:
    """`{name: tool}` for everything registered on the app, read SYNCHRONOUSLY.

    FastMCP keeps its components in a private dict keyed `tool:{name}@{version}`, and it
    has moved that store between versions. We read it by object identity (anything with a
    `.name` and a `.description`) rather than by parsing the key, so a key-format change
    does not silently return nothing.

    A silent no-op here is exactly the failure mode this module exists to prevent, so it
    is not defended against in this function — `tests/test_tool_surface.py` reads the
    SERVED description back through the public `list_tools()` and fails if a guardrail did
    not land. That test is the real guarantee; this is just the mechanism.
    """
    out: dict = {}
    store = getattr(getattr(app, "_local_provider", None), "_components", None)
    if isinstance(store, dict):
        for obj in store.values():
            name = getattr(obj, "name", None)
            if name and hasattr(obj, "description"):
                out[name] = obj
    return out


def positioned(position: str) -> list[str]:
    return sorted(t.tool for t in REGISTRY.values() if t.position == position)
