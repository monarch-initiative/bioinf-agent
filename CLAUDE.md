# Bioinformatics Install Agent

Installs bioinformatics tools into isolated conda envs, validates them against test data, packages as HPC Docker images, and emits a machine-verified spec. Designed to be **a solved component** — call once per tool/version, get a trustworthy artifact, never look at it again.

```bash
./scripts/setup.sh          # one-time, one mode: private miniforge + runtime env (./.conda_runtime) + editable install + core_tools env + the full test-data corpus (chr22, 8 read datasets, ACTB phenopacket)
./scripts/setup.sh --check  # systems check (scripts/doctor.py) — every FAIL names its fix
source scripts/activate.sh  # this shell: the runtime env's nextflow (+ its own Java) on PATH, to run a rendered pipeline by hand
```

Then drive via Claude Code MCP, or any agent that speaks our tool surface.

---

## The honesty contract

Two machine-verified layers — nothing is taken on faith from the agent.

**The invariant roster is DATA, not prose.** `agent/skills/invariants.py` declares every invariant — id, layer, statement, and the function that refuses on it — and `tests/test_invariant_registry.py` fails the build when a clause emits an unregistered id, when the registry advertises a gate nothing runs, or when the tables below diverge from it. Look an invariant up in the registry, not here.

### Layer 1 — the env image (`freeze` → `env_honesty.check_build`)

An env is **built and validated INSIDE the image it ships**, so install==ship is ONE event. The guarantees are declared as data in `env_honesty.LAYER1_GUARANTEES`; `freeze` refuses to register an env on any violation.

| Guarantee | Means | Source of truth |
|-----------|-------|-----------------|
| **BUILT** | the env image + `image_digest` resolve in the local Docker daemon and the image is the architecture the record claims. `platform` is the request; `image_arch` is read off the shipped image, and the two must agree. A digest that names no architecture (a manifest list) is refused; a record with no `image_arch` is UNOBSERVED, never a pass | `docker image inspect` + `locus.image_arch` / `locus.target_arch` |
| **VALIDATED_IN_IMAGE** | every tool's evidence command re-runs green INSIDE the shipped image and discriminates: it names the tool as a word-boundary token and is not a constant-true short-circuit, a comment-hidden token, or rc-laundered (`… \|\| true`). Agent-authored evidence (`freeze_from_image`, or `freeze` with `evidence=`) is also run in a digest-pinned control image that LACKS the tool and must FAIL there | `run_in_container` + `env_honesty.evidence_shape_violation` + the control-image run |
| **POLICY_CLEAN** | I12 accelerator honesty + I13 license firewall (gated ⇒ `redistributable: false` AND `licenses[]`). I12: `cuda`/`rocm` need `toolkit_version`, `mps` must be `dev_only`, and the toolkit read off the shipped image (`image_accelerator`) must match the claim; `runtime: runtime_verified` needs a captured probe (`{command, returncode, locus}`) plus `min_driver_version`. Absent observation is UNOBSERVED. Under-claiming is reported, over-claiming is refused | `env_honesty._clause_accelerator` / `_clause_accelerator_observed` / `_clause_license` + `locus.image_accelerator` |
| **PROVENANCE_CLEAN** | a synthesized install (the synthesis tier) carries its audit trail into the recipe: every sub-command tagged `extracted` (lifted verbatim from a named repo file, anchored by that file's sha256) or `agent_authored`. `NOT_APPLICABLE` when nothing was synthesized | `env_honesty._clause_provenance` |
| **WELL_FORMED** | every sub-record with a declared model parses (today: `shipped_binaries[]` → `ShippedBinary`), asserted at both the write (`EnvCache.register`) and the serve (`check_build`) | `core_data.shipped_binaries` |

**Typed records at the disk seam:** `extra="forbid"` and no fabricated defaults — every field required, `None` a value a producer must state. The producer captures narrowly; a reader never scrapes a version out of a blob (absence, rendered "unrecorded", beats a confident lie).

### Layer 2 — the workflow run (`seal_workflow` → `check_workflow_invariants` + `self_test_usage`)

A `WorkflowSpec` consumes a frozen env BY DIGEST and records a validated run. `seal_workflow` refuses to write on any of:

| ID | Invariant | Source of truth |
|----|-----------|-----------------|
| I0 | every top-level list-of-records holds only dicts (shape sanity) | structural check |
| I3 | every `pipeline_step` with rc=0 has validated `detected_outputs` and no validation record says `passed: False`. Existence + non-empty (`expected_type="any"`) IS a legitimate validation; the type-aware checkers are a bonus. **`mark_step_validated=passed` is a NARROW override**: it substitutes for *absent* per-file validation, never overrides `passed: False`, and refuses a step with no outputs | filesystem snapshot + `validate_output` |
| I4 | `usage.command_template` executes against **every declared trial** AND each produced file passes validation | `self_test_usage` — per-trial fresh scratch dir; sets `usage_verified`; seal refuses as `seal.usage_self_test_failed`. An empty `usage.outputs` is `not_attempted`, never `verified`. `command_template` is a `str` or a `list[str]` run in order in one working dir (read via `core_data.usage_commands()`). A multi-ENV chain cannot have a fully self-tested how-to (every command runs inside the ONE image `freeze_request_key` names) — it seals `degraded(seal.sealed_howto_unproven)`. Four states via `core_data.usage_status()`: `verified` · `failed` · `not_attempted` · `unrecorded`. The seal carries the transcript (`usage_verification.trials[]`, via `core_data.usage_proven_trials()`): the literal commands with every placeholder resolved; renderers never re-substitute `usage.trials[*].substitutions` themselves |
| I5 | every declared `reference_database` still exists, is non-empty, and hashes to what was recorded | sha256 + size refresh at seal; a `locus:cluster` DB is verified over ssh (existence + non-empty + sidecar hash). The refresh is fill-only: `available`/`size_bytes` are re-derived, `sha256` is an anchor written only when absent |
| I6 | every input/output path is absolute AND every `{PLACEHOLDER}` in `usage.command_template` is declared | absoluteness is enforced at construction (`PipelineStep._paths_are_absolute`); the walk emits only `I6.template_placeholders_declared` |
| I7 | every `pipeline_step` with rc=0 has `resource_usage` (wall, peak RSS, peak CPU) | presence is enforced at construction; the walk keeps `I7.resource_usage_captured`. Captured by psutil (host), GNU `time -v` (in-container), or sacct (cluster) |
| I8 | every `pipeline_step` input traces to a prior step's output OR a declared external source (test_data, reference_databases, runtime_configs, authored_artifacts), and every traced artifact still hashes to what was recorded | walk over prior outputs at seal, matched on the full path. Narrow tolerances: a sidecar (`{p}.bai`, `{p}.gz`) traces to its parent; an off-host step may match an external source by basename; a step may consume the exact directory a prior step wrote into. The universe includes `remote_outputs` (where an off-host step's outputs live at the locus that made them). `test_data` is anchored against the sha256 `select_test_data` recorded at selection; a block with no anchors is stated `unanchored`, never a pass |
| I10 | every declared `service_dependency` has at least one HEALTHY probe in its `health_check_log` | `start_service` / `verify_service_dependency` append probes; seal reads the log |

**Validated == shipped.** Once frozen, run the workflow's steps with `run_step_in_container` (not `run_pipeline_step`) so they execute INSIDE the shipped image; `seal_workflow` sets `validated_in_shipped_image` when every validated step's image digest matches the pinned env's. The `WorkflowSpec` carries its external input sources, so its I8 re-checks standalone.

**patch_pipeline** is restricted to agent-authored keys: `description`, `notes`, `final_summary`, `conda_env`, `created_at`, `python_version`, `reference_free`, `runtime_environment`, `runtime_configs`, `reference_databases`, `usage`, `accelerator`, `license_gated`, `licenses`, `redistributable`. Patches to runtime-captured or derived fields (`pipeline_steps`, `install_steps`, `packages`, `verifications`, `test_data`, `authored_artifacts`, `service_dependencies`) are **rejected** — those flow through their dedicated primitive so the runtime is the sole producer.

---

## Primitives — the only tools the agent needs

Compose these. The agent picks the right primitive; the primitive enforces its category's invariants internally. **The full contract of each primitive is its MCP tool description**, which you already have in context — signature, arguments, refusal modes, return shape. This table is a ROUTING INDEX: which primitive the job belongs to.

| Primitive | When |
|-----------|------|
| `agent_status` | **Where am I?** — the first call of a resumed session. Drafts in flight, frozen envs, sealed workflows, available data, which compute envs are reachable, which background jobs are alive. Query-only |
| `resolve_tool` | **Start here when unsure which tier.** Ranks the install tiers for one tool and returns the concrete `install_call` + rationale. Query-only |
| `list_installed_pipelines` | **What is ALREADY built here — ask BEFORE solving a tool again.** Frozen envs + sealed workflows. Compact by default; `detail=True` for digests. Query-only |
| `list_available_resources` | The same question for DATA — which genomes and test datasets are already on disk. Ask before downloading. Query-only |
| `show_pipeline_draft` | What the open draft actually holds right now. The draft is what `seal_workflow` will judge, so read it BEFORE sealing rather than learning its gaps from a refusal. Query-only |
| `interpret_request` | Turn a free-text ask into a typed `RequestIntent` and find out what is still MISSING before you start installing. Query-only |
| `plan_request` | Check a multi-step plan BEFORE running it — I8 lifted to authoring time, plus the topological order to walk. Query-only |
| `install_conda_packages` | Anything on bioconda / conda-forge / defaults. **Prefer this** — a clean conda package ships as a pre-built BioContainer that freeze ADOPTS by digest |
| `install_r_package` | CRAN / Bioconductor / `github:owner/repo`. Pass `functional_check` so validated means RAN, not merely imported |
| `install_pip_package` | pip / PyPI |
| `install_jar_tool` | Java tools — Exomiser, Picard, GATK, snpEff |
| `install_git_repo` | Clone-and-run repos and C/C++ tools built with make. **Pin `ref`** — a bare default branch drifts |
| `install_release_binary` | Precompiled static binaries on GitHub releases / vendor URLs. sha256 mismatch is a hard fail |
| `install_perl_package` | cpanm-only Perl modules. Prefer conda when the module is on bioconda |
| `install_cargo_tool` / `install_go_tool` | Rust / Go tools not on bioconda. Prefer conda when available |
| `synth_fetch` / `synth_build` | **The residual path — a tool with no packaged home at all.** Fetch the repo at an immutable anchor, READ its own build files, then record commands tagged `extracted` or `agent_authored`. Both refuse a repo whose manifest says it is a Nextflow/Snakemake **pipeline** — that is run by an engine, not installed |
| `freeze` | **Layer 1.** Build (or adopt by digest) the content-addressed, HPC-shippable env image; install and validate INSIDE the image that ships. Refuses on any honesty-contract violation. Writes the ENV report, attestation and build recipe |
| `freeze_from_image` | The authors' OWN published image, or one built from their Dockerfile — same honesty contract. Route here from `resolve_tool`'s `author_image` tier |
| `build_env_from_authors_recipe` | Clone a repo at a pinned ref, `docker build` its Dockerfile, then freeze that. Route here from the `authors_recipe` tier |
| `verify_env_recipe` | **Does the build recipe actually rebuild?** Reads `{name}.recipe.yaml` and re-derives the image from it alone. Says what it can prove for this build method; refuses rather than pretending |
| `seal_workflow` | **Layer 2 — the LAST step.** Validate the run-side invariants, self-test the how-to, pin the env by digest, write the `WorkflowSpec` + RUN dashboard |
| `generate_user_guide` | Opt-in Markdown export of a validated run. NOT the seal deliverable |
| `describe_sealed_step` | Typed read of ONE recorded step of a sealed workflow — its command, inputs, outputs and locus — so you can re-run it without re-deriving it from the yaml. Query-only |
| `render_pipeline` | **The PIPELINE layer — a sealed workflow over MANY samples.** Renders a sealed how-to as a directory under `<workspace>/pipelines/<name>/` that runs ONE way: Nextflow over `samples.csv` (one row per sample, one stage per how-to command, `-resume` re-runs only what changed) — `nextflow run … -profile local` on a laptop, `sbatch launcher.sh` on a cluster, the image per locus in `nextflow.config`'s profiles and nothing about where in `params.yaml`. `pipeline.html` shows it in the files' own vocabulary, with the command main.nf runs per stage. **Every run self-documents** under `runs/<stamp>/` (params.json as resolved, samples.csv as read, trace, report, timeline) through two standard blocks the renderer writes into every pipeline — `RUN_RECORD_CONFIG` in nextflow.config, `RUN_RECORD_BLOCK` at the top of the workflow. **Cohort stages** — `cohort=[{sealed_workflow, collect: {PLACEHOLDER: artifact}}]` attaches a second sealed workflow whose stages run ONCE over every sample after the per-sample stage they collect from (`.collect()`; the fan-in is DECLARED, never inferred); its `format: samplesheet` input receives the pipeline's own `samples.csv` (the trial sheet's extra columns join the samplesheet), and its authored scripts are carried into `bin/`. Invents nothing — every rendered command is checked against the sealed how-to before a file is written. Executes nothing; the directory is a template to copy next to the data |
| `download_reference_database` | Large external data (>100 MB). Set `compute_env` to make a compute node pull it straight onto the cluster |
| `acquire_reference_via_recipe` | **Use the authors' OWN gather script.** Runs it on a compute node, sha256-sidecars every file it produces, and records one cluster-locus `reference_database` entry so I5 can re-verify it at seal |
| `run_pipeline_step` | Run + auto-validate a step on the HOST env — pre-freeze iteration only |
| `run_step_in_container` | **Validated == shipped.** Run + auto-validate INSIDE the frozen image. Use this once frozen |
| `stage_authored_artifact` | Any file the agent wrote outside MCP. Without it the path is an orphan to I8 and seal REFUSES |
| `start_service` / `verify_service_dependency` / `stop_service` | Service-dependent tools (Redis, Postgres, Spark). Satisfies I10 |
| `add_core_test_data` / `add_core_pod5_data` | Pull a NEW sequencing dataset into the core manifest when nothing on disk fits — short-read/assay by accession, or nanopore pod5 signal. Both register what they fetched, so `select_test_data` can anchor it |
| `add_phenopacket` | Register a GA4GH phenopacket from a URL. Every field is read out of the JSON — nothing is supplied by hand. Feeds `phenopacket_to_vcf` |
| `phenopacket_to_vcf` | Materialize a single-sample VCF from a phenopacket |
| `snapshot_project` | **HPC bridge.** Read-only listing of a project's authorized dirs — a one-level overview, or (with `path=`) a RECURSIVE capped listing under any granted dir, `name_glob`-filterable, truncation stated with its remedy |
| `cluster_module_avail` | **HPC bridge.** Discover loadable Lmod modules so you pick a real `module load` line |
| `cluster_partitions` | **HPC bridge.** Discover SLURM partitions, which carry GPUs (with the card type), and which QoS each accepts — so `slurm.gpu: {partition, qos}` is READ off the cluster rather than typed. Returns `gpu_convention_candidates` (candidates, not a pick: A100 vs consumer card is a sizing judgement). Feed a chosen pair into a job's `slurm={partition, qos}` — it wins over the env's convention. Naming one is OPTIONAL: submitting with neither is valid and reports `gpu_placement: undeclared` |
| `upload` / `download` | **HPC bridge — the transfer surface.** Auto-routed by where the remote path falls (scratch / common_data / container_upload / reports / project_path). Blocks until the bytes are verified |
| `globus_task_status` | Resolve a Globus task's real end state. Reach for it after `transfer.globus_sync_wait_exceeded` — that means WE stopped waiting, not that Globus stopped |
| `cluster_job_status` | **HPC bridge.** SLURM state query. Read `verdict`, not the exit code — a scheduler-killed job reports rc=0 |
| `stage_apptainer_image` | **HPC bridge.** Deliver a frozen env to the cluster as a `.sif`. Idempotent. Never builds on the head node |
| `submit_workflow_job` | **HPC bridge — production submission.** Render + upload + sbatch, then return. Submit-and-document: no polling |
| `run_production_pipeline` | **The locus-agnostic production run — ONE verb, swap the env.** Dispatches on `env.type` (local docker / cluster nextflow+SLURM). Pass `sealed_workflow=` to pin the DATA, not just the env |
| `run_step_on_cluster` | **Cluster VALIDATION/seal.** Runs in the agent's scratch sandbox, polls, fetches, validates, records a cluster-locus step. Use a FRESH `workflow_name` per attempt |

Below the primitives there are still lower-level tools — use them when a primitive doesn't fit, prefer the primitive when it does. **Every low-level tool SAYS SO in its own description**, naming the primitive that supersedes it and when reaching for it directly is correct. Those notes are generated from `agent/skills/tool_surface.py`, which positions EVERY registered tool, and `tests/test_tool_surface.py` fails the build when a new tool is registered without a position, when a primitive is missing from the table above, or when a guardrail stops reaching the description the model is served.

### Two layers — environment vs. workflow

Two lifecycles. **Layer 1 — the environment** is solved *once*, frozen, and content-addressed: build it with the install primitives, then `freeze()` produces a digest-addressed, HPC-shippable artifact registered in the EnvCache (a later identical request returns it by hash, no re-solve). **Layer 2 — the workflow** *consumes* a frozen env by digest: `seal_workflow()` validates the run-side invariants, pins the env, and writes a `WorkflowSpec` + an HTML run dashboard rendered from the passing run. The env is the reusable "solved component"; workflows are the run-many, per-experiment artifacts on top of it.

**RECORDS are immutable; VIEWS are not.** The EnvCache entry, `{name}.recipe.yaml` and `{name}.attestation.json` are digest-pinned provenance and are never rewritten. `{name}.ENV.html` and `{name}.recipe.md` are VIEWS rendered purely from a record — when a renderer is corrected, run **`scripts/rerender_env_reports.py`** (`--check` to report only; `scripts/rerender_run_dashboards.py` is the Layer-2 twin) or the fix reaches zero pages a human opens. Re-freezing is NOT the substitute: a rebuild yields a new record with a new digest, discarding the artifact you were trying to correct.

---

## Protocol

The full flow, in order — two layers (env, then workflow):

1. **`start_pipeline(name, description)`** — returns `pipeline_id`. Thread it through every subsequent call.
2. **Compose install primitives** to build the env. Conda first; then R / pip / JAR / binary / source / cargo / go / perl; then `download_reference_database` for any large external data. Each primitive auto-merges its install_step into the draft.
3. **`select_test_data(...)`** — pick a dataset from `<resources>/core_test_data_hg38/manifest.yaml` (or generate one with `phenopacket_to_vcf` or a small script). It sha256-anchors every path it records so I8 can re-verify the inputs at seal, and it declares the matched dataset's core genome (`test_data.reference_fasta` + `.fai`), which is what makes an aligner's reference input traceable. The result states which of three things happened (`genome_reference.state`: recorded · declared-but-not-on-disk · none-declared).
4. **`run_pipeline_step(...)`** — run the tool against test inputs on the host for fast iteration. Every detected output is auto-validated.
5. **`patch_pipeline(pipeline_id, {usage: {...}, ...})`** — fill the fields no tool provides. Most important is **`usage`**: command_template with `{PLACEHOLDER}` slots, inputs[].format, outputs[].files globs. This is the contract `seal_workflow` self-tests against.
6. **`freeze(env, tools, pipeline_id=…)`** — **Layer 1.** Returns a `freeze_request_key`. Docker daemon required. **Pass `evidence={tool: command}` when you can write a command that RUNS the tool** — the adopt path's default is a conda-metadata presence probe, which proves the package is in the image and nothing about whether it works. Authored evidence is held to the same two guards as `freeze_from_image`'s (shape rule + the control-image experiment).
7. **`run_step_in_container(freeze_request_key, …)`** — re-run the workflow's steps INSIDE the frozen image so the recorded run is the one that ships (validated == shipped; in-container `resource_usage`).
8. **`seal_workflow(pipeline_id, freeze_request_key)`** — **Layer 2.** Validates the run-side invariants, self-tests `usage.command_template` (I4), pins the env BY DIGEST, writes the `WorkflowSpec` + RUN dashboard. **This is the last step** — the sealed spec IS the record of the run.

**Three ways in, and which one is canonical is NOT settled.** The eight steps above are the protocol itself. `interpret_request` → `plan_request` is the typed front door. `install_pipeline_brief(name)` hands back the invariants and primitives for one tool as a structured brief a subagent can execute unsupervised. All three reach the same primitives; they differ in how much is decided before the first install runs.

To execute the *same* frozen env ON HPC, the Phase 2 bridge consumes the `freeze_request_key` directly: `run_step_on_cluster` for the validate-then-seal flow; `stage_apptainer_image` → `submit_workflow_job` → `cluster_job_status` → `download` for production runs. See **HPC bridge — Phase 2**.

---

## Async pattern — for anything that may run silently >5 minutes

The agent's stream-watchdog kills a tool call that goes silent for ~600s. Six primitives can legitimately run past it and carry **`background=True`** — `freeze`, `freeze_from_image`, `build_env_from_authors_recipe`, `install_conda_packages`, `run_step_in_container`, `seal_workflow`. Pass it and the call returns a `job_id` immediately; `check_job` then carries the tool's **real return value inline under `result`** once `state=='exited'`. Prefer the flag over hand-rolling the same thing with `run_in_background`, which is for arbitrary shell (`download_reference_database` backgrounds itself internally).

```python
job = freeze(env_name="x", tools=["dorado"], background=True)
while (r := check_job(job["job_id"], wait_s=300))["state"] == "running":
    pass                      # each call blocks up to wait_s; start other jobs between calls
r["result"]                   # the freeze record itself, or result_missing saying why not
```

**Never end your turn to wait for a job.** A headless session ends with the turn, so a job left for "later" is never read back. Wait inside `check_job(wait_s=…)` (cap 540 s, under the watchdog) and keep calling until `state` is `exited`.

Parallel background installs into ONE conda prefix are serialized (different envs still run fully in parallel), so a batch can fan out freely.

Resuming a session and don't know what you left running? `check_job` needs a `job_id` you already hold; **`list_jobs`** answers the prior question — everything ever backgrounded on this machine, newest first, `include_terminated=False` for just the live ones. `agent_status` reports the same jobs alongside drafts, envs and reachable compute.

---

## Data on disk

**THE SYSTEM AND ITS ARTIFACTS LIVE APART.** The CHECKOUT is the system, self-contained: code plus everything setup rebuilds into it untracked — `.miniforge/`, `.conda_runtime/`, the host tool envs (`envs/`), the core test-data corpus (`resources/`). Delete the clone and the system is gone; re-run `./scripts/setup.sh` on a fresh clone and it builds itself back. What the agent PRODUCES outlives any clone, so it lives in a WORKSPACE outside — `~/bioinf_workspace` default, `$BIOINF_WORKSPACE` overrides. All of it is resolved by `agent/skills/workspace.py` and by nothing else: ask it; do not derive a path from `__file__`. `agent_status` reports every resolved zone, `./scripts/setup.sh --check` prints them, and `tests/test_workspace_resolution.py` fails the build on a second answer. (`projects_access.yaml` lives in NEITHER — its home is fixed at `~/.bioinf_agent/`; see the HPC bridge section.)

| zone | where | accessor | holds | may I delete it? |
|------|-------|----------|-------|------------------|
| `envs/` | checkout | `conda_envs_dir()` | host conda tool envs (pre-freeze iteration) | yes — rebuild from the recipe |
| `resources/` | checkout | `resources_root()` | reference genomes + test datasets; relocatable via `$BIOINF_RESOURCES` (shared mount) | yes, reluctantly — setup refetches, but it is multi-GB |
| `environments/` | workspace | `environments_dir()`, `env_dir(name)`, `env_cache_path()`, `sealed_workflow_path(name)` | **one directory per frozen env** — `{name}.tar` (the `docker save` tarball, once it ships) beside **the record**: `{name}.ENV.html`, `{name}.attestation.json`, both recipe forms, and every workflow sealed on the env (`{workflow}.workflow.yaml` + `{workflow}.RUN.html`); the EnvCache (`_env_cache.json`) at the top | the tarball yes (rebuild from the recipe); the rest **never** — it IS the deliverable |
| `pipelines/` | workspace | `pipelines_dir()` | **rendered pipeline templates** — one directory per sealed workflow (`render_pipeline`): main.nf, nextflow.config, params.yaml, samples.csv, launcher.sh, pipeline.html and the `.pipeline/` record it was rendered from; copy the files into the directory where the data is to run | yes — re-render from the sealed workflow |
| `scratch/` | workspace | `scratch_dir(…)` | job state, drafts, render staging, install logs, the bridge's transfer receipts and submission manifests | freely |
| `experiments/` | workspace | `experiments_dir()` | headless runs of the agent (`scripts/experiments.py`): per run, the transcript plus the isolated workspace it produced, and the metrics report | freely |

Core test data lives at `<resources>/core_test_data_hg38/` (8 read datasets + ACTB phenopacket + chr22 reference). Read `manifest.yaml` to enumerate. Pipeline-specific test data goes in `<resources>/{pipeline_name}_test_data/`. Every dataset's licence and credit is in [docs/data_sources.md](docs/data_sources.md) and on its entry in `config/core_datasets.yaml` (`license` / `citation`, lint-enforced); the pod5 seed is the one dataset committed to the repo, derived from CC0 HPRC data by `scripts/derive_pod5_seed.py` because every Oxford Nanopore-published pod5 is non-commercial.

Generated artifacts:
- `<checkout>/envs/bioinf_{name}/` — the host conda env (pre-freeze iteration)
- **Layer 1 (`freeze`)** — the env image in the local Docker daemon + its EnvCache record; the Apptainer delivery (registry-free `docker save` tarball at `<workspace>/environments/{name}/{name}.tar` → `apptainer build docker-archive`, or a registry push); and record-rendered deliverables in the same directory: `{name}.ENV.html` + `{name}.attestation.json` (in-toto/SLSA provenance) + `{name}.recipe.yaml` (machine build recipe, self-contained) + `{name}.recipe.md` (the runnable rebuild commands). The two recipe forms are written for EVERY install path, so an env is always reproducible
- **Layer 2 (`seal_workflow`)** — `<workspace>/environments/{env}/{name}.workflow.yaml` (the `WorkflowSpec`, filed under the env it pins) + `{name}.RUN.html` (the run dashboard: validated evidence per compute locus, the how-to panel carrying the I4 transcript, every external input source I8 traced, and the runtime prerequisites I10 gated on). **The page must SHOW what the seal GATED ON** — a field the invariants read and no renderer prints leaves the reader the yaml as their only recourse; `tests/integration/correctness/test_run_dashboard_shows_what_was_gated.py` is the standing form of the rule. Its second form: never print a field while dropping the record's own qualifier on it — read `resource_usage` authority and output types through the `core_data` leaves (`resource_usage_authority`, `usage_output_type`), which are three-state, never a bare bool

---

## Schema cheatsheet (avoid seal rejection)

**THE GATE IS THE GUIDE** — knowledge about satisfying a check lives AT the check, so you pay for it only when you trip it. Enums, missing seal-required fields, undetected step outputs and mutated `test_data` all name their own remedy when they fire. What's here is only what a gate cannot teach in time:

- **`usage` is validated when you patch it**: `{description, command_template, inputs: [{name, format?, description?}], outputs: [{name, files: [pattern…], description?}], trials: […]}` — `description` and every `outputs[*].name` are required.
- `usage.trials[*]`: `{name, substitutions: {PLACEHOLDER: abs_path}, description?}` — declare one trial per input shape (paired-gz, single-uncompressed, …) so I4 proves multi-shape coverage. Empty list ⇒ single inferred trial.
- **`usage.command_template`: `str | list[str]`.** Use a LIST for a multi-phase how-to — one command per entry, run in order in one shared working dir. Every consumer reads it through `core_data.usage_commands()`.
- **A thread count is a slot, not a literal.** Write the tool's thread flag as `-p {THREADS}` with an input `{name: THREADS, format: threads}` and bind one count in every trial. `render_pipeline` binds it to the stage's CPU request (`task.cpus`), the sealed count by default, so the command and the SLURM request cannot disagree; a literal `-p 4` renders as 4 threads on a 1-cpu request. Two sibling slots for a COHORT how-to (one that runs once over every sample, attached with `render_pipeline(cohort=)`): an input `format: samplesheet` is the pipeline's own `samples.csv` (bind the trial to a sheet with a `sample` column plus the columns the script reads), and the fan-in input (a directory of every sample's file) is declared at render with `collect={PLACEHOLDER: "{SAMPLE}.counts.tsv"}`, so name per-sample outputs after the sample. A script the how-to runs is a `stage_authored_artifact` (content mode) bound as a plain path input: the render carries it into `bin/`.
- **One reading per field.** A new consumer MUST use the `core_data` leaves rather than re-spell the logic (`tests/test_one_reading_per_field.py` enforces it): `usage_commands()`, `usage_status()`/`usage_label()`, `record_is_gated()`, `test_data_paths()`/`test_data_anchors()`/`resolve_data_path()`, `step_is_validated()`, `usage_proven_trials()`, `service_probe_log()`/`service_healthy_probes()`/`probe_is_healthy()`. Two readings of one field is how drift starts. `reference_databases[*].source_url` is optional; I5 pins content by sha256, not URL.
- **Output placeholders in `usage.command_template`**: write every output path through an OUTPUT slot — one named `{OUTPUT_DIR}`/`{OUT_DIR}` or containing `output`. The I4 self-test runs each trial in a fresh scratch dir and fills THAT path into output slots, then scans it for `usage.outputs[*].files`. An output written via an unrecognized slot (e.g. `-o {OUT_TSV}`) lands outside the scratch dir → I4 fails with `produced_files: []`. Correct idiom: `-o {OUTPUT_DIR}/stats.tsv`.
- **`run_pipeline_step` output detection**: the step only detects files created/modified under `watch_dir` (default: the input's directory). If your command writes elsewhere via `-o <path>`/`> <path>`, pass `watch_dir=<that dir>` — an undetected output has no validation and fails I3 at seal.

---

## Configuration

`config/agent_config.yaml` — conda channels, default Python, the install timeout. **NOT paths** (resolved by `agent/skills/workspace.py`, not configured) and **NOT the Docker base image** (`BASE_IMAGE` in `agent/skills/container_build.py`, pinned by digest and deliberately not configurable — it is an input to the content digest). Every key in the file has a reader.
`config/core_datasets.yaml` — the bootstrapped test-data corpus (read datasets + phenopackets + the pod5 seed), each entry with `license`/`citation`.
`.mcp.json` — MCP server registration. The launcher runs the server on `./.conda_runtime/bin/python`. Set `BIOINF_MCP_AUTO_RELOAD=1` (default in this repo) so the server hot-reloads on code changes.

---

## HPC bridge — Phase 2

Layer 1 produces an HPC-shippable container; the bridge drives real jobs on a real cluster — push inputs, stage the container, sbatch a Nextflow workflow, poll, pull outputs back. Every primitive is gated by `projects_access.yaml`, every transfer is sha256-round-tripped, every shell line passes a safe-token validator BEFORE any ssh.

### Two walls, two operations

- **Scratch — the agent's sandbox.** Cluster validation/seal runs live here: `<env.agent_scratch_target.path>/<project>/<workflow_name>/`. Env-implicit grant, project-prefix isolation. Validation jobs are short and bounded, so `run_step_on_cluster` polls to completion synchronously.
- **`directories[]` — the user's territory.** Production runs live here, under explicit per-directory grants. Production jobs run hours-to-days, so `submit_workflow_job` is **submit-and-document**: it returns the `job_id`, writes a manifest to `<reports>/job_submissions/<project>/<workflow_name>_<job_id>.submission.json`, and the user (or a future agent invocation) follows up via `cluster_job_status` + `download`.

The two operations share the render+sbatch machinery but each owns its auth surface — scratch via `check_env_target_capability`, project_path via `check_permission` against `directories[]`. The walls don't get crossed inside one primitive.

### The command-and-control file: `projects_access.yaml`

A single user-authored YAML at the FIXED machine-level home **`~/.bioinf_agent/projects_access.yaml`** — it describes a compute world that belongs to the MACHINE, not to any clone, and it holds real hostnames and usernames. `compute_access.default_access_path()` is the one answer to where it lives (`$BIOINF_PROJECTS_ACCESS` overrides — the test seam); the config menu writes there and every reader looks there. Two top-level sections:

- **`compute_envs[]`** — one block per environment. Each has `type: ssh|local`, ssh `host`/`user`, and optional target blocks: `agent_scratch_target`, `agent_common_data_target`, `container_upload_target`, `agent_pipelines_target`, plus `slurm`, `data_transfer`, and — for ssh+apptainer envs — `apptainer_module` / `nextflow_module` (the Lmod names a cluster production run loads). A **local** env is at zone-parity with the cluster: it declares the same zones with local paths, which is what lets a production run be the same kind of thing on either locus.
  - The `slurm` block (closed-key) is the cluster's SCHEDULER POLICY, merged into every job header: `account`, `partition` (the CPU default), and `gpu: {partition, qos}` (the standing GPU convention — **fillable, never required**: the job's own `slurm` wins each slot, the env fills what is left, and what resolved is REPORTED as `gpu_placement` — `not_applicable` · `declared` · `partially_declared` · `undeclared` — on the return, the submission manifest, the recorded step, and the rendered launcher. `env.slurm.partition` is the CPU default and never fills a GPU job's slot). Per-job SIZING — `time`/`mem`/`cpus`/`gpus` — is the caller's `slurm` argument, not policy.
  - The `data_transfer` block (closed-key) picks the wire protocol: `scp_head_node` (default) or `globus` (nested block carries `local_endpoint_id` + `remote_endpoint_id`).
- **`projects[]`** — one block per logical project, FLAT schema: `compute_envs: [names]` (which envs this project may use — this also carries the implicit scratch/common_data grant) + `directories: [{env, path, permissions, description}]` (the explicit user-territory grants; permission tokens `file_name_only`, `upload`, `download`, `exec`).

Auth is **discrete**, not a lattice: `upload` ≠ `download` ≠ `exec`. A dir declared `[upload]` does NOT implicitly grant `download`. Mismatches raise `PermissionDenied` BEFORE any ssh.

### Five transfer zones, routed by path

TWO primitives — `upload` / `download` — and the ZONE is decided by where `remote_abs_path` falls on that env (`transfer._classify_zone_and_authorize`). Every path is ABSOLUTE. The four env-implicit zones are granted by the env block itself; only `project_path` consults the project's `directories[]`.

| Zone | Under | Auth chain |
|------|-------|------------|
| **scratch** | `env.agent_scratch_target.path` | env-implicit; **path must be under `<scratch>/<project_name>/`** — the one zone with project isolation |
| **common_data** | `env.agent_common_data_target.path` | env-implicit; no project prefix. **Reference data only** |
| **container_upload** | `env.container_upload_target.path` | env-implicit; no project prefix (a `.sif` name is content-addressed by image digest, so collisions are impossible). Deliberately NOT common_data |
| **pipelines** | `env.agent_pipelines_target.path` | env-implicit; no project prefix (a pipeline directory is named for the pipeline). Where a rendered pipeline is copied to run on the env and where its run records are read back from. `upload`+`download`+`exec` |
| **project_path** | anywhere else | explicit: `directories[]` longest-prefix-match contains the path AND carries the right permission token |

A path matching no zone is refused with `PermissionDenied` — and because `_ad_hoc` carries an empty `directories[]`, that is exactly what bounds it to the env-implicit zones. `tests/integration/honesty/L14_compute_env_safety/test_transfer_surface.py` pins each zone.

### Wire protocol — scp_head_node vs globus

- **`scp_head_node`** (default) — scp + ssh sha256sum round-trip. Fine for small files; rude to the head node for GB-scale .sif images.
- **`globus`** — every `upload` / `download` + `stage_apptainer_image`'s `.tar` transfer goes through the Globus CLI. Off-head-node, checksum-verified by Globus itself. Requires `globus login` + Globus Connect Personal locally; both endpoint UUIDs in the env's `data_transfer.globus` block. Hard-errors on Globus failure — never silent fallback to scp. First-time setup: `globus endpoint search "<display name>"` → `globus gcs collection show <UUID>` (errors name the exact `globus login --gcs <GCS_UUID>` to run) → add local folders in Globus Connect Personal's Access tab. A successful read-only `globus ls` does NOT prove transfers will work (no `data_access` consent needed for ls); verify with a small test transfer.

**The sync-wait ceiling.** Transfers BLOCK until verified, but the wait is capped at `_SYNC_WAIT_S_DEFAULT = 540s` — just under the stream-watchdog, so the call returns a record instead of being killed. At ~9.4 MB/s measured against a real cluster that is roughly **5 GB**. Past it you get `transfer.globus_sync_wait_exceeded` **with the task_id**. That is not a failure — *we* stopped watching, not Globus. Resolve with `globus_task_status(project, env, task_id)` (SUCCEEDED → proven · FAILED → broke · ACTIVE → poll again). **Do not re-run the transfer blind** — a second copy of the same bytes races the first.

### The validation chain (scratch — short, synchronous, seal-ready)

Prove a frozen env works on the cluster — the bytes the user runs on HPC are the bytes we validated:

1. **`freeze(env, tools)`** — Layer 1.
2. **`start_pipeline(name, description)`** — opens a draft.
3. **`run_step_on_cluster(pipeline_id, freeze_request_key, project, env, workflow_name, …)`** — stages the .sif (idempotent), renders+uploads main.nf/nextflow.config/launcher.sh to `<scratch>/<project>/<workflow_name>/`, sbatches, polls to completion, fetches outputs back, validates each, records a cluster-locus `pipeline_step`. Workflow_dir is computed internally — no caller knob.
4. **`patch_pipeline(pipeline_id, {usage: …})`** — fill the seal-required fields the runtime can't capture.
5. **`seal_workflow(pipeline_id, freeze_request_key)`** — Layer 2.

### The production chain (`directories[]` — long-running, submit-and-document)

**The locus-agnostic front door is `run_production_pipeline(project, env, …)`** — one verb that runs the frozen env's workflow in production on WHICHEVER env you name, dispatching on `env.type`. For a **local** env it is the whole chain (render `run.sh` → background `docker run` → poll `check_job`). The steps below are the CLUSTER mechanism its ssh branch delegates to:

1. **`stage_apptainer_image(project, env, freeze_request_key)`** — idempotent .sif delivery.
2. **`upload(project, env, local_path, remote_abs_path)`** (×N) — push input data into the project workspace.
3. **`submit_workflow_job(project, env, workflow_dir, workflow_name, …)`** — renders + uploads + sbatches, returns `job_id`, writes the submission manifest. No polling.
4. **`cluster_job_status(project, env, job_id)`** — query whenever asked. sacct-backed; read `verdict`.
5. **`download(project, env, remote_abs_path, local_path)`** — pull outputs back; sha256 round-trip (or Globus end-to-end).

Pre-submission exploration: `snapshot_project` (read-only recursive capped listings, `name_glob`-filterable) and `cluster_module_avail` (pick a real `module load X/Y.Z` line).

### The renderer's contract

Every workflow `workflow_render` produces is **human-readable + locally re-runnable**:

- Inputs/outputs flow through top-level `params.x = '<value>'` declarations (dot notation — Nextflow rejects the `params { }` block form).
- Each process's `script:` block holds the LITERAL shell command with `${params.x}` substituted — no DSL magic. A human reads main.nf, copy-pastes the line, re-runs the step from a shell.
- Tools come from a frozen apptainer .sif: the process invokes `apptainer exec <sif> <cmd>`, so swapping versions is a one-line path change.
- launcher.sh sets `NXF_HOME=$PWD/.nextflow_home` and cd's via `${SLURM_SUBMIT_DIR:-…}` — HOME is not writable from compute nodes, and SLURM stages `$0` into `/var/spool/slurmd/`.
- **`slurm.gpus > 0` ⇒ `apptainer exec --nv`** (binds the driver userspace in; without it the tool sees `/dev/nvidia0` but cannot load the driver, so it silently falls back to CPU or dies in `dlopen`). Gated on `gpus`, NOT on placement — whether a device was allocated and where the job landed are different questions. `--nv` on a node with no NVIDIA driver is an error, hence the gate.
- **`--partition` / `--qos` are emitted on PRESENCE** — a GPU job may legitimately have neither (the scheduler places gres requests itself on some clusters). When placement is `undeclared` or `partially_declared` the launcher carries a one-line `# GPU placement:` note, because the launcher is the artifact a human re-runs.

### ssh ControlMaster pattern (no password prompts)

The user opens `ssh hpc-agent` in a separate terminal and leaves it open. Every bridge primitive uses ssh BatchMode and piggybacks on the ControlMaster socket. The agent never sees a password or key.

---

## Tests

`pytest tests/` — covers both layers: `env_honesty.check_build` for the env, `check_workflow_invariants` (roster: `agent/skills/invariants.py`) + the usage self-test for the workflow. Sanity tests verify the gates themselves catch silent-empty-success steps, relative paths, undeclared placeholders, and orphan step inputs. The suite runs on `./.conda_runtime/bin/python`.

### The two maps

The suite tells you the code does what it says. These tell you **what the system IS**, from opposite ends:

| | question | source | page |
|---|---|---|---|
| **outcomes dashboard** | what can the code EMIT, and has it ever run? | AST sweep → `docs/outcomes_ledger.json` + real coverage | `docs/outcomes_dashboard.html` |
| **intent grid** | what can a user MEAN, and does it reach that? | live-probed → `docs/intent_corpus.json` | `docs/intent_grid.html` |

Neither substitutes for the other: an intent that reaches no terminal isn't a dark cell — it isn't a cell; and a terminal can be green with the wrong tool in it (a name collision resolving to the wrong project). The resolver's duty there is disclosure — `SAME NAME, DIFFERENT PROJECTS` blocks with the `github_repo=` to re-run with — and judging the entry's own words remains the reader's call.

    ./scripts/refresh_meters.sh   # output side: ledger + coverage overlay + dashboard (--fast skips the re-measure)
    pytest -m live && python scripts/build_intent_corpus.py && python scripts/render_intent_grid.py   # input side

**The meters are DERIVED, and derived files are never line-merged.** `outcomes_ledger.json` and `terminal_coverage.json` are tracked RECORDS; the dashboard HTML is an untracked VIEW. On any merge conflict in the two records: take EITHER side, run `./scripts/refresh_meters.sh` on the merged tree, commit what it writes.

**The intent corpus** (`tests/live/test_intent_corpus.py`) is a **ratchet, not a green suite**: each row is one real user intent with the outcome it deserves; a change detector fires on behaviour drift, and a correctness ratchet (`xfail(strict=True)`) fails-on-XPASS so a fix must be PROMOTED. Its rules:

- **Live, never mocked, and opt-in** (`-m live`) — a mocked corpus asserts only that our fixtures agree with our fixtures. Its pure integrity tests DO run on every push.
- **Assert on the DECISION, never on volatile registry state** — a corpus that goes red because a maintainer cut a release is one people learn to ignore.
- **Judgement vs observation** — `expect` is reviewed and never auto-written; only `actual_today` is re-probed.

It declares its own blind spots (`known_gaps`, rendered above the table, and tested). **FOUR row states, only two of them verdicts**: `ok`, `measured wrong`, `deferred`, and `the harness cannot check` (`expect.assertable: false`). The last two carry `is_correct_today: null` and count in NEITHER the numerator nor the denominator — `False` is a claim, `null` is the truth that nobody measured, and a meter that cannot tell them apart is not trustworthy about the rows it can grade.

### Measuring the agent

`scripts/experiments.py run experiments/<name>.yaml` puts one prompt to the agent in a fresh headless session (`claude -p` with the server attached, an empty workspace per run, the Docker images the run added removed after it) and writes one row per run — cost, tokens, tool calls, MCP outcomes by class, the artifact ladder — plus a report with pass@1, pass^k and cost-of-pass, and the same experiment across code revisions. The definitions in `experiments/` are a scenario corpus, one probe model, varied by scenario and by code revision: a run is judged by a rule (`completed` · `sealed` · `pipeline` · `refused` with expected codes · `asked`) and the report's catalogue of what tripped the agent is the deliverable. A definition may declare a compute world inside the run (`projects_access: access/local_run.yaml`). The runs land in the `experiments/` zone and are never tracked.
