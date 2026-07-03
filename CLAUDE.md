# Bioinformatics Install Agent

Installs bioinformatics tools into isolated conda envs, validates them against test data, packages as HPC Docker images, and emits a machine-verified spec. Designed to be **a solved component** — call once per tool/version, get a trustworthy artifact, never look at it again.

```bash
pip install -r requirements.txt
./scripts/setup_core_test_data.sh   # one-time: core_tools env + chr22 + 8 read datasets + ACTB phenopacket
```

Then drive via Claude Code MCP, or any agent that speaks our tool surface.

---

## The honesty contract

Two machine-verified layers — nothing is taken on faith from the agent.

### Layer 1 — the env image (`freeze` → `env_honesty.check_build`)

An env is **built and validated INSIDE the image it ships**, so install==ship is ONE event. That collapses the old per-tier env-build invariants (the retired host writer's I1/I2/I5/I9/I10/I11/I12/I13/I14 — which had to *re-anchor*: re-hash binaries, re-clone source, re-hash authored files, because install and ship were two events that could drift) into **three structural guarantees**. `freeze` refuses to register an env on any violation:

| Guarantee | Means | Source of truth |
|-----------|-------|-----------------|
| **BUILT** | the env image + `image_digest` resolve in the local Docker daemon | `docker image inspect` |
| **VALIDATED_IN_IMAGE** | every tool's evidence command re-runs green INSIDE the shipped image AND references the tool as a word-boundary token (echo / print / `true` / `:` cheats rejected; perl `-M` + conda-prefix-strip handled) | `run_in_container` + `env_honesty.evidence_shape_violation`. Because the bytes validated ARE the bytes that run on HPC, this subsumes the old I1/I2/I5/I9/I10/I11/I14 (presence, verify, ref-DB, authored-hash, service, source-commit, binary-sha) — no host re-anchoring needed |
| **POLICY_CLEAN** | I12 accelerator honesty (`cuda`/`rocm` need `toolkit_version`; `runtime: runtime_verified` needs a captured `runtime_probe` + `min_driver_version`; `mps` must be `dev_only`) + I13 license firewall (gated ⇒ `redistributable: false` AND `licenses[]`) | `env_honesty._check_accelerator` / `_check_license` — the procedural firewall against republishing a gated artifact |

### Layer 2 — the workflow run (`seal_workflow` → `check_workflow_invariants` + `self_test_usage`)

A `WorkflowSpec` consumes a frozen env BY DIGEST and records a validated run. `seal_workflow` refuses to write on any of:

| ID | Invariant | Source of truth |
|----|-----------|-----------------|
| I0 | every top-level list-of-records holds only dicts (shape sanity) | structural check |
| I3 | every `pipeline_step` with rc=0 has validated `detected_outputs` AND no validation uses `expected_type="any"` | filesystem snapshot + type-aware `validate_output` (samtools / bcftools / json.loads / …) |
| I4 | `usage.command_template` executes against **every declared trial** AND each produced file passes type-aware validation | `self_test_usage` — per-trial fresh scratch dir + `OutputValidator`; sets `usage_verified` |
| I6 | every input/output path is absolute AND every `{PLACEHOLDER}` in `usage.command_template` is declared | string check + template scan |
| I7 | every `pipeline_step` with rc=0 has `resource_usage` (wall, peak RSS, peak CPU) | psutil monitor (host `run_pipeline_step`) OR GNU `time -v` in-container (`run_step_in_container`) |
| I8 | every `pipeline_step` input traces to a prior step's output OR an external source (test_data, reference_databases, runtime_configs, **authored_artifacts**) | universe-of-prior-outputs walk at seal |

**Validated == shipped.** Once frozen, run the workflow's steps with `run_step_in_container` (not `run_pipeline_step`) so they execute INSIDE the shipped image; `seal_workflow` then sets `validated_in_shipped_image` when every validated step's image digest matches the pinned env's. The `WorkflowSpec` carries its external input sources, so its I8 re-checks standalone — it doesn't depend on the draft it was sealed from.

**patch_pipeline** is restricted to agent-authored keys: `description`, `notes`, `final_summary`, `conda_env`, `created_at`, `python_version`, `reference_free`, `runtime_environment`, `runtime_configs`, `reference_databases`, `usage`, `accelerator`, `license_gated`, `licenses`, `redistributable`. Patches to `pipeline_steps`, `install_steps`, `packages`, `verifications`, `test_data`, `authored_artifacts`, `service_dependencies` (or any runtime-captured / derived field) are **rejected** — those flow through their dedicated primitive so the runtime is the sole producer. The few free-form fields (`description`, `notes`) are clearly LLM-authored and explicitly *not* part of the verified surface.

---

## Primitives — the only tools the agent needs

Compose these. Each one absorbs the per-category knowledge that would otherwise live in prose here. The agent picks the right primitive; the primitive enforces its category's invariants internally.

| Primitive | When |
|-----------|------|
| `resolve_tool(tool, version?, github_repo?, prefer?, language?)` | **Start here when unsure which tier.** Probes conda/PyPI/CRAN (+release-binary/source if `github_repo` given), ranks (conda > pip/cran/bioc > binary > source > manual), returns the concrete `install_call` + rationale + rejected alternatives. Query-only. **Cross-registry name collisions** (a PyPI `ape` ≠ CRAN's R `ape`) come back `ambiguous: true` — pass `language='python'\|'r'` to restrict the ecosystem (or `prefer` to force a tier) |
| `install_conda_packages(env, [{spec, channel}], pipeline_id)` | Anything on bioconda / conda-forge / defaults |
| `install_r_package(env, name, source, pipeline_id, deps_first?, functional_check?)` | source = `cran` \| `bioconductor` \| `github:owner/repo` — handles library isolation, BiocManager bootstrap (mirror-pinned), requireNamespace load-or-die. `deps_first` pre-installs undeclared transitive deps (the `missing_packages` retry surface drives this). **`functional_check`** = a SELF-CONTAINED R expr that RUNS the package on inline-generated data and `stop()`s if it doesn't work — becomes the freeze `VALIDATED_IN_IMAGE` evidence so *validated == ran*, not merely imported (import ≠ works: a pkg can `library()` an undeclared dep in a rarely-hit path, install+import clean, fail at runtime) |
| `install_pip_package(env, name, version?, pipeline_id)` | pip / PyPI — handles import check |
| `install_jar_tool(env, tool, jar_url, java_flags, pipeline_id)` | Java tools (Exomiser, Picard, GATK, snpEff) — auto-downloads, unpacks, writes wrapper, sets install_method.type=jar |
| `install_git_repo(env, repo_url, tool_name, ref?, build_command?, verify_command?, bin_path?, pipeline_id)` | Clone-and-run repos that aren't conda/pip/jar packages (academic script collections; C/C++ tools built with make). Clones into `{env}/share/{tool}`, pins the commit SHA, optional build + smoke verify. `bin_path` (relative path to the built executable, e.g. `seqtk`) writes a PATH wrapper AND is recorded with `build_command` so **freeze can rebuild the tool in the ship image** (source replay); omit for run-by-path scripts. Sets install_method.type=source; anchored by I11. **Pin `ref` to a tag/commit** — a bare default branch drifts |
| `install_release_binary(env, tool_name, url, sha256?, binary_in_archive?, wrapper_name?, verify_command?, pipeline_id)` | Precompiled static binaries on GitHub releases / vendor URLs (mosdepth, somalier, sylph, dorado, cellranger). Download → sha256-anchor (mismatch = hard fail) → extract → chmod → PATH wrapper. A smoke verify catches a wrong-arch binary. Sets install_method.type=binary; anchored by I14 |
| `install_perl_package(env, module, distribution?, cpanm_flags?, build_env?, pipeline_id)` | Perl/CPAN modules (Ensembl VEP plugins, niche CPAN) via cpanm. Requires `perl` + `perl-app-cpanminus` in the env. Verifies `perl -M{module} -e1`. `build_env` = `KEY=VAL` exports for XS builds linking a conda C lib (e.g. `HTSLIB_DIR=$CONDA_PREFIX`) — use `$CONDA_PREFIX` so freeze's replay resolves it in the ship image. Sets install_method.type=perl; freeze replays the cpanm build. **Prefer conda when the module is on bioconda** (e.g. `perl-bio-db-hts`, `perl-bioperl` — BioPerl-class mega-deps install far more reliably via conda); this tier is for the cpanm-only modules |
| `install_cargo_tool(env, crate, version?, binary_name?, git_url?, pipeline_id)` / `install_go_tool(env, package, version?, binary_name?, pipeline_id)` | Rust / Go tools not on bioconda. `cargo install --root {env}` / `GOBIN={env}/bin go install`. cli_which is the anchor (locally built ⇒ can't be wrong-arch). **Captures the host `rustc`/`go` version** into install_method so freeze can replay the build with the identical toolchain on the ship platform (reproducible cross-arch). **Prefer conda when available** (many are on bioconda); these are the fallback |
| `freeze(env, tools, pipeline_name?, platform?, accel?, gated?, licenses?, push_target?, pipeline_id)` | Freeze the env into a content-addressed, HPC-shippable artifact. request_key→EnvCache lookup (hit = proven artifact by hash, no re-solve). **ADOPT-or-BUILD**: **pure conda + a published BioContainer → ADOPT by digest** (no build); **everything else → CONTAINER-NATIVE BUILD** via `env_freeze.build_env_image` — install **and validate INSIDE the ship-platform image** (one generic bake; *validated == shipped*), cross-arch included (built in-container; no host-arch conda-pack, no cross-arch refusal). Covers every tier — conda · pip (engine `--pypi`, in-lock) · cran/bioconductor (Rscript) · binary (linux asset re-fetched + sha256) · jar (download + JRE + `java -jar` wrapper) · source (clone at the pinned commit + rebuild) · cargo/go (engine toolchain) · perl (cpanm; **XS compiles against the conda perl** via conda c/cxx-compiler + an xlocale.h shim) · half-baked (authored files baked). The build is **multi-stage**: a builder with the full toolchain → a slim runtime that COPYs only the env (identical paths) + artifacts + the `.so` runtime libs (build-essential never ships). Refuses on any honesty-contract violation (`env_honesty.check_build`: BUILT · VALIDATED_IN_IMAGE · POLICY_CLEAN), incl. I13 — a **gated** build must pass `licenses`. A biocontainer is NOT adopted when the env has non-conda installs it can't represent. Records `shipped_binaries[]` (the baked long-tail command = its provenance) + `build_method`, the **full SBOM** (`resolved_packages` conda/pip closure + `system_packages` apt/OS layer, all versioned, read from the shipped image) + **`validation_locus`** (native/emulated — I7 timings authoritative only when native). Writes two **Layer-1 deliverables rendered PURELY from the verified record** (can't be faked): `env_reports/{name}.ENV.html` (the env report — REQUESTED tools with in-image validation evidence vs along-for-the-ride deps; cyberpunk dark theme, deterministic, escaped) + `env_reports/{name}.attestation.json` (in-toto/SLSA provenance; sign with `cosign attest`). The base image is **pinned by digest** (`container_build.BASE_IMAGE`) for OS-layer reproducibility. Emits the Apptainer HPC delivery contract: registry-free `docker save`→`apptainer build docker-archive` by DEFAULT, OR — when a default registry (`config docker.registry`) or `push_target` is set — pushes the image and delivers via `apptainer pull docker://` (push failure → tarball fallback, reported in `push_status`; gated ⇒ tarball-only, never pushed, per I13). `tools` = the PRIMARY requested tools. **`env_freeze.build_env_from_tools(name, tools, …)`** is the *declarative* sibling: build a verified image straight from tool NAMES (resolve→route→build), no host install. |
| `generate_user_guide(pipeline_id\|spec, freeze_request_key?)` | **Standalone Markdown export** (NOT the seal deliverable — `seal_workflow` renders the HTML run dashboard instead). Renders a runnable Markdown guide from the pipeline's PASSING, validated run — every command shown was executed + validated. `freeze_request_key` pins the env BY DIGEST. Writes `env_reports/{name}.GUIDE.md`. An opt-in escape hatch for anyone who explicitly wants markdown; the normal Layer-2 deliverable is `{name}.RUN.html` |
| `seal_workflow(pipeline_id, freeze_request_key, workflow_name?, description?)` | **Layer-2 seal.** Validate the run-side invariants (I0/I3/I6/I7/I8 — env-build invariants are Layer 1's), self-test the `usage.command_template` (sets `usage_verified`), PIN the frozen env BY DIGEST, write a `WorkflowSpec` (`{name}.workflow.yaml`) + render the **Layer-2 run dashboard** (`{name}.RUN.html` — validated evidence grouped by compute locus + a distinct how-to panel, rendered PURELY from the verified spec; NO markdown). The env's `{env}.ENV.html` (Layer 1) is **immutable post-freeze** and is NOT touched by seal — cluster-worthiness is a Layer-2 fact carried by the RUN dashboard, never claimed by the env report. Accretion is **per-workflow-per-digest**: a run dashboard accretes loci as the SAME env is validated on more compute resources; a step whose evidence ran against a DIFFERENT env digest (a rebuild) is marked STALE, not shown green. The WorkflowSpec carries its external input sources (`test_data`/`reference_databases`/`runtime_configs`/`authored_artifacts`) so it is **self-verifying** — its I8 re-checks against the artifact alone; seal re-validates the constructed spec and refuses if it wouldn't pass standalone. Sets `validated_in_shipped_image` when every validated step ran in a shipped frozen env image (digest-matched via `run_step_in_container`) — i.e. **validated == shipped**. **Multi-env chaining**: a workflow may chain steps that each ran in their OWN frozen env; the check matches every step's container digest against the set of ALL frozen env digests (the EnvCache), and the `WorkflowSpec` records `envs[]` ({request_key, image, image_digest}). Call `freeze()` first. Refuses on any workflow-invariant violation |
| `download_reference_database(name, url, local_path, pipeline_id)` | Large DBs (>100 MB). Watchdog-safe via run_in_background. Auto-records ReferenceDatabase entry |
| `run_pipeline_step(env, command, pipeline_id, inputs, output_types?, watch_dir?)` | Run + auto-validate every detected output in one call (runs on the HOST conda env) |
| `run_step_in_container(freeze_request_key, command, pipeline_id, inputs, output_types?, watch_dir?, data_dir?, extra_mounts?)` | **The validation-locus pivot: validated == shipped.** Run + auto-validate a step INSIDE the frozen env image (resolved from the EnvCache by `freeze_request_key`; adopted images pulled local). Bind-mounts `data_dir` at its own host path so existing absolute paths work verbatim and outputs land back on the host for validation; captures I7 IN the container (GNU `time -v` → exact peak RSS, host `docker stats` fallback); stamps the step `ran_in_container` + `container_image[_digest]`. Use this (not run_pipeline_step) once frozen, so the recorded run is the one that actually executes on HPC. `seal_workflow` then asserts `validated_in_shipped_image` when every validated step's digest matches the pinned env's |
| `stage_authored_artifact(pipeline_id, path, role, description, content?, generated_by?, language?)` | Any time the agent writes a file outside MCP — driver scripts, synthetic test data, hand-staged BAM/VCF/FASTA, transformed configs. Records content verbatim (text) or genesis command (binary) + sha256. Without this, the artifact's path is an orphan to I8 and `seal_workflow` refuses to write. |
| `start_service(env, service_name, start_command, health_check_command, pipeline_id, service_type?, port?, …)` | Service-dependent tools (Redis, Postgres, web server, Spark). Starts background process, polls until healthy, registers a ServiceDependency record + appends the readiness probe to its health_check_log. Satisfies I10 if the start succeeds. |
| `verify_service_dependency(pipeline_id, service_name, env_name, health_check_command?)` | Append an additional health probe to a declared service's log (mid-pipeline checkpoint, recovery after a flap, manual re-verify). |
| `stop_service(env_name, service_name, stop_command?, pipeline_id?)` | Stop a background service; marks status=stopped + stopped_at when pipeline_id is supplied. |
| `phenopacket_to_vcf(phenopacket_id, output_vcf)` | Materialize a single-sample VCF from a phenopacket — eliminates hand-VCF synthesis |
| `snapshot_project(project_name)` | **HPC bridge.** Walk a project's authorized directories on every compute env it touches (one-level `find`). Returns `entries[]` tagged by `compute_env`. Read-only; the agent's only window into the user's cluster filesystem. Auth: each project's `compute_env_access[].directories[]` lists dirs the agent may walk on each env, each with `permissions:` (`file_name_only` is the snapshot token). Phase 2.5 auto-extends the walk into the env's `agent_scratch_target` / `agent_common_data_target` subtrees for THIS project. |
| `cluster_module_avail(project, env, pattern=)` | **HPC bridge.** Discover loadable Lmod/tmod modules on the cluster so the agent can pick the right `module load X/Y.Z` line. ONE ssh hop running `bash -lc 'module avail <pattern>'`. Output parsed: drops section headers + `(D)`/`(L)` annotations + footer legends. `pattern` must be a safe token (alnum + `_+.-/`); shell metacharacters refused. |
| `upload(project, env, local_path, remote_abs_path, async_globus=False)` / `download(project, env, remote_abs_path, local_path, async_globus=False)` | **HPC bridge — unified transfer surface.** TWO primitives, three auth families auto-routed by where `remote_abs_path` falls: **scratch** (under `env.agent_scratch_target.path`; env-implicit grant; path must be under `<scratch>/<project>/...` for multi-project isolation); **common_data** (under `env.agent_common_data_target.path`; env-implicit; shared across projects); **project_path** (anywhere else; longest-prefix match in `project.directories[]` with the right token). Discrete capabilities: `upload ≠ download ≠ exec`. Wire protocol from `env.data_transfer.type`: `scp_head_node` (default, scp + sha256) or `globus` (encrypted, off-head-node). `async_globus=True` returns a task_id; poll via `globus_task_status`. For one-off transfers use `project_name="_ad_hoc"` — synthesized virtual project with scratch + common_data access on every env, no YAML edit needed. **Every call writes a human-readable `transfer_history/<project>/<YYYY-MM-DD>/<stamp>_<direction>_<hash>.json`** — leads with a plain-English `outcome` (`uploaded`/`downloaded`/`submitted`/`refused`/`failed`) + one-line `summary`, records the ACTUAL `command` that ran (the `globus transfer …` / `scp …` / `cp …`), human `size`/`took`, and omits fields that don't apply (no wall of nulls). The audit trail, no agent memory needed. The diagnostic for `PERMISSION_DENIED` parses `globus task event-list` and routes the actionable hint based on which endpoint reported the error (local GCP Accessible-Folders config / remote `data_access` consent / remote POSIX perms). |
| `globus_task_status(project, env, task_id)` | **HPC bridge — async-Globus poll + confirm.** Pairs with `async_globus=True` on the upload/download primitives. ONE `globus task show <id> --format json` query; returns `status` (ACTIVE / INACTIVE / SUCCEEDED / FAILED), `nice_status`, `bytes_transferred`, `files_transferred`, `fatal_error`. Task_id validated as UUID BEFORE any shell-out. **Reconciles the transfer manifest**: on a terminal state it rewrites the async-submit's `submitted` record to its true `outcome` (`uploaded`/`downloaded` — Globus end-to-end checksum — or `failed`). This is the half-baked-transfer defense: a downstream consumer trusts `outcome == "uploaded"` in the record rather than guessing whether a `submitted` upload landed. **Confirm an async upload SUCCEEDED before any step consumes the file** (or use a sync transfer — `async_globus=False` — which blocks to verified). |
| `cluster_job_status(project, env, job_id)` | **HPC bridge.** SLURM job-state query for polling. ONE ssh hop running `bash -lc 'sacct -j <id> -P --noheader -X -o JobID,State,Elapsed,ExitCode,NodeList,Reason,Start,End'`. `job_id` validated as `\d{1,12}(_\d{1,12})?` (digits + optional `_<task>` for array jobs) BEFORE any ssh — metacharacters refused. Empty sacct output → `jobs: []` (not an error); caller distinguishes "not yet in slurmdbd" from "never existed" with a short retry. |
| `stage_apptainer_image(project, env, freeze_request_key, sif_subpath="")` | **HPC bridge.** Mode-aware HPC delivery from the EnvCache. **ADOPT** (pure-conda + public BioContainer): ONE ssh hop, `apptainer pull <sif> docker://<image_by_digest>`. **BUILD-with-push**: same shape via pushed ref. **BUILD-archive** (default for non-conda): transfer .tar via the unified `upload` primitive (routed to the **container_upload zone** — `env.container_upload_target`, NOT common_data; no fallback), then ssh `apptainer build <sif> docker-archive://<tar>`. Default .sif location: `<container_upload_target>/<env_name>_<short_digest>.sif` (the .tar lands at `<container_upload_target>/apptainer_sources/<name>.tar`). Idempotent — re-stages skip if .sif exists. NOT a composite (caller still calls freeze + submit_workflow_job separately). |
| `submit_workflow_job(project, env, workflow_dir, workflow_name, tool_name, command, inputs, outputs, apptainer_sif, apptainer_module, nextflow_module, slurm)` | **HPC bridge — PRODUCTION submission.** Render via `workflow_render` (main.nf + nextflow.config + launcher.sh) → upload all 3 files to `workflow_dir` via the unified `upload` primitive (routes to project_path zone) → ssh `sbatch --parsable launcher.sh` → write LOCAL manifest → return SLURM `job_id`. No polling — the agent may be cut off long before a real production job finishes, so this primitive is **submit-and-document**: returns immediately + writes `job_submissions/<project>/<workflow_name>_<job_id>.submission.json` carrying everything the user (or a future agent invocation) needs to find the job later (job_id, workflow_dir, command, inputs/outputs, slurm config, apptainer_sif). Auth: `workflow_dir` is REQUIRED and must be authorized via `project.directories[]` with BOTH `upload` (for the file pushes) AND `exec` (so the SLURM job may write outputs in-place). Scratch paths are NOT auto-routed here — for validation/seal runs in the agent's scratch sandbox, use `run_step_on_cluster`. The renderer is generic + per-project ([[project-nextflow-module-principles]]): every `${name}` in `command` must be declared in `inputs ∪ outputs`; script blocks contain the literal shell command; main.nf is human-readable + re-runnable from copy-pasted shell. NOT a composite (caller still calls `freeze` + `stage_apptainer_image` + polls `cluster_job_status` themselves + downloads outputs via the unified `download` primitive). |
| `run_step_on_cluster(pipeline_id, freeze_request_key, project, env, workflow_name, tool_name, command, inputs, outputs, download_local_dir, apptainer_module, nextflow_module, slurm)` | **HPC bridge — Path-4 keystone (VALIDATION/seal).** Run a workflow step ON CLUSTER **in the agent's scratch sandbox**, poll to completion, fetch outputs, validate, record a `pipeline_step` with `validation_locus="cluster"` for `seal_workflow` to consume. The wall: workflow_dir is ALWAYS `<env.agent_scratch_target.path>/<project>/<workflow_name>/` — no caller knob. Hard-fails if the env has no scratch target. Auth: `check_env_target_capability(scratch, "exec")` — schema enforces scratch has `exec`. Composes a **loud input precondition** (`remote_paths_exist` — every declared input must ALREADY exist on the cluster; run_step_on_cluster does NOT stage input DATA, so a missing input fails fast BEFORE any staging/sbatch instead of deep in the Nextflow run — no auto-staging, the user's rails decide where data lives) + `stage_apptainer_image` + **`inspect_staged_sif`** (the **C2 round-trip**: sha256 + `apptainer inspect` of the .sif THAT WILL RUN — recorded as `cluster_sif_sha256`/`cluster_image_verified`, so the shipped-image badge rests on a real on-cluster observation, not a nominal digest copied from the EnvCache) + render + `upload`×3 (routed to scratch zone) + `sbatch_via_ssh` + `cluster_job_status` (polling viable here because validation jobs are bounded, unlike production) + `cluster_job_resources` (sacct **real MaxRSS/Elapsed** for I7) + `download`×N (sha256 round-trip) + type-aware validation. Seals the **cluster context** onto the step (`cluster_apptainer_module`/`cluster_nextflow_module`/`cluster_slurm` placement + observed `cluster_node`) for reproducibility. The cluster analog of `run_step_in_container`. After it returns, three legitimate next moves: (a) `seal_workflow` to produce a `WorkflowSpec` with cluster-locus evidence, (b) call again with a different command for multi-step workflows, (c) `discard_pipeline_draft` to run-and-go. |

Below the primitives there are still lower-level tools (`run_in_env`, `validate_output`, `verify_installation`, `patch_pipeline`, etc.) — use them when a primitive doesn't fit. Prefer the primitive when it does.

### Two layers — environment vs. workflow

Two lifecycles. **Layer 1 — the environment** is solved *once*, frozen, and content-addressed: build it with the install primitives, then `freeze()` produces a digest-addressed, HPC-shippable artifact registered in the EnvCache (a later identical request returns it by hash, no re-solve). **Layer 2 — the workflow** *consumes* a frozen env by digest: `seal_workflow()` validates the run-side invariants, pins the env, and writes a `WorkflowSpec` + an HTML run dashboard (`{name}.RUN.html`) rendered from the passing run. The env is the reusable "solved component" (its `{env}.ENV.html` written once at freeze, immutable); workflows are the run-many, per-experiment artifacts on top of it. (The pre-respine combined `finalize_pipeline` / `save_pipeline_spec` / `build_docker_image` host path — and its host-side env-build invariant re-anchoring — has been **retired**; `freeze` + `seal_workflow` are the only spec-producing surface.)

**Validated == shipped.** Once an env is frozen, run the workflow's steps with `run_step_in_container` (not `run_pipeline_step`) so they execute INSIDE the shipped image. This collapses the host-validate/ship-build split that otherwise forces per-tier cross-arch replay: the bytes the user runs on HPC are the exact bytes we validated, and `seal_workflow` records that as `validated_in_shipped_image` (digest-matched). The host `run_pipeline_step` remains for pre-freeze iteration. (Freeze itself is platform-honest: it ADOPTs a biocontainer only for pure-conda envs and CONTAINER-NATIVE-BUILDs the ship-platform image for any non-conda install — built in-container, so cross-arch needs no host conda-pack and no cross-arch refusal; see the freeze row.)

---

## Protocol

The full flow, in order — two layers (env, then workflow):

1. **`start_pipeline(name, description)`** — returns `pipeline_id`. Thread it through every subsequent call.
2. **Compose install primitives** to build the env. Conda first; then R / pip / JAR / binary / source / cargo / go / perl; then `download_reference_database` for any large external data. Each primitive auto-merges its install_step into the draft.
3. **`select_test_data(...)`** — pick a dataset from `data/core_test_data_hg38/manifest.yaml` (or generate one with `phenopacket_to_vcf` or a small Rscript / Python).
4. **`run_pipeline_step(...)`** — run the tool against test inputs on the host for fast iteration. Every detected output is auto-validated.
5. **`patch_pipeline(pipeline_id, {usage: {...}, notes: [...], runtime_environment: {...}, ...})`** — fill the fields no tool provides. Most important is **`usage`**: command_template with `{PLACEHOLDER}` slots, inputs[].format, outputs[].files globs. This is the contract `seal_workflow` self-tests against.
6. **`freeze(env, tools, pipeline_id=…)`** — **Layer 1.** Build (or adopt by digest) the content-addressed, HPC-shippable env image; non-conda installs are installed + validated INSIDE the ship image (`env_honesty.check_build`). Returns a `freeze_request_key`. Docker daemon required.
7. **`run_step_in_container(freeze_request_key, …)`** — re-run the workflow's steps INSIDE the frozen image so the recorded run is the one that ships (`validated == shipped`; in-container `resource_usage`). Replaces the host `run_pipeline_step` runs once frozen.
8. **`seal_workflow(pipeline_id, freeze_request_key)`** — **Layer 2.** Validate the run-side invariants (I0/I3/I6/I7/I8), self-test `usage.command_template` (I4), pin the env BY DIGEST, and write the `WorkflowSpec` + the HTML run dashboard (`{name}.RUN.html`) rendered from the validated run. Refuses on any violation.
9. **`write_pipeline_provenance(...)`** — record the specific run.

That's the local-validation protocol. To execute the *same* frozen env ON HPC, the Phase 2 bridge consumes the `freeze_request_key` directly: `run_step_on_cluster` (validation/seal — runs in the agent's scratch sandbox + records a sealed `pipeline_step`) for the validate-then-seal flow; `stage_apptainer_image` → `submit_workflow_job` → `cluster_job_status` → `download` for production runs against the user's project workspace. See the **HPC bridge — Phase 2** section below for the full split.

---

## Async pattern — for anything that may run silently >5 minutes

The agent's stream-watchdog kills a tool call that goes silent for ~600s. Use `run_in_background` + `check_job` for big downloads, long conda solves, multi-hour assemblies. `download_reference_database` already does this internally.

```python
job = run_in_background(command="...", env_name="bioinf_x")
while check_job(job["job_id"])["state"] == "running":
    # do other work; the agent stays alive because check_job is constantly producing output
    pass
```

---

## Data on disk

Core test data lives at `data/core_test_data_hg38/` (8 read datasets + ACTB phenopacket + chr22 reference). Read `manifest.yaml` to enumerate. Pipeline-specific test data goes in `data/{pipeline_name}_test_data/`.

Generated artifacts:
- `envs/bioinf_{name}/` — the host conda env (pre-freeze iteration)
- **Layer 1 (`freeze`)** — the env image in the local Docker daemon + its EnvCache record (`request_key` → digest, with the full SBOM + `validation_locus`); the Apptainer HPC delivery (registry-free `docker save` tarball under `docker_images/{name}/` → `apptainer build docker-archive`, or a registry push); and two record-rendered deliverables: `env_reports/{name}.ENV.html` (the env report) + `env_reports/{name}.attestation.json` (in-toto/SLSA provenance)
- **Layer 2 (`seal_workflow`)** — `env_reports/{name}.workflow.yaml` (the `WorkflowSpec`, machine-verified) + `env_reports/{name}.RUN.html` (the run dashboard rendered from the validated run — validated evidence per compute locus + a distinct how-to panel; the retired markdown guide is available on demand via `generate_user_guide`)

---

## Schema cheatsheet (avoid seal rejection)

- `notes`: `list[str]` (a bare string is auto-wrapped)
- `runtime_configs[*].format`: `yaml | properties | java_properties | ini | json | xml | tsv | txt`
- `OutputFile.type`: see the FileType union in `agent/models/core_data.py` (includes `jsonl`, `bedgraph`, `methylation_report`, `sqlite`, etc.)
- `install_method.type`: `conda | jar | pip | r_install | binary | source | perl | cargo | go | docker_pull | manual`
- `runtime_environment.type`: `conda | jar | r | docker | native`
- `usage.trials[*]`: `{name, substitutions: {PLACEHOLDER: abs_path}, description?}` — declare one trial per input shape (paired-gz, single-uncompressed, …) so I4 proves multi-shape coverage. Empty list ⇒ single inferred trial (backward-compatible).
- **Seal-required field the invariants DON'T catch**: `usage.description` (a one-line string — required by the model whenever a `usage` block is present; surfaces only as a pydantic `WorkflowSpec validation failed` at write time, *after* every invariant passes). Fill it before `seal_workflow`. (`reference_databases[*].source_url` is now optional — a locally-staged reference with no download origin may omit it; I5 pins content by sha256, not URL.)
- **Output placeholders in `usage.command_template`**: write every output path through an OUTPUT slot — one named `{OUTPUT_DIR}`/`{OUT_DIR}` or containing `output`. The I4 self-test runs each trial in a fresh scratch dir and fills THAT path into output slots, then scans it for `usage.outputs[*].files`. An output written via an unrecognized slot (e.g. `-o {OUT_TSV}`) lands outside the scratch dir → I4 fails with `produced_files: []`. Correct idiom: `-o {OUTPUT_DIR}/stats.tsv` (declare the glob in `usage.outputs[*].files`).
- **`run_pipeline_step` output detection**: the step only detects files created/modified under `watch_dir` (default: the input's directory). If your command writes elsewhere via `-o <path>`/`> <path>`, pass `watch_dir=<that dir>` — an undetected output has no validation and fails I3 at seal. A rc=0 run with no `detected_outputs` returns an `output_detection_hint`.

---

## Configuration

`config/agent_config.yaml` — Docker base image, conda channels, default Python, agent timeouts.
`config/core_datasets.yaml` — what gets bootstrapped by `setup_core_test_data.sh` (read datasets + phenopackets).
`.claude/settings.json` — MCP server registration. Set `BIOINF_MCP_AUTO_RELOAD=1` (default in this repo) so the server hot-reloads on code changes; no manual `/mcp` reconnect needed.

---

## HPC bridge — Phase 2

Layer-1 produces an HPC-shippable container (per the freeze row above). The bridge is what the agent uses to **actually drive a job on a real cluster** — push inputs, stage the container, sbatch a Nextflow workflow, poll, pull outputs back. Same trust posture as the rest of the codebase: every primitive is gated by `projects_access.yaml`, every transfer is sha256-round-tripped, every shell line passes a safe-token validator BEFORE any ssh.

### Two walls, two operations

The bridge is built around a deliberate split between WHERE the agent operates and HOW long it stays around to watch:

- **Scratch — the agent's sandbox.** Cluster validation/seal runs live here. Always `<env.agent_scratch_target.path>/<project>/<workflow_name>/`. The agent owns this zone (env-implicit grant, project-prefix isolation). Validation jobs are short and bounded, so the synchronous "poll-to-completion + fetch + validate + record + seal" loop in `run_step_on_cluster` is viable.
- **`directories[]` — the user's territory.** Production runs live here. The user explicitly declares each path in `project.directories[]` with the right permission tokens. Production jobs may run for hours-to-days; the agent's stream-watchdog kills tool calls that go silent for ~10 min. So `submit_workflow_job` is **submit-and-document**: returns the `job_id` + writes a local manifest to `job_submissions/<project>/<workflow_name>_<job_id>.submission.json`, and the user (or a future agent invocation) follows up at their own pace via `cluster_job_status` + the unified `download` primitive.

The two operations share the same render+sbatch machinery (via `submit_workflow.render_workflow_files` + `submit_workflow.sbatch_via_ssh`) but each owns its own auth surface — scratch via `check_env_target_capability`, project_path via `check_permission` against `directories[]`. The walls don't get crossed inside one primitive.

### The command-and-control file: `projects_access.yaml`

A single user-authored YAML at the repo root (gitignored — personal). Two top-level sections:

- **`compute_envs[]`** — one block per environment (laptop, hpc_cluster, …). Each has `type: ssh|local`, ssh `host`/`user`, and optional Phase-2 target blocks: `agent_scratch_target` (sandbox), `agent_common_data_target` (shared reference / staged .sifs), `slurm` (closed-key block: `queue_default`, `account`, etc.), and `data_transfer` (closed-key block picking the wire protocol: `scp_head_node` (default) or `globus` — when globus, the nested `globus` block carries `local_endpoint_id` + `remote_endpoint_id` + display names).
- **`projects[]`** — one block per logical project. Each lists `compute_env_access[]` — which envs this project may use, AND a project-specific `directories[]` per env with explicit `permissions:` per dir (`file_name_only`, `upload`, `download`, `exec`).

Auth is **discrete**, not a lattice: `upload` ≠ `download` ≠ `exec`. A dir declared `[upload]` does NOT implicitly grant `download`. The primitive table above shows which token each primitive requires; mismatches raise `PermissionDenied` BEFORE any ssh.

### Three transfer auth families (intentionally separate)

| Family | Path syntax | Auth chain |
|--------|-------------|------------|
| **scratch** | relative `remote_subpath` | project on env + `env.agent_scratch_target.permissions` includes op; path auto-prefixed with `project_name` |
| **common_data** | relative `remote_subpath` | project on env + `env.agent_common_data_target.permissions` includes op; NO project prefix (shared zone) |
| **project_path** | absolute `abs_path` | Phase-1 explicit: `project.directories[]` longest-prefix-match contains `abs_path` AND has the right permission token |

scratch is for per-run staging; common_data is for reference data + staged container images; project_path is for the user's real project layout. Each family is a separate primitive (so the agent can't accidentally mix authority).

### Wire protocol — scp_head_node vs globus

The auth families above describe WHERE files can go. The `data_transfer` block on each env picks HOW bytes move:

- **`scp_head_node`** (default when `data_transfer` is absent) — scp + ssh sha256sum round-trip. Fine for small files; rude to the cluster's head node for GB-scale .sif images.
- **`globus`** — every `upload` / `download` + `stage_apptainer_image`'s `.tar` transfer goes through the Globus CLI. Encrypted end-to-end, off-head-node, checksum-verified by Globus itself. Requires `globus login` + Globus Connect Personal installed locally; UUIDs of both endpoints declared in the env's `data_transfer.globus` block. Hard-errors on Globus failure — never silent fallback to scp. The first-time-setup discovery flow: `globus endpoint search "<cluster display name>"` → collection UUID → `globus gcs collection show <UUID>` (errors with `MissingLoginError` + the exact `globus login --gcs <GCS_UUID>` to run) → run that → Globus Connect Personal Preferences → Access tab → add any local folders to transfer FROM (default is `$HOME` only). Read-only `globus ls` does NOT require `data_access` consent — a successful `ls` does NOT prove transfers will work; verify by submitting a small test transfer.

For huge transfers (multi-GB .sif images, full datasets), every primitive accepts `async_globus=True` — returns immediately with a Globus `task_id` instead of polling to SUCCEEDED. Poll later via `globus_task_status(project, env, task_id)`. The escape hatch when the agent's ~10-minute stream-watchdog would kill a sync wait.

### The validation chain (scratch — short, synchronous, seal-ready)

Use this to prove a frozen env actually works on the cluster — the bytes the user runs on HPC are the bytes we validated:

1. **`freeze(env, tools)`** — Layer 1. Builds (or adopts by digest) the HPC-shippable image.
2. **`start_pipeline(name, description)`** — opens a draft.
3. **`run_step_on_cluster(pipeline_id, freeze_request_key, project, env, workflow_name, …)`** — Path-4 keystone. Stages the .sif (idempotent), renders+uploads main.nf/nextflow.config/launcher.sh to `<scratch>/<project>/<workflow_name>/`, sbatches, polls to completion, fetches outputs back via the unified `download` primitive, validates each, records a cluster-locus `pipeline_step`. Workflow_dir is computed internally — no caller knob.
4. **`patch_pipeline(pipeline_id, {usage: …})`** — fill the seal-required fields the runtime can't capture.
5. **`seal_workflow(pipeline_id, freeze_request_key)`** — Layer 2. Validates I0/I3/I6/I7/I8, self-tests `usage.command_template`, writes `{name}.workflow.yaml` + `{name}.RUN.html`.

### The production chain (`directories[]` — long-running, submit-and-document)

Use this once an env is validated and the user wants to run their real pipeline:

1. **`stage_apptainer_image(project, env, freeze_request_key)`** — Mode-aware delivery; idempotent (skips if .sif already exists).
2. **`upload(project, env, local_path, remote_abs_path)`** (×N) — Push input data into the project workspace (path under `directories[]` routes to project_path zone).
3. **`submit_workflow_job(project, env, workflow_dir, workflow_name, …)`** — Renders + uploads + sbatches. Returns immediately with `job_id`. Writes `job_submissions/<project>/<workflow_name>_<job_id>.submission.json` recording everything needed to find this job again. **No polling here — the agent doesn't sit around for hours.**
4. **`cluster_job_status(project, env, job_id)`** — Query whenever the user (or a future agent invocation) wants to check. sacct-backed; covers both running and completed jobs. The manifest from step 3 carries the `job_id`.
5. **`download(project, env, remote_abs_path, local_path)`** — Pull outputs back when done; sha256 round-trip on the fetch (or Globus end-to-end if configured).

For pre-submission exploration: **`snapshot_project(project_name)`** is a read-only `find -maxdepth 1` against the project's authorized dirs; **`cluster_module_avail(project, env, pattern=)`** lists Lmod modules so the agent picks the right `module load X/Y.Z` line.

### The renderer's contract (per [[project-nextflow-module-principles]])

Every workflow `workflow_render` produces is **human-readable + locally re-runnable**:

- Inputs/outputs flow through top-level `params.x = '<value>'` declarations (dot notation — Groovy parses `params { ... }` as a method call, which Nextflow rejects).
- Each process's `script:` block holds the LITERAL shell command with `${params.x}` substituted — no DSL magic. A human reads main.nf, copy-pastes the line with the params filled in, re-runs the step from a shell.
- Tools come from a frozen apptainer .sif (`stage_apptainer_image`'s output). The process invokes `apptainer exec <sif> <cmd>` so swapping versions is a one-line .sif path change.
- The launcher.sh sets `NXF_HOME=$PWD/.nextflow_home` and cd's via `${SLURM_SUBMIT_DIR:-…}` — both bake in fixes for real-world HPC traps surfaced during live-driving (HOME not writable from compute nodes; SLURM stages `$0` into `/var/spool/slurmd/job<id>/`).

### ssh ControlMaster pattern (no password prompts)

The user opens `ssh hpc-agent` in a separate terminal and leaves it open. Every bridge primitive uses ssh BatchMode (fails fast if no live session) and piggybacks on the ControlMaster socket the side terminal opened. The agent never sees a password or key.

### Drive playbooks

- [docs/hpc_bridge_phase_a_playbook.md](docs/hpc_bridge_phase_a_playbook.md) — the read + small-transfer surface (4-call MINIMUM PATH).
- [docs/hpc_bridge_phase_b_playbook.md](docs/hpc_bridge_phase_b_playbook.md) — the full submit→poll→fetch chain (6-call MINIMUM PATH, samtools-view-on-a-BAM demo). Predates `stage_apptainer_image` — uses an explicit upload of the .sif instead; the principle is unchanged.

---

## Tests

`pytest tests/` — covers both layers: `env_honesty.check_build` (BUILT / VALIDATED_IN_IMAGE incl. echo-cheat shapes / POLICY_CLEAN I12+I13) for the env, and `check_workflow_invariants` (I0/I3/I6/I7/I8) + the usage self-test for the workflow. Sanity tests verify the gates themselves catch silent-empty-success steps, relative paths, undeclared placeholders, and orphan step inputs.
