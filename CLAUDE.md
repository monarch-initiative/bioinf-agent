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

**patch_pipeline** is restricted to agent-authored keys: `description`, `notes`, `final_summary`, `conda_env`, `created_at`, `python_version`, `reference_free`, `runtime_environment`, `runtime_configs`, `reference_databases`, `usage`. Patches to `pipeline_steps`, `install_steps`, `packages`, `verifications`, `test_data`, `docker`, `authored_artifacts`, `service_dependencies`, or any derived status (`env_status` / `pipeline_status` / `docker_status` / `usage_verified` / `lock_sha256`) are **rejected** — those must flow through their dedicated primitive so the runtime is the sole producer.

`env_status` / `pipeline_status` / `docker_status` are derived, never agent-asserted. `docker_status` is anchored to `docker image inspect <tag>` at finalize, not the spec's claim — even if `docker.build_success` is truthy, status flips to `failed` if the image isn't present in the local daemon. `lock_sha256` is computed from a fresh `conda list --explicit` probe of the env at finalize.

Nothing in a finalized spec is taken on faith from the agent. The few free-form fields (`description`, `notes`) are clearly LLM-authored and explicitly *not* part of the verified surface.

---

## Primitives — the only tools the agent needs

Compose these. Each one absorbs the per-category knowledge that would otherwise live in prose here. The agent picks the right primitive; the primitive enforces its category's invariants internally.

| Primitive | When |
|-----------|------|
| `install_conda_packages(env, [{spec, channel}], pipeline_id)` | Anything on bioconda / conda-forge / defaults |
| `install_r_package(env, name, source, pipeline_id)` | source = `cran` \| `bioconductor` \| `github:owner/repo` — handles library isolation, BiocManager bootstrap, requireNamespace load-or-die |
| `install_pip_package(env, name, version?, pipeline_id)` | pip / PyPI — handles import check |
| `install_jar_tool(env, tool, jar_url, java_flags, pipeline_id)` | Java tools (Exomiser, Picard, GATK, snpEff) — auto-downloads, unpacks, writes wrapper, sets install_method.type=jar |
| `install_git_repo(env, repo_url, tool_name, ref?, build_command?, verify_command?, pipeline_id)` | Clone-and-run repos that aren't conda/pip/jar packages (academic script collections). Clones into `{env}/share/{tool}`, pins the commit SHA, optional build + smoke verify. Sets install_method.type=source; anchored by I11. **Pin `ref` to a tag/commit** — a bare default branch drifts |
| `download_reference_database(name, url, local_path, pipeline_id)` | Large DBs (>100 MB). Watchdog-safe via run_in_background. Auto-records ReferenceDatabase entry |
| `run_pipeline_step(env, command, pipeline_id, inputs, output_types?, watch_dir?)` | Run + auto-validate every detected output in one call |
| `stage_authored_artifact(pipeline_id, path, role, description, content?, generated_by?, language?)` | Any time the agent writes a file outside MCP — driver scripts, synthetic test data, hand-staged BAM/VCF/FASTA, transformed configs. Records content verbatim (text) or genesis command (binary) + sha256. Without this, the artifact's path is an orphan to I8 and finalize fails. |
| `start_service(env, service_name, start_command, health_check_command, pipeline_id, service_type?, port?, …)` | Service-dependent tools (Redis, Postgres, web server, Spark). Starts background process, polls until healthy, registers a ServiceDependency record + appends the readiness probe to its health_check_log. Satisfies I10 if the start succeeds. |
| `verify_service_dependency(pipeline_id, service_name, env_name, health_check_command?)` | Append an additional health probe to a declared service's log (mid-pipeline checkpoint, recovery after a flap, manual re-verify). |
| `stop_service(env_name, service_name, stop_command?, pipeline_id?)` | Stop a background service; marks status=stopped + stopped_at when pipeline_id is supplied. |
| `phenopacket_to_vcf(phenopacket_id, output_vcf)` | Materialize a single-sample VCF from a phenopacket — eliminates hand-VCF synthesis |

Below the primitives there are still lower-level tools (`run_in_env`, `validate_output`, `verify_installation`, `patch_pipeline`, etc.) — use them when a primitive doesn't fit. Prefer the primitive when it does.

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
