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
| `install_r_package(env, name, source, pipeline_id)` | source = `cran` \| `bioconductor` \| `github:owner/repo` — handles library isolation, BiocManager bootstrap, requireNamespace load-or-die |
| `install_pip_package(env, name, version?, pipeline_id)` | pip / PyPI — handles import check |
| `install_jar_tool(env, tool, jar_url, java_flags, pipeline_id)` | Java tools (Exomiser, Picard, GATK, snpEff) — auto-downloads, unpacks, writes wrapper, sets install_method.type=jar |
| `install_git_repo(env, repo_url, tool_name, ref?, build_command?, verify_command?, bin_path?, pipeline_id)` | Clone-and-run repos that aren't conda/pip/jar packages (academic script collections; C/C++ tools built with make). Clones into `{env}/share/{tool}`, pins the commit SHA, optional build + smoke verify. `bin_path` (relative path to the built executable, e.g. `seqtk`) writes a PATH wrapper AND is recorded with `build_command` so **freeze can rebuild the tool in the ship image** (source replay); omit for run-by-path scripts. Sets install_method.type=source; anchored by I11. **Pin `ref` to a tag/commit** — a bare default branch drifts |
| `install_release_binary(env, tool_name, url, sha256?, binary_in_archive?, wrapper_name?, verify_command?, pipeline_id)` | Precompiled static binaries on GitHub releases / vendor URLs (mosdepth, somalier, sylph, dorado, cellranger). Download → sha256-anchor (mismatch = hard fail) → extract → chmod → PATH wrapper. A smoke verify catches a wrong-arch binary. Sets install_method.type=binary; anchored by I14 |
| `install_perl_package(env, module, distribution?, cpanm_flags?, build_env?, pipeline_id)` | Perl/CPAN modules (Ensembl VEP plugins, niche CPAN) via cpanm. Requires `perl` + `perl-app-cpanminus` in the env. Verifies `perl -M{module} -e1`. `build_env` = `KEY=VAL` exports for XS builds linking a conda C lib (e.g. `HTSLIB_DIR=$CONDA_PREFIX`) — use `$CONDA_PREFIX` so freeze's replay resolves it in the ship image. Sets install_method.type=perl; freeze replays the cpanm build. **Prefer conda when the module is on bioconda** (e.g. `perl-bio-db-hts`, `perl-bioperl` — BioPerl-class mega-deps install far more reliably via conda); this tier is for the cpanm-only modules |
| `install_cargo_tool(env, crate, version?, binary_name?, git_url?, pipeline_id)` / `install_go_tool(env, package, version?, binary_name?, pipeline_id)` | Rust / Go tools not on bioconda. `cargo install --root {env}` / `GOBIN={env}/bin go install`. cli_which is the anchor (locally built ⇒ can't be wrong-arch). **Captures the host `rustc`/`go` version** into install_method so freeze can replay the build with the identical toolchain on the ship platform (reproducible cross-arch). **Prefer conda when available** (many are on bioconda); these are the fallback |
| `freeze(env, tools, pipeline_name?, platform?, accel?, gated?, licenses?, push_target?, pipeline_id)` | Freeze the env into a content-addressed, HPC-shippable artifact. request_key→EnvCache lookup (hit = proven artifact by hash, no re-solve). **ADOPT-or-BUILD**: **pure conda + a published BioContainer → ADOPT by digest** (no build); **everything else → CONTAINER-NATIVE BUILD** via `env_freeze.build_env_image` — install **and validate INSIDE the ship-platform image** (one generic bake; *validated == shipped*), cross-arch included (built in-container; no host-arch conda-pack, no cross-arch refusal). Covers every tier — conda · pip (engine `--pypi`, in-lock) · cran/bioconductor (Rscript) · binary (linux asset re-fetched + sha256) · jar (download + JRE + `java -jar` wrapper) · source (clone at the pinned commit + rebuild) · cargo/go (engine toolchain) · perl (cpanm; **XS compiles against the conda perl** via conda c/cxx-compiler + an xlocale.h shim) · half-baked (authored files baked). The build is **multi-stage**: a builder with the full toolchain → a slim runtime that COPYs only the env (identical paths) + artifacts + the `.so` runtime libs (build-essential never ships). Refuses on any honesty-contract violation (`env_honesty.check_build`: BUILT · VALIDATED_IN_IMAGE · POLICY_CLEAN), incl. I13 — a **gated** build must pass `licenses`. A biocontainer is NOT adopted when the env has non-conda installs it can't represent. Records `shipped_binaries[]` (the baked long-tail command = its provenance) + `build_method`, the **full SBOM** (`resolved_packages` conda/pip closure + `system_packages` apt/OS layer, all versioned, read from the shipped image) + **`validation_locus`** (native/emulated — I7 timings authoritative only when native). Writes two **Layer-1 deliverables rendered PURELY from the verified record** (can't be faked): `env_reports/{name}.ENV.md` (the env report — REQUESTED tools with in-image validation evidence vs along-for-the-ride deps) + `env_reports/{name}.attestation.json` (in-toto/SLSA provenance; sign with `cosign attest`). The base image is **pinned by digest** (`container_build.BASE_IMAGE`) for OS-layer reproducibility. Emits the Apptainer HPC delivery contract: registry-free `docker save`→`apptainer build docker-archive` by DEFAULT, OR — when a default registry (`config docker.registry`) or `push_target` is set — pushes the image and delivers via `apptainer pull docker://` (push failure → tarball fallback, reported in `push_status`; gated ⇒ tarball-only, never pushed, per I13). `tools` = the PRIMARY requested tools. **`env_freeze.build_env_from_tools(name, tools, …)`** is the *declarative* sibling: build a verified image straight from tool NAMES (resolve→route→build), no host install. |
| `generate_user_guide(pipeline_id\|spec, freeze_request_key?)` | Layer-2 deliverable: render a runnable Markdown guide from the pipeline's PASSING, validated run — every command shown was executed + validated (only validated pipeline_steps + a self-tested usage template). `freeze_request_key` pins the env BY DIGEST (Apptainer delivery + content/image digests) from the EnvCache. Writes `env_reports/{name}.GUIDE.md` |
| `seal_workflow(pipeline_id, freeze_request_key, workflow_name?, description?)` | **Layer-2 seal.** Validate the run-side invariants (I0/I3/I6/I7/I8 — env-build invariants are Layer 1's), self-test the `usage.command_template` (sets `usage_verified`), PIN the frozen env BY DIGEST, render the guide, write a `WorkflowSpec` (`{name}.workflow.yaml` + `{name}.GUIDE.md`). The WorkflowSpec carries its external input sources (`test_data`/`reference_databases`/`runtime_configs`/`authored_artifacts`) so it is **self-verifying** — its I8 re-checks against the artifact alone, not the draft it was sealed from; seal re-validates the constructed spec and refuses if it wouldn't pass standalone. Sets `validated_in_shipped_image` when every validated step ran in a shipped frozen env image (digest-matched via `run_step_in_container`) — i.e. **validated == shipped**. **Multi-env chaining**: a workflow may chain steps that each ran in their OWN frozen env (each its own `freeze`); the check matches every step's container digest against the set of ALL frozen env digests (the EnvCache), and the `WorkflowSpec` records `envs[]` ({request_key, image, image_digest}) — the primary `freeze_request_key` is the guide's get-the-image env. Call `freeze()` first. Refuses on any workflow-invariant violation |
| `download_reference_database(name, url, local_path, pipeline_id)` | Large DBs (>100 MB). Watchdog-safe via run_in_background. Auto-records ReferenceDatabase entry |
| `run_pipeline_step(env, command, pipeline_id, inputs, output_types?, watch_dir?)` | Run + auto-validate every detected output in one call (runs on the HOST conda env) |
| `run_step_in_container(freeze_request_key, command, pipeline_id, inputs, output_types?, watch_dir?, data_dir?, extra_mounts?)` | **The validation-locus pivot: validated == shipped.** Run + auto-validate a step INSIDE the frozen env image (resolved from the EnvCache by `freeze_request_key`; adopted images pulled local). Bind-mounts `data_dir` at its own host path so existing absolute paths work verbatim and outputs land back on the host for validation; captures I7 IN the container (GNU `time -v` → exact peak RSS, host `docker stats` fallback); stamps the step `ran_in_container` + `container_image[_digest]`. Use this (not run_pipeline_step) once frozen, so the recorded run is the one that actually executes on HPC. `seal_workflow` then asserts `validated_in_shipped_image` when every validated step's digest matches the pinned env's |
| `stage_authored_artifact(pipeline_id, path, role, description, content?, generated_by?, language?)` | Any time the agent writes a file outside MCP — driver scripts, synthetic test data, hand-staged BAM/VCF/FASTA, transformed configs. Records content verbatim (text) or genesis command (binary) + sha256. Without this, the artifact's path is an orphan to I8 and `seal_workflow` refuses to write. |
| `start_service(env, service_name, start_command, health_check_command, pipeline_id, service_type?, port?, …)` | Service-dependent tools (Redis, Postgres, web server, Spark). Starts background process, polls until healthy, registers a ServiceDependency record + appends the readiness probe to its health_check_log. Satisfies I10 if the start succeeds. |
| `verify_service_dependency(pipeline_id, service_name, env_name, health_check_command?)` | Append an additional health probe to a declared service's log (mid-pipeline checkpoint, recovery after a flap, manual re-verify). |
| `stop_service(env_name, service_name, stop_command?, pipeline_id?)` | Stop a background service; marks status=stopped + stopped_at when pipeline_id is supplied. |
| `phenopacket_to_vcf(phenopacket_id, output_vcf)` | Materialize a single-sample VCF from a phenopacket — eliminates hand-VCF synthesis |

Below the primitives there are still lower-level tools (`run_in_env`, `validate_output`, `verify_installation`, `patch_pipeline`, etc.) — use them when a primitive doesn't fit. Prefer the primitive when it does.

### Two layers — environment vs. workflow

Two lifecycles. **Layer 1 — the environment** is solved *once*, frozen, and content-addressed: build it with the install primitives, then `freeze()` produces a digest-addressed, HPC-shippable artifact registered in the EnvCache (a later identical request returns it by hash, no re-solve). **Layer 2 — the workflow** *consumes* a frozen env by digest: `seal_workflow()` validates the run-side invariants, pins the env, and writes a `WorkflowSpec` + a user guide rendered from the passing run. The env is the reusable "solved component"; workflows are the run-many, per-experiment artifacts on top of it. (The pre-respine combined `finalize_pipeline` / `save_pipeline_spec` / `build_docker_image` host path — and its host-side env-build invariant re-anchoring — has been **retired**; `freeze` + `seal_workflow` are the only spec-producing surface.)

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
8. **`seal_workflow(pipeline_id, freeze_request_key)`** — **Layer 2.** Validate the run-side invariants (I0/I3/I6/I7/I8), self-test `usage.command_template` (I4), pin the env BY DIGEST, and write the `WorkflowSpec` + user guide rendered from the validated run. Refuses on any violation.
9. **`write_pipeline_provenance(...)`** — record the specific run.

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
- **Layer 1 (`freeze`)** — the env image in the local Docker daemon + its EnvCache record (`request_key` → digest, with the full SBOM + `validation_locus`); the Apptainer HPC delivery (registry-free `docker save` tarball under `docker_images/{name}/` → `apptainer build docker-archive`, or a registry push); and two record-rendered deliverables: `env_reports/{name}.ENV.md` (the env report) + `env_reports/{name}.attestation.json` (in-toto/SLSA provenance)
- **Layer 2 (`seal_workflow`)** — `env_reports/{name}.workflow.yaml` (the `WorkflowSpec`, machine-verified) + `env_reports/{name}.GUIDE.md` (the runnable guide rendered from the validated run)

---

## Schema cheatsheet (avoid seal rejection)

- `notes`: `list[str]` (a bare string is auto-wrapped)
- `runtime_configs[*].format`: `yaml | properties | java_properties | ini | json | xml | tsv | txt`
- `OutputFile.type`: see the FileType union in `agent/models/core_data.py` (includes `jsonl`, `bedgraph`, `methylation_report`, `sqlite`, etc.)
- `install_method.type`: `conda | jar | pip | r_install | binary | source | perl | cargo | go | docker_pull | manual`
- `runtime_environment.type`: `conda | jar | r | docker | native`
- `usage.trials[*]`: `{name, substitutions: {PLACEHOLDER: abs_path}, description?}` — declare one trial per input shape (paired-gz, single-uncompressed, …) so I4 proves multi-shape coverage. Empty list ⇒ single inferred trial (backward-compatible).

---

## Configuration

`config/agent_config.yaml` — Docker base image, conda channels, default Python, agent timeouts.
`config/core_datasets.yaml` — what gets bootstrapped by `setup_core_test_data.sh` (read datasets + phenopackets).
`.claude/settings.json` — MCP server registration. Set `BIOINF_MCP_AUTO_RELOAD=1` (default in this repo) so the server hot-reloads on code changes; no manual `/mcp` reconnect needed.

---

## HPC / Singularity

Docker images are `--platform linux/amd64`, no `USER`, `WORKDIR=/data`.
```bash
singularity pull bioinf_samtools.sif docker://bioinf_samtools:1.21
```

---

## Tests

`pytest tests/` — covers both layers: `env_honesty.check_build` (BUILT / VALIDATED_IN_IMAGE incl. echo-cheat shapes / POLICY_CLEAN I12+I13) for the env, and `check_workflow_invariants` (I0/I3/I6/I7/I8) + the usage self-test for the workflow. Sanity tests verify the gates themselves catch silent-empty-success steps, relative paths, undeclared placeholders, and orphan step inputs.
