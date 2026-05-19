# Bioinformatics Agent

Installs bioinformatics tools into isolated conda environments, validates them against test data, and packages them as HPC-compatible Docker images.

## How it works

Claude Code drives all orchestration directly using your subscription. The MCP server is registered in `.claude/settings.json` and starts automatically — no separate API credits required.

```bash
pip install -r requirements.txt
./scripts/setup_core_test_data.sh   # one-time bootstrap: conda envs + reference genome + 4 core datasets
```

Then just talk to Claude Code:
```
install latest samtools
install bwa_samtools and freebayes as my wgs_variant_pipeline
what test data is available?
what pipelines have been installed?
```

If you ever need an API-driven orchestration mode (e.g., headless batch installs), the underlying skills under `agent/skills/` are the substrate — wire them into a Claude SDK or Anthropic client loop. The earlier `agent/main.py` + `agent/tools.py` CLI was retired in favor of the MCP path; the git history has a reference implementation if needed.

---

## MCP tools available to Claude Code

| Tool | What it does |
|------|-------------|
| `search_package` | Find package on bioconda/conda-forge/PyPI. **Query-only** — populates `draft.search_cache` for description/homepage annotation, not `draft.packages` (which is derived at finalize from the live env). |
| `create_conda_env` | Create isolated conda env |
| `install_packages` | conda install one or more packages |
| `verify_installation` | Run a custom version/help command for a package. **Advisory** — result is cached in `draft.verifications` and stitched onto the derived package record at finalize. Skipping this no longer drops `env_status`. |
| `run_in_env` | Run any shell command inside the conda env |
| `validate_output` | Check output files are valid (bam/vcf/fastq/gfa/bim/ld/…). Single (`file_path`) or batch (`files=[{path, expected_type}, ...]`) shape — batch validates each file independently. |
| `list_available_resources` | What genomes and test data are on disk |
| `download_resource` | Download a reference genome |
| `add_core_test_data` | Stream-download + subset reads from EBI SRA |
| `build_docker_image` | conda-pack → HPC Docker image (GPU-aware) |
| `check_gpu` | Detect NVIDIA GPU availability and CUDA version |
| `start_service` | Start a background service (web server, DB, Spark) inside the env |
| `stop_service` | Stop a background service by stop_command or PID file |
| `check_service_health` | Probe a running service with a health-check command |
| `save_pipeline_report` | Write YAML + HTML report to env_reports/ (use `finalize_pipeline` instead when using the accumulator) |
| `write_pipeline_provenance` | Write provenance YAML for a pipeline run |
| `list_installed_pipelines` | List installed pipelines from env_reports/ (drafts are skipped) |
| `fetch_r_package_deps` | Parse DESCRIPTION from a GitHub R repo; returns all deps pre-categorised |
| `start_pipeline` | Initialize (or resume) a server-side draft; returns `pipeline_id` |
| `finalize_pipeline` | Validate the draft against PipelineSpec, write final YAML + HTML, delete draft |
| `discard_pipeline_draft` | Delete a draft without finalizing (fresh start) |
| `show_pipeline_draft` | Inspect the current draft without finalizing |
| `patch_pipeline` | Deep-merge arbitrary patches into the draft (escape hatch). `pipeline_steps` / `install_steps` are merged by `step` field, not replaced. |
| `select_test_data` | Find a matching test dataset; returns a TestDataRef shape ready for the spec |
| `run_install_command` | Mirror of `run_in_env` for install commands (BiocManager, install_github, pip install, …). Routes to `install_steps`. Optional `verify_command` runs after the install in the same env — if it fails the step is recorded as failed (catches silent-failure cases like R install printing "ERROR: lazy loading failed" with rc=0). |
| `mark_step_validated` | Set `validation_status` on a `pipeline_step` (when outputs are known good but `validate_output` wasn't called). **Refuses `passed` on a step with no detected outputs and no validation** — the silent-empty-success guard. |
| `install_jar_tool` | One-shot Java tool install (Exomiser, Picard, GATK, snpEff). Downloads JAR (or distribution .zip), writes wrapper at `{env}/bin/{tool}`. Sets `install_method: {type: jar, jar_path, wrapper_script, java_flags}` automatically. |
| `phenopacket_to_vcf` | Materialise a single-sample VCF from a registered phenopacket's variant block — eliminates hand-writing a VCF for Exomiser-style installs. |
| `validate_pipeline_draft` | Dry-run finalize: validate the draft against PipelineSpec without writing artifacts. Use before `finalize_pipeline` to catch schema problems early. |
| `run_in_background` | Spawn a long-running shell command in the background (watchdog-proof — see Async patterns below). Returns `{job_id, status_path, log_path}` immediately. |
| `check_job` | Poll status of a background job. ~50 ms; returns `{state, returncode, bytes_logged, elapsed_seconds, log_tail}`. |
| `cancel_job` | Terminate a background job (SIGTERM, then SIGKILL after 5 s; `force=True` skips straight to SIGKILL). Signals the whole process group. |
| `list_jobs` | List every job ever started; `include_terminated=False` filters to only running. |
| `add_phenopacket` | Download and register a GA4GH phenopacket JSON. GitHub blob URLs auto-normalize to raw. |

---

## How to install a pipeline (phases Claude Code follows)

When the user asks to install a tool or pipeline, execute ALL phases in order using the **pipeline state accumulator**. This eliminates the spec-assembly burden: each tool's output that has an obvious destination is merged into a server-side draft automatically. You pass `pipeline_id` to participating tools and they handle the rest.

### Phase 0 — Start
- Call `start_pipeline(pipeline_name, description)` once at the very beginning. It returns `{pipeline_id, draft_path, resumed}`. **Use the returned `pipeline_id` (= the pipeline_name) on every subsequent participating tool.** If `resumed: true`, a draft already existed — call `show_pipeline_draft` first to see what was already done before deciding whether to continue or `discard_pipeline_draft` for a fresh start.

### Phase 1 — Research
- Call `search_package(name, version, pipeline_id=<id>)` for each requested package. **A PackageRecord-shaped entry is appended to `draft.packages` automatically** — you do not need to re-state the result anywhere. The return value still contains the data so you can reason about channel/version choices.

### Phase 2 — Install
- Call `create_conda_env(env_name="bioinf_{pipeline_name}", pipeline_id=<id>)`. Sets `draft.conda_env` AND appends an entry to `draft.install_steps` for the env creation.
- **For conda tools** (the default): call `install_packages(env_name, packages, pipeline_id=<id>)`. Installs all packages in one solve AND appends a single entry to `draft.install_steps` with `installed_packages` parsed from each spec (e.g. `samtools=1.21` → `{name: samtools, version: 1.21}`). Pass `step=N` to replace install_step N after a solver conflict (same retry semantics as `run_install_command`) — otherwise failed attempts accumulate and drop `env_status` to `failed`.
- **For Java tools** (Exomiser, Picard, GATK, snpEff, …): use `install_jar_tool` —
  it does the openjdk-assumed download + unzip + wrapper-script in one call and
  sets `install_method: {type: jar, jar_path, wrapper_script, java_flags}`
  automatically. The conda env must already have `openjdk` (and `unzip` if the
  URL is a .zip): include them in your prior `install_packages` call.
  ```python
  install_jar_tool(
    env_name="bioinf_exomiser",
    tool_name="exomiser",
    jar_url="https://github.com/exomiser/Exomiser/releases/download/15.0.0/exomiser-cli-15.0.0-distribution.zip",
    java_flags=["-Xmx6g", "-Xms2g"],
    pipeline_id=pid,
  )
  ```
  Still patch the spec's `runtime_environment` separately:
  `patch_pipeline(pid, {"runtime_environment": {"type": "jar", "java_flags": [...], "jar_path": "...", "wrapper_script": "..."}})`.
  Conda-pack will bundle the JVM + JAR + wrapper — the Docker image is self-contained, no `module load`.
- **For database-heavy tools** (tools needing >1 GB reference data beyond the genome FASTA):
  - Download via `run_in_background` to avoid the 600 s watchdog (see Async patterns below).
  - Add a `ReferenceDatabase` entry to the spec with `name`, `version`, `size_gb`, `source_url`, `local_path`.
  - The data directory is **not** baked into the Docker image. Users mount it at runtime.
- Call `verify_installation(env_name, package_name, check_command, pipeline_id=<id>)` for each package. The package's record in `draft.packages` is patched with `verify_command` + `verify_output` automatically. This is what powers `env_status: fully_validated` at finalize.
- **For non-conda install commands** (BiocManager::install, remotes::install_github, pip install, JAR downloads via curl/wget for Java tools like Exomiser/Picard/GATK, reference DB downloads, etc.): call `run_install_command(env_name, command, installed_packages=[...], pipeline_id=<id>, ...)`. Same shape as `run_in_env` but routes to `install_steps`. Each entry in `installed_packages` (e.g. `{"name": "GAPIT", "channel": "github", "source": "remotes::install_github('jiabowang/GAPIT')"}`) becomes a package record in the final spec — derived at finalize from the union of successful `install_steps`. `name` and `channel` are what matter; `version` is optional (finalize probes the env). Pass `step=N` to replace a failed install step for retries.

### Phase 3 — Test data
- Call `list_available_resources(both)` to see what's on disk.
- Canonical test data lives at `data/core_test_data_{genome_build}/`:
  - Genome + indexes: `core_test_data_hg38/genome/chr22.fa` (+ .fai, bwa indexes)
  - Reads (bootstrapped by `setup_core_test_data.sh`):
    - Exome PE 10K:   `short_read/paired_end/exome/HG00096_SRR1517830_10K_R{1,2}.fastq.gz`
    - RNA-seq SE 10K: `short_read/single_end/rnaseq/airway_SRR1039508_10K_R1.fastq.gz`
    - RNA-seq PE 10K: `short_read/paired_end/rnaseq/NA20503_ERR188297_10K_R{1,2}.fastq.gz`
    - Hi-C PE 10K:    `short_read/paired_end/hic/GM12878_SRR1658581_10K_R{1,2}.fastq.gz`
    - WGS PE 10K:     `short_read/paired_end/wgs/NA12878_ERR001268_10K_R{1,2}.fastq.gz`
    - WGBS PE 10K:    `short_read/paired_end/wgbs/ENCSR890UQO_SRR4235788_10K_R{1,2}.fastq.gz`
    - ONT WGS 500:      `long_read/ont/ont_wgs/NA12878_ERR3152364_500_R1.fastq.gz` (best-effort)
    - PacBio HiFi 500:  `long_read/pacbio/pacbio_hifi/HG002_HG002_CCS_15kb_500_R1.fastq.gz` (best-effort, NCBI FTP)
  - Phenopackets (GA4GH v2 JSON; `add_phenopacket` extracts HPO/disease/variants):
    - `phenopackets/PMID_30315159_Patient_N.json` — ACTB / Thrombocytopenia 8 (OMIM:620475)
  - Pre-built pipeline outputs: `core_test_data_hg38/pipeline_outputs/{pipeline}/`
  - Read `core_test_data_hg38/manifest.yaml` to discover exactly what is available.
- Default strategy: use chr22 reference, 10K reads, write outputs to `data/{pipeline_name}_test_data/`.
- If genome index is missing for this tool, build it with `run_in_env` before the main test run.
- Only call `download_resource` if the needed genome is not already on disk.
- Call `select_test_data(genome_build, assay_type, end_type, pipeline_id=<id>, ...)` to find a matching dataset. **A TestDataRef-shaped dict is set on `draft.test_data` automatically.** Inspect the returned `test_data` and `match_score`; if the match is wrong, call again with different criteria or use `patch_pipeline` to override.

### Phase 4 — Algorithm / Pipeline runs
This is where the actual analysis runs (alignment, variant calling, GWAS, etc.). Each step here lands in `draft.pipeline_steps` — distinct from `install_steps` from Phase 2.

For each algorithmic step in pipeline order:
- Build a test command with sensible defaults for small data. Use absolute paths.
- Call `run_in_env(env_name, command, inputs=[...], watch_dir=<abs>, pipeline_id=<id>, tool="...", subcommand="...", purpose="...")`. **A PipelineStep is appended to `draft.pipeline_steps`** with the command, returncode, runtime_seconds, inputs, and detected outputs. The return value's `pipeline_merge.step_index` is the 1-based step number — capture it for the validate_output calls.
- **Structured inputs**: every file the step *reads* belongs in `inputs`, including files a wrapper script then opens. Bare path strings still work, but when an input is a script / config / wrapper, use the dict form so the secondary files become first-class spec citizens (and downstream Nextflow channel declarations):
  ```python
  inputs=[
      {"path": "/abs/run_gapit.R",
       "references": ["/abs/genotype.hmp.txt.gz", "/abs/traits.txt.gz"]},
      "/abs/some_other_input.tsv",   # plain string for direct args
  ]
  ```
  The HTML report renders references as an indented sublist under their parent input. Skipping this is the difference between "the script is the input" (opaque) and "the script + its data are the inputs" (programmatic lineage).
- Call `validate_output(files=[{path, expected_type}, ...], env_name=<env>, pipeline_id=<id>, step=<step_index>)` once with **every file in `result.detected_outputs`** — batched, per-file errors are independent. Each result merges into `draft.pipeline_steps[step].validation[basename]` automatically. The single-file `file_path=..., expected_type=...` shape is preserved for ad-hoc one-off validations. Don't cherry-pick: a detected output that isn't validated drops `pipeline_status` below `fully_validated`.
- `result.detected_outputs` is a list of **absolute paths** — use them directly. Subdirectory structure is preserved (Flye writes to `00-assembly/`, `20-repeat/`, etc.; those subdir paths are kept). Pass the same paths to `validate_output` without manual joining.
- **If you know a step's outputs are good but `validate_output` doesn't apply** (e.g., a step's success is confirmed by the next step running cleanly): call `mark_step_validated(pipeline_id, step, validation_status="passed")`. This is the explicit alternative to `validate_output` for cases without checkable output files.
- Pass `result.detected_outputs` as `inputs` to the next `run_in_env` call (full lineage).
- On failure, diagnose and retry. To **replace** a failed step instead of appending a new one, pass `step=<failed_step_index>` to `run_in_env`. Default behavior is append (preserves history).
- **Why this matters**: `pipeline_status` only lands on `fully_validated` if every pipeline_step has `validation_status="passed"`. A step that exits 0 but never gets a `validate_output` (or `mark_step_validated`) call counts as unvalidated and drops the pipeline to `partially_validated` or `complete`. Same logic applies to `env_status`: it's `fully_validated` only when every non-conda-pack package in `draft.packages` has a `verify_output` recorded.

### Phase 5 — Docker
- Call `build_docker_image(env_name, pipeline_name, pipeline_description, version=<primary_version>, pipeline_id=<id>)`. **`draft.docker` is set to the DockerBuild-shaped subset of the return automatically.**
- **Call this BEFORE `finalize_pipeline`.** `finalize_pipeline` deletes the draft, so a post-finalize `build_docker_image` call with `pipeline_id` returns `unknown_pipeline_id` and the `docker:` block never lands in the spec. The Dockerfile + tarball still get written to `docker_images/`, but the spec won't know about them. If you forgot and already finalized: re-run `build_docker_image` without `pipeline_id` to just rebuild the image (no spec mutation).

### Phase 6 — Fields the accumulator doesn't fill (escape hatch)
Some fields don't come from any tool — fill them with `patch_pipeline(pipeline_id, patches)` before finalize:
- `runtime_environment` — only if non-conda (e.g. `{type:"jar", java_flags:[...], jar_path:"..."}`)
- `reference_databases` — `[{name, version, size_gb, source_url, local_path}, ...]` for large external DBs
- `runtime_configs` — `[{name, format, path, content?}, ...]` for global config files
- `service_dependencies` — for tools that need a companion process (web server, DB, Spark)
- `notes` — any observations worth recording in the final spec
- `reference_free: true` — for de novo assemblers
- **`usage`** — the canonical "run on new data" contract. Required for the spec to be useful to downstream callers (Nextflow generator, the SRA agent driving mass pipeline generation). Take `pipeline_steps[-1].command`, parameterise its inputs/outputs with `{PLACEHOLDER}` slots, and patch as:
  ```python
  patch_pipeline(pipeline_id, {"usage": {
    "description": "Run GAPIT GLM on a HapMap genotype matrix and quantitative traits",
    "command_template": "Rscript run.R {INPUT_GENOTYPE} {INPUT_TRAITS} {OUTPUT_DIR}",
    "inputs":  [{"name": "INPUT_GENOTYPE", "format": "hapmap",  "description": "..."},
                {"name": "INPUT_TRAITS",   "format": "tsv",     "description": "..."}],
    "outputs": [{"name": "OUTPUT_DIR", "description": "...",   "files": ["GAPIT.Association.GWAS_Results.*.csv"]}],
    "example": "..."   # optional concrete invocation
  }})
  ```

### Patch-pipeline schema cheatsheet (avoid finalize rejection)
- `notes`: `list[str]` (a bare string is auto-wrapped to `[string]` for safety)
- `docker.volume_mounts`: `list[str]`, each a `host:container[:mode]` triple — purely informational, runtime users mount whatever they want with `docker run -v`
- `runtime_configs[*].format`: one of `yaml | properties | java_properties | ini | json | xml | tsv | txt`
- `OutputFile.type`: see the FileType union in `agent/models/core_data.py`. Notable additions: `jsonl`, `ndjson` (for Exomiser-style streaming JSON)
- `install_method.type`: `conda | jar | pip | r_install | docker_pull | source | manual`
- `runtime_environment.type`: `conda | jar | r | docker | native`

Call `validate_pipeline_draft(pipeline_id)` BEFORE `finalize_pipeline` to dry-run the schema check — it returns `{valid: bool, validation_errors?: [...]}` without writing artifacts or deleting the draft. Saves the repair-finalize-repeat loop.

### Phase 7 — Finalize
- Call `finalize_pipeline(pipeline_id)`. This:
  1. **Rebuilds `spec.packages` from the live env** — the env itself is the single source of truth. `conda env export --from-history` provides the explicitly-requested conda packages (transitive deps are kept in the lock file, not the spec). Every `installed_packages` entry from successful `install_steps` becomes a non-conda package record (R via BiocManager/install_github, JARs, pip wheels, custom downloads). Versions come from `conda list --json` and `Rscript packageVersion('X')`. Homepages are templated from `(channel, name, source)`. **Result**: you don't have to remember to pass `version` in `installed_packages` or manually patch homepages — both are derived. `r-*` conda packages get a `runtime_version` field showing the R-side number alongside the conda recipe version.
  2. **Derives `pipeline_steps[*].depends_on`** from input/output overlap when not set explicitly. This is the DAG a Nextflow generator (or parallel scheduler) consumes downstream.
  3. Validates the populated draft against `PipelineSpec`.
  4. If valid: writes four artifacts to `env_reports/`:
     - `{name}_{version}.yaml` — the structured spec
     - `{name}_{version}.html` — human report
     - `{name}_{version}.environment.yml` — portable conda recipe (`--from-history`); recreate the env on any platform
     - `{name}_{version}.lock` — `conda list --explicit` for bit-exact recreation on the same arch
     Returns `{saved_yaml, saved_html, saved_env_yml, saved_lock, env_status, pipeline_status}`.
  5. If invalid: preserves the draft, returns `{error, validation_errors, draft_path}` — use `show_pipeline_draft` to inspect, `patch_pipeline` to fix, then retry `finalize_pipeline`.
- **Capture `saved_yaml` from the return** — this is `pipeline_spec_path` needed in Phase 8.
- **`env_status` model**: `fully_validated` requires all `install_steps` exited 0 AND the derived packages list is non-empty. Per-package `verify_installation` calls are no longer gating — they're advisory enrichment for the report. `pipeline_status` still requires every `pipeline_step` to have `validation_status: passed` (or `mark_step_validated`).

### Phase 8 — Provenance
- Call `write_pipeline_provenance` with the exact output files produced.
- Pass `pipeline_spec_path` = `saved_yaml` returned by Phase 7.
- Pass all other absolute paths; relative paths inside the YAML are computed automatically.
- Always required: `pipeline`, `conda_env_path`, `pipeline_spec_path`, `output_files`, `output_dir`, `sample_key`.
- `genome_build` / `chromosome` / `reference_path` are optional — omit for tools that don't use a reference FASTA.
- Input types (pass at least one):
  - `reads` → `{r1, r2?, sample, accession, subset, num_reads, assay_type, end_type, database}`
  - `bam_input` → `{bam, bai}`
  - `vcf_input` → `{vcf, tbi?, genome_build, upstream_pipeline?, sample_ids?}`
  - `assembly_input` → `{assembly: <path>, upstream_pipeline?}` — draft contig FASTA as input (scaffolders/polishers)
  - `phenotype` → `{ontology?, terms: [...HPO ids...], source?}` — HPO/GO/DOID ontology-coded disease terms
  - `pedigree` → `{ped, proband?}`
  - `genotype_array` → `{file, format: hapmap|plink_bed|vcf|dosage|bgen, bim?, fam?, n_samples?, n_snps?, genome_build?, upstream_pipeline?}` — population-level genotype matrix for GWAS/QTL tools
  - `quantitative_traits` → `{traits: [...], file, n_samples?, measurement_type: continuous|binary|ordinal}` — continuous phenotype measurements (distinct from HPO-coded phenotype)

### Accumulator is opt-in
All `pipeline_id` parameters are optional. When omitted, tools behave exactly as before — no merge, no draft side effects. This is correct for ad-hoc operations: running a one-off command, debugging a tool, validating a single file, or anything outside an install flow. For installs, always start with `start_pipeline` and thread the `pipeline_id` through.

### Rules
- Always use absolute paths in `run_in_env` commands.
- Prefer bioconda > conda-forge > defaults channel priority.
- conda-pack is added to every env automatically by `install_packages`.
- For tools that link against htslib (samtools, bcftools, bwa), install them in one `install_packages` call for compatible dependency resolution.
- Always pass `env_name` to `validate_output` so samtools/bcftools resolve from inside the pipeline env.
- **Java tools**: always install `openjdk` (conda-forge) into the conda env — never rely on system Java. This ensures `conda-pack` bundles the JVM and the Docker image is self-contained.
- **Large reference databases** (>1 GB, tool-specific): document in `reference_databases` and add to `docker.volume_mounts`. Do NOT embed them in the Docker image.
- **Config-file-driven tools**: write config files with `run_in_env`, then record them in `runtime_configs` (global) or `PipelineStep.config_files` (per-step) so they are captured in the spec.

---

## R tools (GAPIT, DESeq2, limma, custom packages, …)

R packages can come from four sources. Always prefer the conda-packaged version — it is pre-compiled, version-pinned, and fully bundled by conda-pack.

### Install priority
1. **conda-forge** — `r-{lowercase-pkg-name}` (e.g. `r-ggplot2`, `r-data.table`). Use `install_packages` as normal.
2. **bioconda** — `bioconductor-{lowercase-pkg-name}` (e.g. `bioconductor-deseq2`). Use `install_packages` with `-c bioconda`.
3. **CRAN** — for packages not on conda. Use `run_in_env` with Rscript (see template below).
4. **Bioconductor via BiocManager** — for Bioc packages not on bioconda. Use `run_in_env`.
5. **GitHub via remotes** — always last, always with `dependencies=FALSE` after all deps are pre-installed.

### R library isolation (critical)
`conda run` inherits the parent shell's `R_LIBS_USER`, which can silently redirect `install.packages()` to `~/R/…` outside the conda env. **Always** derive the library path from `CONDA_PREFIX` (set reliably by `conda run`):

```bash
# Template for all R install commands inside run_in_env:
Rscript -e "
  lib <- file.path(Sys.getenv('CONDA_PREFIX'), 'lib', 'R', 'library')
  install.packages(c('pkg1', 'pkg2'), lib=lib, repos='https://cloud.r-project.org')
"

# Bioconductor:
Rscript -e "
  lib <- file.path(Sys.getenv('CONDA_PREFIX'), 'lib', 'R', 'library')
  BiocManager::install(c('snpStats', 'DESeq2'), lib=lib, ask=FALSE)
"

# GitHub (always last, after all deps are installed):
Rscript -e "
  lib <- file.path(Sys.getenv('CONDA_PREFIX'), 'lib', 'R', 'library')
  remotes::install_github('owner/repo', lib=lib, dependencies=FALSE)
"
```

### Pre-discovering GitHub deps (friction-free installs)
Before installing any R package from GitHub, call `fetch_r_package_deps(github_repo)`. It fetches and parses the DESCRIPTION file and returns:
- `imports`, `depends`, `suggests`, `linking_to` — all dep name lists
- `all_required` — union of imports + depends + linking_to (what must be present before the GitHub install)
- `undeclared_runtime_installs` — packages that appear in `BiocManager::install("...")` / `install.packages("...")` / `requireNamespace("...")` / `library("...")` calls in the package's R/ source but are NOT in DESCRIPTION. These are common sources of install failure (the package tries to bootstrap them at load time). Best-effort scan — custom helper functions (e.g. GAPIT's `.gapit_require_or_install`) won't be caught.
- `install_strategy` — concrete ordered steps

Install order from the strategy:
1. Call `search_package` for each dep to find what's on conda-forge (`r-{pkg}`) or bioconda (`bioconductor-{pkg}`). Install all confirmed conda packages in one `install_packages` call.
2. For anything not on conda, use `BiocManager::install()` — it is the authoritative resolver for both CRAN and Bioconductor packages and needs no pre-categorisation on our side.
3. GitHub install last, with `dependencies=FALSE`.

**Always pass `verify_command` to `run_install_command` for R installs** — R's
`install.packages` and `remotes::install_github` swallow load errors into
warnings, so an `Rscript -e "remotes::install_github('...')"` can print
`ERROR: lazy loading failed for package 'X'` and still exit 0. The
`verify_command` runs after the install in the same env; non-zero exit
flips the step to failed:
```python
run_install_command(
  env_name=env, command=install_cmd, installed_packages=[...],
  verify_command="Rscript -e 'if(!requireNamespace(\"X\")) quit(status=1)'",
  pipeline_id=pid,
)
```

This prevents the multi-cycle discover-fail-retry pattern without requiring a hardcoded list of known packages.

### Compilation failures
If an R package fails to compile from CRAN or GitHub, it needs a system library. Install the library via conda (not apt/brew — it must be inside the env for conda-pack):

| R package | Missing system dep | conda spec |
|-----------|-------------------|------------|
| nloptr, lme4 | NLopt | `nlopt` (conda-forge) |
| curl, httr | libcurl | `libcurl` (conda-forge) |
| xml2, rvest | libxml2 | `libxml2` (conda-forge) |
| openssl | OpenSSL | `openssl` (conda-forge) |
| RPostgres | PostgreSQL client | `libpq` (conda-forge) |
| rJava | JDK | `openjdk` (conda-forge) |
| rgdal, sf | GDAL | `gdal` (conda-forge) |
| Cairo | Cairo | `cairo` (conda-forge) |

After conda-installing the system dep, retry the R package install via `run_in_env` using the template above.

### Spec fields for R pipelines
- `runtime_environment.type`: `"r"`, `r_version`: resolved version string
- Packages installed via CRAN/Bioc/GitHub: record in `packages` with `channel: "cran"`, `"bioconductor"`, or `"github"` and `install_method: {type: "r_install", source: "..."}`.

---

## Assembly tools (hifiasm, Flye, Canu, 3D-DNA, …)

Assembly pipelines differ from alignment pipelines in two key ways:

**De novo assemblers** (hifiasm, Flye, Canu, MetaFlye):
- Set `reference_free: true` in the spec — Phase 3 skips the reference genome step.
- Test with whatever long reads are available (PacBio HiFi 500-read or ONT 500-read subset from `core_test_data`). With only 500 reads the assembly will be highly fragmented — a valid GFA file with at least one S (segment) record is the acceptance criterion, not chromosome-length contigs.
- **Small-data parameter override**: hifiasm's defaults reject low-coverage data and emit zero-byte primary-contig GFAs. For the 500-read smoke test, relax to `-k 31 -w 31 -r 1 --min-hist-cnt 1 -n 1 -f 0` and validate the `*.bp.p_utg.gfa` unitig graph instead of the empty `p_ctg.gfa`. Record the defaults in `usage.command_template` (real-data users want defaults); record the relaxed flags in `notes` + the provenance `parameters` field. Other assemblers (Flye, Canu) have analogous quirks — start with defaults, fall through to permissive settings on empty output.
- Primary outputs are `.gfa` (assembly graph) and `.fa` (contig FASTA). Use `validate_output` with `expected_type: gfa` for GFA files. Convert GFA→FASTA with `gfatools gfa2fa input.gfa > output.fa`.
- Provenance uses `reads` as input. `genome` and `assembly_input` are both None.

**Reference-guided scaffolders / polishers** (3D-DNA, SALSA2, YaHS, Medaka, Pilon):
- Set `reference_free: false`.
- The 'reference' input is a **draft assembly** from a prior pipeline step (e.g. hifiasm contigs), not the canonical genome (chr22.fa).
- Use `assembly_input` in `write_pipeline_provenance` instead of a genome reference. Set `upstream_pipelines` to the assembler pipeline name.
- Additional reads (Hi-C, short-read) go in `reads` as normal.
- Output is a scaffolded/polished FASTA. Use `validate_output` with `expected_type: fasta`.

---

## GPU-accelerated tools (Clair3, DeepVariant, Parabricks, …)

1. **Phase 1**: call `check_gpu` early to know whether a GPU is available.
2. **Phase 2 — Install**: include `cudatoolkit` and `cudnn` in the `install_packages` call when the tool needs them (check the bioconda recipe). Always install the conda-packaged GPU tool directly — do not pull a Docker image.
3. **Phase 4 — Test**: if `check_gpu` returned `available: false`, run the tool in CPU fallback mode (most tools expose `--device cpu` or equivalent). Document this in the step's `purpose` field: "CPU fallback — no GPU on this machine".
4. **Phase 5 — Docker**: pass `gpu_required=true` and `cuda_version` (from `check_gpu` or the tool's bioconda page) to `build_docker_image`. The builder will use `nvidia/cuda:{version}-base-ubuntu22.04` automatically.
5. **Spec fields**: set `RuntimeEnvironment.gpu_required=true` and `cuda_version`. The resulting `DockerBuild.nvidia_runtime=true` signals to HPC users that `--gpus all` (Docker) or `--nv` (Singularity) is required at runtime.

---

## Service-dependent tools (OpenCRAVAT, Cromwell+MySQL, Hail, …)

Some tools require a companion process (web server, database, Spark driver) running during execution.

Pattern:
1. **Phase 2 — Install**: install the tool plus its service dependency (e.g. `mysql-server`, `spark`) via `install_packages` or `run_in_env`.
2. **Phase 4 — Validation loop**:
   a. Call `start_service` before the first step that needs it. Provide `start_command`, `health_check_command`, and a reasonable `health_check_timeout_seconds`.
   b. Run the pipeline step(s) normally with `run_in_env`.
   c. Call `stop_service` after the last step that uses it.
3. **Spec**: add a `ServiceDependency` entry to `service_dependencies` in the spec dict passed to `save_pipeline_report`. Fields: `type`, `name`, `version`, `start_command`, `stop_command`, `health_check_command`, `port`, `env_vars`, `data_dir`.
4. If `start_service` returns `success: false`, check the log path it returns, diagnose, and retry up to 2 times with adjusted commands before failing the install.

Typical health checks:
- Web server: `curl -sf http://localhost:{port}/`
- MySQL: `mysqladmin ping -h 127.0.0.1`
- Spark: `curl -sf http://localhost:4040/`

---

## Async patterns — for any operation that may run silently >5 minutes

The agent stream-watchdog kills a tool call that produces no stdout for ~600 s.
That makes `run_in_env` with a synchronous long-running command (multi-GB
download, hour-long conda solve, multi-hour assembly on real data) a hard
failure mode — the agent dies mid-step, the draft freezes, and any subprocess
the agent kicked off gets orphaned. **Use the background-job tools instead.**

```python
job = run_in_background(
    command="curl -L --progress-bar -o big.zip '{URL}' && unzip big.zip && rm big.zip",
    env_name="bioinf_x",
)
# job_id returned immediately. Poll periodically — each check_job is ~50ms.
while True:
    s = check_job(job["job_id"])
    if s["state"] != "running":
        break
    # Do unrelated work, or just continue the loop — agent stays alive because
    # check_job is constantly producing output.
final_state = s   # state="exited", returncode, elapsed_seconds, log_tail, bytes_logged
```

When to use:
- Reference-DB downloads over 100 MB (Exomiser bundles, Parabricks data, BLAST nt, full hg38)
- Long conda solves on dependency-heavy envs
- Real-data assemblies (hifiasm on full HiFi sets, Flye on full ONT)
- Anything else where you'd be tempted to use `curl --silent` or `unzip -q` (the original watchdog trap)

Quick rules of thumb for sync `run_in_env` (no async needed):
- Conda installs of < 10 packages on a fresh env
- Test runs on the 10K-read / 500-read subsets (under 30 s on most tools)
- Output validation

If you're not sure, prefer async — the cost is one extra `check_job` poll loop, the upside is you don't lose 20 min of work to a watchdog kill.

---

## How to add core test data

When the user asks to add test data:
1. Call `add_core_test_data` with the accession, assay_type, and any optional fields.
2. It streams reads from EBI SRA, subsets to the requested read count, writes a SampleMeta YAML, and rebuilds the manifest. Idempotent.
3. If the genome is missing afterwards, call `download_resource(genome, hg38_chr22)`.

---

## Project layout

```
agent/
├── mcp_server.py               # MCP server — sole orchestration entry point
├── skills/
│   ├── pipeline_state.py       # In-progress draft accumulator (disk-backed)
│   ├── spec_writer.py          # save_pipeline_spec + write_provenance
│   ├── package_search.py       # anaconda.org / PyPI lookup
│   ├── env_manager.py          # conda create / install / run
│   ├── test_runner.py          # Reference genome downloads
│   ├── core_test_data.py       # EBI SRA stream-download + subset
│   ├── docker_builder.py       # conda-pack → Docker image
│   ├── resources.py            # Manifest + installed-pipeline listing
│   └── report_builder.py       # HTML report generator
├── validators/
│   └── output_validator.py     # SAM/BAM/VCF/FASTQ/BIM/LD/… validation
└── models/
    └── core_data.py            # Pydantic models (Provenance, SampleMeta, PipelineSpec, …)
```

## Generated artifacts

| Location | What it is |
|----------|-----------|
| `envs/bioinf_{name}/` | Conda environment |
| `env_reports/{name}_{version}.yaml` | Pipeline spec: packages (derived from env), install steps, pipeline steps with DAG, validation, usage template |
| `env_reports/{name}_{version}.html` | Human-readable install + usage report |
| `env_reports/{name}_{version}.environment.yml` | Portable conda recipe (`conda env export --from-history`) — recreate the env on any platform with `conda env create -f` |
| `env_reports/{name}_{version}.lock` | URL-pinned explicit lock (`conda list --explicit`) — bit-exact env recreation on the same architecture |
| `docker_images/{name}/` | Dockerfile + conda-pack tarball |
| `data/core_test_data_{build}/` | Reference genome, reads, pipeline outputs |

## Configuration

Edit `config/agent_config.yaml` to change Docker base image, conda channels, default Python version, or agent model/timeouts.

## HPC / Singularity

Docker images are built `--platform linux/amd64`, no `USER` directive, `/data` as WORKDIR.

```bash
singularity pull bioinf_samtools.sif docker://samtools:1.21
```
