# Bioinformatics Install Agent

Installs bioinformatics tools into isolated conda envs, validates them against test data, packages as HPC Docker images, and emits a machine-verified spec. Designed to be **a solved component** — call once per tool/version, get a trustworthy artifact, never look at it again.

```bash
pip install -r requirements.txt
./scripts/setup_core_test_data.sh   # one-time: core_tools env + chr22 + 8 read datasets + ACTB phenopacket
```

Then drive via Claude Code MCP, or any agent that speaks our tool surface.

---

## The honesty contract

A spec written by `finalize_pipeline` is machine-verifiable. The runtime enforces these invariants at finalize — if any fails, the spec doesn't get written:

| ID | Invariant | Source of truth |
|----|-----------|-----------------|
| I1 | every `install_step.returncode == 0` (or marked `status: failed`) | runtime subprocess exit code |
| I2 | every non-infrastructure package has `verify_output` AND its check_command invokes the package (or, for conda-prefixed names like `r-locfit` / `bioconductor-deseq2` / `python-foo`, the unprefixed library name) as a word-boundary token | `verify_installation` rejects echo-cheats; requires a presence anchor — `which {name}` (CLI) OR a `conda list`/`pip show` registry hit (library) — so a print-string cheat fails even for library-only packages |
| I3 | every `pipeline_step` with rc=0 has validated `detected_outputs` AND no validation uses `expected_type="any"` | filesystem snapshot + type-aware `validate_output` (samtools / bcftools / json.loads / …) |
| I4 | `usage.command_template` executes against **every declared trial** AND each produced file passes type-aware validation | per-trial fresh scratch dir + `OutputValidator` on each produced file |
| I5 | every `reference_database.local_path` exists on disk | filesystem check |
| I6 | every path in inputs / outputs is absolute | string check |
| I7 | every `pipeline_step` with rc=0 has `resource_usage` (wall, peak RSS, peak CPU) | psutil monitor polled the process tree during execution |
| I8 | every `pipeline_step` input traces to a prior step's output OR an external source (test_data, reference_databases, runtime_configs, **authored_artifacts**) | universe-of-prior-outputs walk at finalize |
| I9 | every `authored_artifact` is present on disk AND its bytes hash to the recorded sha256 | `stage_authored_artifact` captures sha256 at stage-time; finalize re-hashes and rejects drift |
| I10 | every `service_dependency` has ≥1 entry in `health_check_log` with `healthy: true` | `start_service(pipeline_id=…)` records the readiness probe; `verify_service_dependency` appends additional probes |
| I11 | every `source` (git-repo) install has a recorded `commit_sha` AND its `local_path` clone exists on disk | `install_git_repo` clones at a pinned ref, resolves `git rev-parse HEAD`; finalize re-checks the clone is present |
| I12 | accelerator honesty: `cuda`/`rocm` need `toolkit_version`; `runtime: runtime_verified` needs a captured `runtime_probe` + `min_driver_version`; `mps` must be `dev_only` (doesn't containerize) | `_check_accelerator` — structural guard; true runtime anchoring needs a GPU runner (Phase 3) |
| I13 | license-gated artifacts must set `redistributable: false` AND record `licenses[]` | `_check_license` — the procedural firewall against republishing someone else's gated artifact |
| I14 | every `binary` (release-binary) install records `binary_url` + `sha256`, exists on disk, AND re-hashes to the recorded `sha256` at finalize | `install_release_binary` records `sha256` = the EXTRACTED binary's hash (what runs / I14 re-hashes) and `asset_sha256` = the downloaded archive's hash (provenance, checked vs the publisher); finalize re-hashes the on-disk binary |

**patch_pipeline** is restricted to agent-authored keys: `description`, `notes`, `final_summary`, `conda_env`, `created_at`, `python_version`, `reference_free`, `runtime_environment`, `runtime_configs`, `reference_databases`, `usage`, `accelerator`, `license_gated`, `licenses`, `redistributable`. Patches to `pipeline_steps`, `install_steps`, `packages`, `verifications`, `test_data`, `docker`, `authored_artifacts`, `service_dependencies`, or any derived status (`env_status` / `pipeline_status` / `docker_status` / `usage_verified` / `lock_sha256`) are **rejected** — those must flow through their dedicated primitive so the runtime is the sole producer.

`env_status` / `pipeline_status` / `docker_status` are derived, never agent-asserted. `docker_status` is anchored to `docker image inspect <tag>` at finalize, not the spec's claim — even if `docker.build_success` is truthy, status flips to `failed` if the image isn't present in the local daemon. `lock_sha256` is computed from a fresh `conda list --explicit` probe of the env at finalize.

Nothing in a finalized spec is taken on faith from the agent. The few free-form fields (`description`, `notes`) are clearly LLM-authored and explicitly *not* part of the verified surface.

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
| `install_git_repo(env, repo_url, tool_name, ref?, build_command?, verify_command?, pipeline_id)` | Clone-and-run repos that aren't conda/pip/jar packages (academic script collections). Clones into `{env}/share/{tool}`, pins the commit SHA, optional build + smoke verify. Sets install_method.type=source; anchored by I11. **Pin `ref` to a tag/commit** — a bare default branch drifts |
| `install_release_binary(env, tool_name, url, sha256?, binary_in_archive?, wrapper_name?, verify_command?, pipeline_id)` | Precompiled static binaries on GitHub releases / vendor URLs (mosdepth, somalier, sylph, dorado, cellranger). Download → sha256-anchor (mismatch = hard fail) → extract → chmod → PATH wrapper. A smoke verify catches a wrong-arch binary. Sets install_method.type=binary; anchored by I14 |
| `install_perl_package(env, module, distribution?, cpanm_flags?, pipeline_id)` | Perl/CPAN modules (Ensembl VEP, BioPerl) via cpanm. Requires `perl` + `perl-app-cpanminus` in the env. Verifies `perl -M{module} -e1`. Sets install_method.type=perl |
| `install_cargo_tool(env, crate, version?, binary_name?, git_url?, pipeline_id)` / `install_go_tool(env, package, version?, binary_name?, pipeline_id)` | Rust / Go tools not on bioconda. `cargo install --root {env}` / `GOBIN={env}/bin go install`. cli_which is the anchor (locally built ⇒ can't be wrong-arch). **Prefer conda when available** (many are on bioconda); these are the fallback |
| `freeze(env, tools, pipeline_name?, platform?, accel?, gated?, push_target?, pipeline_id)` | Freeze the env into a content-addressed, HPC-shippable artifact. request_key→EnvCache lookup (hit = proven artifact by hash, no re-solve). ADOPT-or-BUILD by what the env actually contains: **pure conda + a published BioContainer → ADOPT by digest**; **any non-conda install (binary/source/…) → RECIPE BUILD** that replays the installs on the ship platform (release binaries re-fetched from the SAME release's linux asset, sha256-anchored, smoke-tested in-image — a foreign biocontainer is NOT adopted, it would ship a different unvalidated artifact); **pure conda, no biocontainer → conda-pack build only when host arch == ship arch** (a cross-arch conda-pack is refused, not shipped). Records `shipped_binaries[]` (the ship-platform hash vs the host-validated hash) + `build_method`. Emits the Apptainer HPC delivery contract (registry-free `docker save`→`apptainer build docker-archive` by default; gated ⇒ tarball-only per I13). `tools` = the PRIMARY requested tools |
| `generate_user_guide(pipeline_id\|spec, freeze_request_key?)` | Layer-2 deliverable: render a runnable Markdown guide from the pipeline's PASSING, validated run — every command shown was executed + validated (only validated pipeline_steps + a self-tested usage template). `freeze_request_key` pins the env BY DIGEST (Apptainer delivery + content/image digests) from the EnvCache. Writes `env_reports/{name}.GUIDE.md` |
| `seal_workflow(pipeline_id, freeze_request_key, workflow_name?, description?)` | **Layer-2 seal.** Validate the run-side invariants (I0/I3/I6/I7/I8 — env-build invariants are Layer 1's), self-test the `usage.command_template` (sets `usage_verified`), PIN the frozen env BY DIGEST, render the guide, write a `WorkflowSpec` (`{name}.workflow.yaml` + `{name}.GUIDE.md`). The WorkflowSpec carries its external input sources (`test_data`/`reference_databases`/`runtime_configs`/`authored_artifacts`) so it is **self-verifying** — its I8 re-checks against the artifact alone, not the draft it was sealed from; seal re-validates the constructed spec and refuses if it wouldn't pass standalone. Sets `validated_in_shipped_image` when every validated step ran in the pinned env image (digest-matched via `run_step_in_container`) — i.e. **validated == shipped**. Call `freeze()` first. Refuses on any workflow-invariant violation |
| `download_reference_database(name, url, local_path, pipeline_id)` | Large DBs (>100 MB). Watchdog-safe via run_in_background. Auto-records ReferenceDatabase entry |
| `run_pipeline_step(env, command, pipeline_id, inputs, output_types?, watch_dir?)` | Run + auto-validate every detected output in one call (runs on the HOST conda env) |
| `run_step_in_container(freeze_request_key, command, pipeline_id, inputs, output_types?, watch_dir?, data_dir?, extra_mounts?)` | **The validation-locus pivot: validated == shipped.** Run + auto-validate a step INSIDE the frozen env image (resolved from the EnvCache by `freeze_request_key`; adopted images pulled local). Bind-mounts `data_dir` at its own host path so existing absolute paths work verbatim and outputs land back on the host for validation; captures I7 IN the container (GNU `time -v` → exact peak RSS, host `docker stats` fallback); stamps the step `ran_in_container` + `container_image[_digest]`. Use this (not run_pipeline_step) once frozen, so the recorded run is the one that actually executes on HPC. `seal_workflow` then asserts `validated_in_shipped_image` when every validated step's digest matches the pinned env's |
| `stage_authored_artifact(pipeline_id, path, role, description, content?, generated_by?, language?)` | Any time the agent writes a file outside MCP — driver scripts, synthetic test data, hand-staged BAM/VCF/FASTA, transformed configs. Records content verbatim (text) or genesis command (binary) + sha256. Without this, the artifact's path is an orphan to I8 and finalize fails. |
| `start_service(env, service_name, start_command, health_check_command, pipeline_id, service_type?, port?, …)` | Service-dependent tools (Redis, Postgres, web server, Spark). Starts background process, polls until healthy, registers a ServiceDependency record + appends the readiness probe to its health_check_log. Satisfies I10 if the start succeeds. |
| `verify_service_dependency(pipeline_id, service_name, env_name, health_check_command?)` | Append an additional health probe to a declared service's log (mid-pipeline checkpoint, recovery after a flap, manual re-verify). |
| `stop_service(env_name, service_name, stop_command?, pipeline_id?)` | Stop a background service; marks status=stopped + stopped_at when pipeline_id is supplied. |
| `phenopacket_to_vcf(phenopacket_id, output_vcf)` | Materialize a single-sample VCF from a phenopacket — eliminates hand-VCF synthesis |

Below the primitives there are still lower-level tools (`run_in_env`, `validate_output`, `verify_installation`, `patch_pipeline`, etc.) — use them when a primitive doesn't fit. Prefer the primitive when it does.

### Two layers — environment vs. workflow

The re-spine separates two lifecycles. **Layer 1 — the environment** is solved *once*, frozen, and content-addressed: build it with the install primitives, then `freeze()` produces a digest-addressed, HPC-shippable artifact registered in the EnvCache (a later identical request returns it by hash, no re-solve). **Layer 2 — the workflow** *consumes* a frozen env by digest: `seal_workflow()` validates the run-side invariants, pins the env, and writes a `WorkflowSpec` + a user guide rendered from the passing run. The env is the reusable "solved component"; workflows are the run-many, per-experiment artifacts on top of it. (The combined `finalize_pipeline` path still exists and coexists during the re-spine.)

**Validated == shipped.** Once an env is frozen, run the workflow's steps with `run_step_in_container` (not `run_pipeline_step`) so they execute INSIDE the shipped image. This collapses the host-validate/ship-build split that otherwise forces per-tier cross-arch replay: the bytes the user runs on HPC are the exact bytes we validated, and `seal_workflow` records that as `validated_in_shipped_image` (digest-matched). The host `run_pipeline_step` remains for pre-freeze iteration. (Freeze itself is platform-honest: it ADOPTs a biocontainer only for pure-conda envs, RECIPE-BUILDs the ship-platform image for any non-conda install, and refuses a cross-arch conda-pack — see the freeze row.)

---

## Protocol

The full install flow, in order:

1. **`start_pipeline(name, description)`** — returns `pipeline_id`. Thread it through every subsequent call.
2. **Compose primitives** to build the env. Conda first; then R / pip / JAR; then `download_reference_database` for any large external data. Each primitive auto-merges its install_step into the draft.
3. **`select_test_data(...)`** — pick a dataset from `data/core_test_data_hg38/manifest.yaml` (or generate one with `phenopacket_to_vcf` or a small Rscript / Python).
4. **`run_pipeline_step(...)`** — actually run the tool against test inputs. Every detected output is auto-validated. For multi-step pipelines, call this N times; `depends_on` is derived from input/output overlap at finalize.
5. **`patch_pipeline(pipeline_id, {usage: {...}, notes: [...], runtime_environment: {...}, ...})`** — fill the fields no tool provides. Most important is **`usage`**: command_template with `{PLACEHOLDER}` slots, inputs[].format, outputs[].files globs. This is the contract the spec self-tests against.
6. **`build_docker_image(env, name, ..., pipeline_id)`** — BEFORE finalize. Finalize deletes the draft; a post-finalize call returns `unknown_pipeline_id`.
7. **`validate_pipeline_draft(pipeline_id)`** — dry-run finalize. Surfaces invariant violations and schema errors without committing.
8. **`finalize_pipeline(pipeline_id)`** — runs the invariant check + self-tests `usage.command_template` + writes 4 artifacts. Refuses to write if anything fails.
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
- `envs/bioinf_{name}/` — the conda env
- `env_reports/{name}_{version}.yaml` — the structured spec (machine-verified)
- `env_reports/{name}_{version}.html` — human-readable report
- `env_reports/{name}_{version}.environment.yml` — portable conda recipe
- `env_reports/{name}_{version}.lock` — bit-exact lock (sha256 recorded in spec)
- `docker_images/{name}/` — Dockerfile + conda-pack tarball

---

## Schema cheatsheet (avoid finalize rejection)

- `notes`: `list[str]` (a bare string is auto-wrapped)
- `docker.volume_mounts`: `list[str]`, each `host:container[:mode]` — informational only
- `runtime_configs[*].format`: `yaml | properties | java_properties | ini | json | xml | tsv | txt`
- `OutputFile.type`: see the FileType union in `agent/models/core_data.py` (includes `jsonl`, `bedgraph`, `methylation_report`, `sqlite`, etc.)
- `install_method.type`: `conda | jar | pip | r_install | docker_pull | source | manual`
- `runtime_environment.type`: `conda | jar | r | docker | native`
- `usage.trials[*]`: `{name, substitutions: {PLACEHOLDER: abs_path}, description?}` — declare one trial per input shape (paired-gz, single-uncompressed, …) so I4 proves multi-shape coverage. Empty list ⇒ single inferred trial (backward-compatible).

If a patch lands shape that doesn't fit, `validate_pipeline_draft` surfaces the exact field + reason — fix and retry.

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

`pytest tests/` — every finalized spec must pass invariants; sanity tests verify the gate itself catches unverified packages, silent-empty-success steps, relative paths.
