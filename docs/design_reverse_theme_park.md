# The Reverse Theme Park

**A design for intent-driven, rail-gated installs.**
Status: BUILDING. **Phase 1 LANDED** (behaviour-neutral) — the typed intent front door
(`agent/skills/intent.py` = `RequestIntent` + completeness gate; `intent_tools.py` =
`interpret_request`, advisory; `test_intent_gate.py` = §7 catalog, 16/16).
**Phase 2 LANDED** (behaviour-changing, user-approved) — RESOLVE shrunk: the 86-word
`_DOMAIN_TERMS` list + `domain_signal` + `assess_identity` are DELETED; `resolve()` now
emits identity FACTS (`identity_facts()`) for the ride to judge, with a clean
`install_call` (no identity poisoning), and a pip/cran-SCRAPED repo is a candidate the
ride confirms (never auto-anchored to the author tiers). The user's ruling made this the
thesis in the small: **the LLM is the identity judge, not a word-list** — it lands on a
tool and the honesty contract validates; genuinely-unsure-after-investigating → ASK.
Full fast suite green (1379). Deferred: reconciling the live intent corpus (35 rows pin
the deleted verdict) — a reviewed re-probe, not an auto-rewrite of the crown jewel.
**Phase 3 LANDED** (behaviour-changing, user-approved) — explicit lifecycle: seal
write-guard refuses a silent clobber of a sealed spec; `pipeline_state.current_state()`
is the ONE re-earned lifecycle deriver (ABSENT<DRAFT<ENV_BUILT<ENV_FROZEN<SEALED); the
fabricated `pipeline_status="in_progress"` default is killed (now DERIVED at seal). See
§8 (GROWS A LITTLE).
**Phase 4 LANDED** (behaviour-neutral, advisory) — composition: `agent/skills/plan.py`
= `ExecutionPlan` (a typed DAG) + `gate_plan()` (Layer-2's I8 lifted to authoring time);
`plan_tools.py` = `plan_request`, advisory like `interpret_request`. Three forks resolved
with the user (see §6): (a) **node vocabulary** — `Rail` is imported by reference and the
ONLY delta is `PlanRide = {SEAL}` (the terminal ride that is a plan node but not an entry
rail; produces the `workflow_spec` no rail does); a node's `action`/`produces`/`depends_on`
are all DERIVED, never stored, so no second truth. AUTHOR_PIPELINE is FOLDED IN (our
system has no author primitive — you author by running-and-validating; the ExecutionPlan
itself IS the authored pipeline), so the §6 worst case is a **5-node** DAG (3× INSTALL_ENV
→ RUN_STEP → SEAL = scenario-15's `install_env ⊕ run_step` + the seal terminal). (b) **the
I8 gate** — a node declares `consumes: [from_step | external]` mirroring runtime I8's
disjunction verbatim; the check is PROVENANCE-NOT-TYPE (runtime I8 is itself pure
provenance), the external vocabulary + the I8 sentence are single-sourced from
`spec_writer` (`EXTERNAL_SOURCE_KINDS`/`I8_STATEMENT`), and the gate is
NECESSARY-NOT-SUFFICIENT (seal re-checks on concrete paths and stays the authority).
Project/cluster data is expressed through the existing four kinds — the project directory
+ compute resource are authorized via `projects_access.yaml` and resolved at walk time (no
new runtime category). (c) **scope** — advisory author-and-gate; it DISPATCHES NOTHING
(an `execute_plan` loop over freeze/run/seal would be the forbidden composite primitive
that buries the per-seam gates); the agent WALKS the plan by calling the existing
primitives. Fast suite green (1437).
**Phase 5 LANDED** (2026-07-18, behaviour-additive) — the new rails, RE-SCOPED by grounding
the code first: two of the three named pieces were ALREADY realized, so building them again
would be redundant scaffolding. **DECLINE** (§4 / scenario 7) IS the completeness gate
(`out_of_scope → decline → DECLINE` + the required `out_of_scope_reason`), built in Phase 1 —
the §4 table itself calls DECLINE "the gate". **Router dispatch consuming a `Rail`** (deferred
out of Phase 3, Q2) IS Phase 4's `plan.py` — `_PRODUCES_FOR_ACTION`, `_SOURCE_ACTIONS`, and
`PlanStep.action` all route over `Rail`; the design never had an auto-executor (that is the
forbidden composite). So Phase 5's one genuinely-new behaviour is **RUN_STEP-of-a-sealed-workflow**
(scenario 5). The gap was precise: a sealed `WorkflowSpec` is WRITE-ONLY at the type level
(authored once at seal, thereafter only `yaml.safe_load`-scraped for summaries), and every run
primitive takes a hand-written command — so re-running a recorded step meant scraping the YAML
for a command we then EXECUTE in a shipped image (the bcftools-1.23.1 scrape hazard). The
deliverable is therefore the FIRST typed read-back of a sealed spec:
`spec_writer.load_workflow_spec` + `select_pipeline_step` (the anti-scrape seam, the WorkflowSpec
analog of `parse_intent`/`parse_plan`), surfaced by ONE advisory tool
`sealed_tools.describe_sealed_step` — the third member of the reverse-theme-park advisory-reader
family (`interpret_request` · `plan_request` · `describe_sealed_step`), each DISPATCHING NOTHING
and returning an untagged `{ok}` query dict OFF the honesty namespace. It returns the recorded
step's runnable facts (command · inputs · `freeze_request_key` · pinned digest · input-existence
preconditions · pinned-env contract state); the agent re-runs it via the existing
`run_step_in_container`, IN THE FROZEN IMAGE (validated == shipped), fail-fast on a missing input
(NO auto-materialize — that cascade would be the forbidden composite). §11's open RUN_STEP
questions resolve from the system's own principles, not a coin-flip. It lives in its OWN module,
NOT the tagged `workflow_tools` — an advisory reader's untagged `{ok/error}` returns belong with
its siblings, not among the honesty primitives, which is what keeps the outcome-tag ratchet
honest (`tests/test_outcome_tags.py`). Fast suite green. `tests/test_sealed_step_reader.py` (12).
**Phase 6 LANDED** (2026-07-18) — the checker RELOCATED to neutral infrastructure (§9's
"adversary property"). `.github/workflows/checker.yml` runs the fast, hermetic honesty tier
(`-m "not live and not integration_docker"`, 1452 passed / 119 deselected / ~85s) on GitHub's
runners on every push + PR — anchored by the crown-jewel ratchet
`test_committed_ledger_matches_a_fresh_sweep_of_the_code`, which RE-DERIVES the committed
`docs/outcomes_ledger.json` from a fresh AST sweep. Because CI recomputes the ledger FROM SOURCE
off the agent's machine, a locally-doctored ledger (or coverage overlay) that hides an untagged
terminal cannot make the gate pass — this is RELOCATION of an existing check beyond edit reach,
not a new check. The invocation is `-m "not live and not integration_docker"` (explicit, not a
bare `pytest tests/`) because GitHub runners ship Docker, so the `integration_docker` self-skip
would NOT fire and the slow tier would run by accident. **Honest scope, per §9's priority**: the
workflow file lives IN the repo, so it is itself in edit reach — CI makes any weakening of the
gate VISIBLE IN THE DIFF (tamper-EVIDENT), not tamper-PROOF; the blocking backstop is branch
protection ("require the `checker` status check" + required review), configured on GitHub, not in
code. That is exactly the stated posture — mistake-proof for a collaborator, tamper-evident for an
adversary a distant second. `live` network probes stay opt-in (a gate that reddens on a maintainer's
release is one people ignore — the corpus's own rule); the hermetic corpus-integrity tests DO run.
**Phase 7 LANDED** (2026-07-18, user-driven) — the user-facing layer, the LAST numbered phase.
Three pieces, all rendered purely from the verified record (§10.7). **(a)** a toggleable report
theme — the two HTML reports share ONE variable-driven stylesheet with cyberpunk (default) + a
professional/light palette, flipped by an in-page toggle; a test forbids any raw hex below the
`:root` blocks (a stray literal is a colour that won't switch). **(b)** the user guide restructured
into a Talos-style copy-pasteable WALKTHROUGH of how to run the tool on the compute resource it was
validated against (prerequisites → `srun` from the RECORDED placement → `module load` → get-the-
container → the ordered validated commands → TL;DR), with `executed_commands` still the single
honesty hook. **(c)** an OPTIONAL agent-authored narrative slot (`generate_user_guide(overview,
traps)`) for the "what it does"/"traps" prose no record can hold — labelled authored-not-verified,
omitted (never fabricated) when absent. The reverse-theme-park split applied to the deliverable
itself: a verified skeleton with a free authored middle. `test_phase7_reports_and_guide.py` (11).
**All 7 build-sequence phases now LANDED.** The frame is complete; remaining work (intent-grid
coverage climb, identity-to-disk, cross-cutting L1 checks) is outside the numbered phases.
The rest of this document is the plan, unchanged.
Date: 2026-07-18.

---

## 0. The one-sentence vision

A **deterministic envelope — typed intake at the front, machine-verified exit at the back — wrapped around a deliberately free LLM middle.** Code owns everything that must be trustworthy (what the request *means*, the *verification*, the *record on disk*). The LLM owns everything open-ended (which tool, which install path, how to investigate, how to repair a broken build).

The two mistakes we must never make: letting the LLM own the trustworthy parts, or letting code own the open-ended parts. Today we make **both** — the LLM improvises the intake with no model, and code (the resolver's 86-word list + tier gates) tries to do judgment it's bad at.

## 1. The metaphor: a reverse theme park

A normal theme park: the *rides* are scripted and on-rails; walking *between* rides is free.

Ours is **inverted**:

- **The park is on rails.** The routes between steps are deterministic code. You cannot wander off the path, seal before you freeze, or ship an env that never built.
- **Each ride is free.** A "ride" is one gated step (resolve a tool, install it, validate it). It has a **typed input contract** and a **typed output contract** — but *inside*, the LLM does whatever it takes to get from the input to the output. A ride is a black box with a locked door on each end.

Formally: the system is a **finite set of RAILS** (top-level routes). Each rail is an **ordered sequence of RIDES** (gated steps). Each ride is `typed_input -> [ LLM free to improvise ] -> typed_output`, and the output is verified before the next ride begins. The honesty contract is just the exit gate of the last ride.

This is the literal realization of the standing principle *"freedom in the search, gate what ships."* Freedom lives inside rides; gates live between them.

## 2. The front gate: `RequestIntent`

The park has one entrance. Before any rail is chosen, the raw prompt is turned into a **typed interpretation** — the LLM's reading of what the user wants, as a validated model, not a free-form guess.

```
RequestIntent:
  kind: Kind                       # THE RAIL SELECTOR (see §4)
  tools: list[ToolRequest]         # for install / add
  target_env: EnvRef | None        # an existing frozen env, for add / step / reproduce
  compute: local | cluster | unspecified
  data_ops: list[DataOp] | None    # for transfer_data
  raw_prompt: str                  # ALWAYS kept verbatim
  unknowns: list[Unknown]          # what the LLM could NOT fill — drives the gate
  out_of_scope_reason: str | None

ToolRequest:
  name: str                        # verbatim as the user said it ("cellranger", "talos")
  version: str | None              # verbatim, UNRESOLVED — None means "user didn't say"
  source_hint: SourceHint | None   # a repo URL / release link / channel the user supplied
  purpose: str | None              # "for scRNA cell calling" — domain context for disambiguation

Kind = install_env | add_to_env | run_step | transfer_data | reproduce | out_of_scope | ambiguous
```

**The load-bearing rule (from the `WorkflowSpec.outputs=[]` scar): "unknown" is a value the LLM must STATE, never a fabricated default.** `version: None` means the user didn't specify — it is not silently filled with "latest." Every gap goes into `unknowns[]` explicitly. A model that defaults doesn't catch missing intent, it *authors* a fake one.

`RequestIntent` is the FIRST typed record and the whole reason the front door stops being brittle: the intake becomes a thing we can validate, log, test against a corpus, and route on — instead of the agent improvising a fresh interpretation every time.

## 3. The completeness gate: ask / investigate / proceed / decline

The gate reads `kind` and `unknowns` and produces exactly one of four outcomes. This is [[feedback-earn-the-refusal]] promoted from a preference into a structural component:

| Outcome | When | Example |
|---|---|---|
| **DECLINE** | `kind == out_of_scope` | "Write me a poem." "Summarize this PDF." |
| **ASK** | `kind == ambiguous`, OR a *required* field is unknown AND cannot be found by investigation | "Install that aligner" (which one?). "Set up my env" (which tools?). |
| **INVESTIGATE** | intent is clear but a *mechanical* detail is missing AND findable | version omitted → find latest; repo not given but discoverable; near-miss version → resolve to the real one. The LLM fills it and **records how**. |
| **PROCEED** | every required field is known | "Install samtools 1.21." |

The ASK/INVESTIGATE split is the whole point: **never ask the user for what you can find yourself; always ask when the intent itself is underspecified.** Unsure is a reason to *look*, not to stop — but a genuinely empty prompt is a reason to ask, not to guess.

## 4. The rails (the finite set of routes)

Seven kinds, seven rails. This is the *complete* top-level surface — if a request doesn't fit one, it's `out_of_scope`.

| Rail | Kind | What it does | Reuses today? |
|---|---|---|---|
| **INSTALL_ENV** | `install_env` | build a new env with 1..N tools, validate, freeze, deliver | ✅ primitives exist |
| **ADD_TO_ENV** | `add_to_env` | add a tool to an existing (unfrozen) draft, or re-freeze an env with one more tool | ⚠️ partial |
| **RUN_STEP** | `run_step` | run one step against an existing frozen env — local or cluster | ✅ run_step_in_container; "step of a *sealed* workflow" now built (Phase 5: `describe_sealed_step` reads the recorded step, `run_step_in_container` executes it in the frozen image) |
| **TRANSFER_DATA** | `transfer_data` | move bytes to/from the cluster, no install at all | ✅ `_ad_hoc` + upload/download exist |
| **REPRODUCE** | `reproduce` | rebuild an env from a recipe and digest-check it | ✅ `verify_env_recipe` exists |
| **DECLINE** | `out_of_scope` | explain scope, do nothing | 🆕 new (the gate) |
| **CLARIFY** | `ambiguous` | ask one targeted question, then re-enter | 🆕 new (the gate) |

## 5. The rides (the reusable gated steps)

The good news, and the reason this is *not* a rewrite: **the rides already exist as primitives.** A rail is an ordering of these; each already has a typed-ish I/O boundary we tighten.

| Ride | Input contract | Output contract | Primitive today |
|---|---|---|---|
| **RESOLVE** | a `ToolRequest` | registry FACTS: which ecosystems have it, at what versions, from what repo | `resolve_tool` (shrunk — see §7) |
| **ACQUIRE** | source + version | fetched source / image / binary, hash-anchored | `download_*`, `synth_fetch`, git clone |
| **INSTALL** | a resolved tool + env | the tool present in the env | `install_conda_packages`, `install_pip_package`, … (10 tiers) |
| **VALIDATE** | a step + declared outputs | ground-truth pass/fail (filesystem + exit + type-aware) | `run_pipeline_step`, `validate_output` |
| **FREEZE** | env + tool list | content-addressed image + SBOM + recipe + attestation, all Layer-1 gated | `freeze`, `freeze_from_image`, `build_env_from_authors_recipe` |
| **RUN** | frozen env + command | validated run inside the shipped image | `run_step_in_container`, `run_step_on_cluster` |
| **SEAL** | a validated run | a `WorkflowSpec`, Layer-2 gated (I0/I3/I4/I6/I7/I8) | `seal_workflow` |
| **DELIVER** | frozen env + target | Apptainer `.sif` on the cluster, sha256-round-tripped | `stage_apptainer_image`, `submit_workflow_job` |

The rides are the vocabulary. The redesign adds the **rail** (deterministic ordering + state) and the **front gate** (intent + completeness) — and *subtracts* the fake intelligence that RESOLVE currently carries.

### 5.1 Controlled inputs — the run-step substance (params · reference data · configs)

The RUN / AUTHOR_PIPELINE rides don't just take "a command." Their real substance is three input classes, and the goal is that all three are **declared and controlled**, not buried in a command string:

- **reference data** — already a controlled rail (`ReferenceDatabase`: typed, versioned, sha256-anchored, I5). ✅
- **config files** — already a controlled rail (`RuntimeConfig`: typed, declared format). ✅
- **parameters** — the gap. Today they live as free text inside `command_template`. Promote them to a declared, typed structure with **sane defaults made explicit** and per-trial overrides. Then "default is good" becomes a *recorded* choice, the propose-and-confirm gate can show the exact params before cluster time, and the params become part of the reproducible record — not an invisible string.

This is **not** scientific-correctness validation (that stays the scientist's job, §9). It's making the *mechanical* inputs — the heavy lifting before analysis — first-class and reproducible.

### 5.2 Validation depth — a disclosed ladder, never a boolean

An image (or a pipeline) is never "validated: yes." It is validated **to a depth**, against **named data**, on a **named compute locus** — all three recorded and shown.

| Rung | Proves | Data | Compute |
|---|---|---|---|
| **L0 smoke** | binary present, links, launches | none | local Docker |
| **L1 self-contained functional** | tool *processes* data, not just launches | none — the test **generates its own** (the `functional_check` pattern, generalized) | local Docker |
| **L2 fixture functional** | real workload → valid-typed output | authors' own test data **>** fitting core data **>** synthesized | local container, or cluster if data is cluster-only |
| **L3 cluster functional** | runs on the real target, real resources (I7 authoritative) | cluster-resident | cluster (scratch sandbox) |

**Default policy** (deepest-cheaply-possible, always disclosed):
- **L0 always** (free, at freeze).
- **L1 conservative by default** — the sweet spot: real functional signal, zero data-selection risk. Generalizing self-contained checks is the highest-leverage validation work.
- **L2 opportunistic** — when fitting authors'/core data is cheaply available locally, *especially for a complex tool where "does it work end-to-end" matters more than "does it launch."*
- **L2-deep / L3 = deliberate opt-in** — the user asks, or the tool is cluster/GPU-bound / needs a cluster-only DB.
- Intent can override in either direction.

**Three honesty traps this ladder exists to avoid:**
1. **Disclose the rung, never fake it.** A green "validated" that only ran L0 is "green terminal, wrong tool" again. The report states rung + data + locus. (The audit's "34/49 FileTypes are touch-satisfiable" finding lives exactly here.)
2. **Wrong data → *false failure*, so data-fit is a gate.** A proteomics tool fed a FASTQ errors → validation fails → but the tool is fine. A false fail is worse than an honest "not functionally tested." Run L2 only with data that FITS; if unsure, drop to L1/L0 and disclose. This is why the core corpus (short+long-read **human genomics** — exome/wgs/rnaseq/wgbs/hic + ONT/PacBio, **no reference DBs**) is *not* a universal validator.
3. **Locus bounds the claim.** I7 timings authoritative only native; "validated == shipped" only in the shipped image; cluster-worthiness only on the cluster. A laptop run never masquerades as a cluster promise.

**Reference-DB coupling.** A tool that can't run without a DB (VEP/kraken/BLAST) has its max depth *bounded by DB availability + locus* — a cluster-only DB forces L3; no DB caps it at "L0 smoke — DB not present," disclosed.

**The ladder recurses to the pipeline level.** `AUTHOR_PIPELINE` validates the *whole DAG* on **test data**: pipeline-smoke (does the chain execute end-to-end on a tiny input?) → pipeline-L2 (valid-typed outputs on representative test data, in-container) → pipeline-L3 (same on the cluster scratch sandbox) → **SEAL**. The **production run** (`submit_workflow_job`, real data, project dirs) then executes the *sealed* pipeline and **inherits** its trust from the seal + frozen image (validated == shipped) — it does **not** re-earn validation per run. This is why the HPC bridge already splits into a **validation chain** (`run_step_on_cluster`, scratch, bounded, poll-to-seal) and a **production chain** (`submit_workflow_job`, project dirs, submit-and-document). Test-run first, seal, then production.

## 6. Composition — the itinerary (one intent, many rails)

Most real requests are **not one rail**. The governing concept is *complexity through simplicity*: the mechanical pieces already exist; the power is in *chaining* them. So the front gate produces intent, and a **PLAN ride** decomposes that intent into a typed **`ExecutionPlan`** — an ordered DAG of rail invocations, the output of an earlier rail feeding the input of a later one.

The plan is the **itinerary through the park.** The LLM chooses it (first-principles reasoning about what has to happen), but once chosen it is a **typed, validated, on-rails** structure: every node is a known rail, every dependency resolves, and it terminates at the requested outcome. Planning is itself a ride — free inside, but its output contract is "a valid plan."

```
ExecutionPlan:
  goal: str                    # the end the user asked for ("xyz results on cluster xyz")
  steps: list[PlanStep]        # a DAG
  PlanStep:
    id: str
    rail: Rail                 # INSTALL_ENV | RUN_STEP | TRANSFER_DATA | ...
    intent: RequestIntent      # the sub-intent this step satisfies
    depends_on: list[str]      # ids of steps whose outputs this consumes
    produces: OutputRef        # env digest | workflow spec | file set
```

**The completeness gate runs on the PLAN, not just the intent.** A plan is executable only if every step's inputs are satisfied by a prior step's `produces` OR an external source (test data, a **project directory**, a reference DB). That is exactly Layer-2's **I8** ("every input traces to a prior output or an external source"), lifted to plan-authoring time — a plan with a dangling input fails the gate *before anything runs.*

### The worst case, solved from first principles

> "Install toolx, tooly, toolz as separate envs and create a pipeline to get out xyz results. Then run this pipeline on my compute resource xyz, using data in the project directory."

Intent → the PLAN ride decomposes to:

1. `INSTALL_ENV(toolx)` → env_x @ digest_x  ┐
2. `INSTALL_ENV(tooly)` → env_y @ digest_y  ├ independent — run in parallel
3. `INSTALL_ENV(toolz)` → env_z @ digest_z  ┘
4. `AUTHOR_PIPELINE(x→y→z ⇒ xyz)` → workflow draft   (depends 1,2,3) — a rail of RUN+VALIDATE rides; the *creative* part (what's the command DAG?) is the free-inside step, gated by its output contract: the steps must actually run and produce type-validated `xyz` outputs
5. `RUN_STEP` × N on `compute = cluster:xyz`, data from `project_path`   (depends 4)
6. `SEAL` → WorkflowSpec pinning **envs[x,y,z]** by digest   (depends 5)

**Every mechanical piece already exists.** Multi-env chaining is *already* a `seal_workflow` feature — it records `envs[]` and digest-matches each step's container against the set of all frozen env digests. Cluster execution is `run_step_on_cluster` (validation) / the production chain (real run). The project-directory data + the compute resource resolve against `projects_access.yaml` — the auth/offload surface you already have. **Nothing new is built to solve this request.** The plan layer deduces the chain; the rails carry it, gated at every seam.

That is why "the worst scenario" isn't special-cased: it's the *same rides in a longer itinerary.* The honesty contract still gates every env (Layer 1) and the sealed pipeline (Layer 2), so a 6-step plan is exactly as trustworthy as a 1-step one — that's the payoff of building on the verified core instead of around it.

## 7. The scenario catalog — grounded in reality

This is the part that matters. Every scenario the user raised, plus the ones they imply, mapped to intent → gate → rail. **This table is the acceptance surface** — the design is only real if it handles every row.

| # | User does… | `kind` | `unknowns` | Gate | Rail / behaviour |
|---|---|---|---|---|---|
| 1 | "Install talos from github.com/X/talos, v11" (repo + version given) | install_env | — | PROCEED | INSTALL_ENV; `source_hint` pins the repo, skips discovery |
| 2 | "I want samtools, bcftools, and bwa in one env" | install_env | — | PROCEED | INSTALL_ENV with `tools:[3]` → 3× RESOLVE/INSTALL → **one** FREEZE (already supported) |
| 3 | "Install bwa 0.7" but real is 0.7.17 (near-miss) | install_env | version imprecise | INVESTIGATE | RESOLVE returns facts; LLM sees `0.7` is a clean prefix of exactly one real `0.7.17` → resolves, **records** "asked 0.7, installed 0.7.17" |
| 3b | "Install X 3.4" but 3.4.1 AND 3.4.2 both exist (ambiguous near-miss) | install_env | version ambiguous | ASK (or PROCEED-latest-patch **with disclosure**) | LLM won't silently pick; discloses the choice |
| 4 | "bwa=1.21, samtools=0.7.17" (versions transposed by accident) | install_env | both versions unreal | ASK | RESOLVE facts show bwa has no 1.21 but samtools does, and samtools has no 0.7.17 but bwa does → LLM: "did you swap these?" **No heuristic finds this; judgment over two fact-sets does.** |
| 5 | "Just generate step 2 of my existing pipeline" | run_step | which artifact/step | ASK if unclear, else PROCEED | RUN_STEP against a frozen env — **NEW rail**, load the artifact, run one step |
| 6 | "Just move these FASTQs to the cluster" | transfer_data | dest maybe | INVESTIGATE/ASK | TRANSFER_DATA via `_ad_hoc` — **no install spun up at all** |
| 7 | "Write me a poem" / "analyze this spreadsheet" | out_of_scope | — | DECLINE | explain scope, do nothing |
| 8 | "Install that aligner" (no tool named) | ambiguous | tool name | ASK | CLARIFY — one targeted question |
| 9 | "Install cellranger" (name collides with a CRAN parser) | install_env | identity risk | INVESTIGATE | `purpose` + RESOLVE facts; LLM confirms identity, **records the anchor** (kills "green terminal, wrong tool") |
| 10 | "Install the latest scanpy" (version omitted) | install_env | version | INVESTIGATE | RESOLVE latest, record it |
| 11 | "Rebuild the env from last week's recipe" | reproduce | — | PROCEED | REPRODUCE → `verify_env_recipe` digest-check |
| 12 | "Add multiqc to my talos env" | add_to_env | — | PROCEED | ADD_TO_ENV → re-freeze |
| 13 | "Install [gated tool], I have a license" | install_env | license terms | INVESTIGATE/ASK | INSTALL_ENV; POLICY_CLEAN (I13) gates delivery; tarball-only |
| 14 | Install succeeds but a step later fails at runtime | (mid-rail) | — | — | the RIDE is free to repair — retry, add a missing dep, switch tier — until its output contract is met or it gives up honestly |
| 15 | "Install x, y, z as separate envs, build a pipeline for xyz results, run it on cluster xyz using project data" (the worst case) | install_env ⊕ run_step | — | PROCEED (plan-gated) | **COMPOSITE** → PLAN ride decomposes into a 6-step DAG (§6); every mechanical piece already exists; on rails, gated at every seam |

Rows 3, 3b, 4, 9 are the thesis in miniature: **structured facts (RESOLVE) + LLM judgment (the ride) catch mistakes no word-list ever could** — and every judgment is *recorded*, so it's auditable.

## 8. What changes, what stays — the fate of the code

**STAYS (the asset — barely touched):**
- The honesty contract (`env_honesty`, `spec_writer`) — the exit gate. Starts reading *typed* records.
- The install primitives (all 10 tiers), `freeze`/`seal`, `run_step_in_container` — the rides' vocabulary.
- The HPC bridge (`transfer`, `compute_access`, `run_cluster_step`) — already config-driven, zero machine literals in runtime code (verified).

**SHRINKS HARD:**
- `resolver.py` (1322 LOC) — delete the 86-word `_DOMAIN_TERMS` list and every identity/gate heuristic. Identity judgment moves to the ride (the LLM). What remains is a thin **registry-facts** probe: "conda/PyPI/CRAN has X at versions [...] from repo Y." Facts, not opinions. This dissolves the stuck "refuse-detector Option A vs B" decision — there is no detector to tune.

**GROWS A LITTLE:**
- `pipeline_state.py` — explicit rail states + legal transitions (`draft → env_built → env_frozen → run_validated → sealed`). A whole class of "sealed before froze" becomes structurally impossible, not runtime-checked.

**NEW (all thin):**
- `intent.py` — the `RequestIntent` model + the completeness gate (§2–3).
- `plan.py` — the `ExecutionPlan` model + the PLAN ride (§6). Deduces the itinerary; I8-gated at authoring time.
- The router — `kind → rail` (§4). Deterministic dispatch.
- Two new rail behaviours: DECLINE and RUN_STEP-of-a-sealed-workflow.

**Net: fewer lines.** The brittleness lives entirely in the untyped intake and the resolver's fake intelligence; the design types the first and deletes the second.

## 9. Verification posture (unchanged priority)

Priority is **mistake-proof for a collaborator**, tamper-evident-for-an-adversary a distant second. So:
- Ground-truth verification (real Docker) — **keep, already done.**
- Relocating the checker out of the agent's edit reach (CI-owned) — **the adversary property; ✅ LANDED (Phase 6, 2026-07-18).** `.github/workflows/checker.yml` runs the fast honesty tier on GitHub's runners on every push + PR, re-deriving `outcomes_ledger.json` from source so a locally-doctored ledger can't pass. Faithful to this priority: it relocates EXECUTION + makes tampering diff-visible (tamper-evident), not tamper-proof — the workflow file is itself in-repo. "Signed" was scoped OUT deliberately: the ledger is a DERIVED artifact and CI re-derives it, so cryptographic signing would defend a threat model ranked "a distant second" with machinery the re-derivation already obviates. The blocking backstop is branch protection (GitHub-side config), not code.
- Typing the record seams (§2, and the parked "type the nouns" work) — **do this**, because a mistaken agent that scrapes a dict is exactly the collaborator-grade failure we're defending against.

**Decided NON-GOAL — a decision-trace / "build log" of the messy path.** Considered and rejected. The clean, portable **recipe** (`recipe.md`/`.yaml`, already rendered from the verified record and digest-checked by `verify_env_recipe`) is the reproduction artifact — it's the *destination*, and a human never needs the dead-ends to rebuild. The journey has value only when an install *fails* (no recipe exists), and that case is served by an honest failure summary ("what I tried, where it stuck") — not a trajectory-logging subsystem. Recorded here so it isn't re-proposed.

## 10. Build sequence (not a big-bang)

1. **Phase 1 — the front gate, behaviour-neutral.** Add `intent.py` + router + the scenario catalog (§7) as a live test corpus. The middle is unchanged; we just make the intake typed and routed. The intent grid already exists to measure this — it stops measuring a component that doesn't exist and starts measuring one that does.
2. **Phase 2 — shrink RESOLVE.** Delete the heuristics; resolver returns facts only; identity judgment moves into the ride. Prove the scenario catalog rows 3/4/9 pass with judgment, not detection.
3. **Phase 3 — explicit rails.** Formalize the lifecycle states + legal transitions in `pipeline_state.py`. Wire the seven rails to the router.
4. **Phase 4 — composition. ✅ LANDED (2026-07-17).** Added `plan.py` (`ExecutionPlan` + `gate_plan`, the I8-at-authoring gate) + `plan_tools.py` (`plan_request`, advisory). The §6 worst case gates GREEN as a 5-node DAG (`tests/test_plan_gate.py`). "Execute a multi-rail plan" is realized the theme-park way — the plan is authored + gated, then WALKED by the agent calling the existing primitives in topo order; auto-dispatch is deferred (it is the forbidden composite primitive; a Phase-5 question the user has not opened). Node vocabulary = `Rail` imported + the 1-element `PlanRide={SEAL}` delta (AUTHOR_PIPELINE folded into the RUN ride); the gate is the runtime I8 lifted (provenance-not-type, single-sourced vocabulary, necessary-not-sufficient).
5. **Phase 5 — the new rails. ✅ LANDED (2026-07-18).** Re-scoped by grounding the code: DECLINE (scenario 7) IS the Phase-1 completeness gate and router-dispatch-consuming-a-`Rail` IS Phase-4's `plan.py` — both already realized. So the one genuinely-new behaviour is RUN_STEP-of-a-sealed-workflow (scenario 5), built as the FIRST typed read-back of a sealed spec (`spec_writer.load_workflow_spec` / `select_pipeline_step`, the anti-scrape seam) + the advisory `describe_sealed_step` reader (`sealed_tools.py`, the third member of the `interpret_request`/`plan_request` advisory-reader family — dispatches nothing). The agent re-runs the recorded step via the existing `run_step_in_container` IN THE FROZEN IMAGE, fail-fast on a missing input, no auto-materialize.
6. **Phase 6 — relocate the checker to CI. ✅ LANDED (2026-07-18).** The adversary property. `.github/workflows/checker.yml` runs the fast, hermetic honesty tier (`-m "not live and not integration_docker"`) on GitHub's runners on every push + PR. The crown jewel is that CI RE-DERIVES `docs/outcomes_ledger.json` from a fresh source sweep (`test_committed_ledger_matches_a_fresh_sweep_of_the_code`) off the agent's machine — a locally-doctored ledger can't make it pass. Relocation, not a new check. Honest limit (per §9): the workflow is in-repo, so tampering is diff-VISIBLE, not impossible; the merge-blocking backstop is branch protection, configured GitHub-side.
7. **Phase 7 — the user-facing layer: guides + report visual identity. ✅ LANDED (2026-07-18, user-driven).** Deliberately last; the reports already render **purely from the verified records**, so the visual identity / how-to layout was safe to design after the records and rails were solid. Three pieces, all faithful to that principle. **(a) Toggleable report theme** — the two HTML reports (env report · run dashboard) share ONE stylesheet (`env_report_html._CSS`, imported by `run_dashboard_html`); every colour now flows through CSS variables, with cyberpunk the default palette and a professional/light palette overriding the same names, flipped by an in-page toggle (persisted to localStorage) shared via `_open_page`/`_close_page`. The tidy discipline is a test: NO raw hex below the `:root` blocks (a stray literal is a colour that won't switch). **(b) Talos-style user guide** — `user_guide.render_user_guide` restructured from a terse spec-dump into a copy-pasteable WALKTHROUGH of how to run the tool on the compute resource it was validated against (prerequisites table → grab-a-node `srun` → `module load` → get-the-container → the ordered validated commands → TL;DR). The SKELETON is DERIVED from the record (the `srun` line is the placement the run RECORDED, or a clearly-labelled example when none was; `executed_commands` stays the single honesty hook, so a step that didn't pass can't appear). **(c) The narrative slot** — the guide's warm prose ("what the tool does", "traps we already hit") is agent-AUTHORED and OPTIONAL (`generate_user_guide(overview=…, traps=[…])`), rendered in clearly-labelled 'authored, not machine-verified' sections and OMITTED (never fabricated) when absent. That is the reverse-theme-park split applied to the deliverable itself: a verified skeleton with a free authored middle. `tests/integration/correctness/test_phase7_reports_and_guide.py` (11).

Each phase ships something usable and is independently reversible. No phase requires throwing away working machinery.

## 11. Where to shoot holes

- ~~Do some requests span two rails?~~ **RESOLVED (§6):** yes — the PLAN ride decomposes one intent into a multi-rail DAG. Open sub-question: does the PLAN ride ever need to *re-plan* mid-execution when a ride fails in a way that changes the itinerary (e.g. a tool can't be installed at all → the pipeline goal is unreachable)? Proposed: a failed ride surfaces to the plan, which may replan or DECLINE — but replanning must itself be gated, not silent.
- Is `unknowns[]` the right gate input, or does the gate need per-field confidence?
- ~~RUN_STEP against a *sealed* workflow: do we re-run in the frozen image, or re-materialize inputs? What does "one step" mean when steps chain?~~ **RESOLVED (Phase 5, from the system's own principles):** re-run IN THE FROZEN IMAGE (validated == shipped — the frozen image IS the artifact); do NOT re-materialize inputs (re-running upstream steps is a silent cascade = the forbidden composite), so fail-fast on a missing input with a loud precondition (`all_inputs_present`), mirroring `run_step_on_cluster`'s `remote_paths_exist`; "one step" = exactly one recorded `PipelineStep`, selected by number, and chaining multiple steps is the PLAN layer's job (or the user asks per-step). `describe_sealed_step` surfaces the runnable facts; `run_step_in_container` executes + validates.
- Multi-tool / multi-env plans (scenarios 2, 15): if tool A resolves to conda but tool B forces a container-native build, does the shared FREEZE still hold? And in a multi-env plan, is the AUTHOR_PIPELINE ride's command-DAG reasoning reliable enough, or does it need its own structured contract? (Believed yes on freeze; the DAG-authoring reliability is the real unknown.)
- Does the intent + plan model belong in code the agent fills via structured output, or is it a prompt-shaped contract? (Structured output — but confirm the MCP surface supports forcing it.)
