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
| `search_package` | Find package on bioconda/conda-forge/PyPI |
| `create_conda_env` | Create isolated conda env |
| `install_packages` | conda install one or more packages |
| `verify_installation` | Run version/help command to confirm install |
| `run_in_env` | Run any shell command inside the conda env |
| `validate_output` | Check output file is valid (bam/vcf/fastq/gfa/bim/ld/…) |
| `list_available_resources` | What genomes and test data are on disk |
| `download_resource` | Download a reference genome |
| `add_core_test_data` | Stream-download + subset reads from EBI SRA |
| `build_docker_image` | conda-pack → HPC Docker image (GPU-aware) |
| `check_gpu` | Detect NVIDIA GPU availability and CUDA version |
| `start_service` | Start a background service (web server, DB, Spark) inside the env |
| `stop_service` | Stop a background service by stop_command or PID file |
| `check_service_health` | Probe a running service with a health-check command |
| `save_pipeline_report` | Write YAML + HTML report to env_reports/ |
| `write_pipeline_provenance` | Write provenance YAML for a pipeline run |
| `list_installed_pipelines` | List installed pipelines from env_reports/ |
| `fetch_r_package_deps` | Parse DESCRIPTION from a GitHub R repo; returns all deps pre-categorised |

---

## How to install a pipeline (phases Claude Code follows)

When the user asks to install a tool or pipeline, execute ALL phases in order:

### Phase 1 — Research
- Call `search_package` for each requested package.
- **Capture from each result** → start building the `packages` list entry:
  ```
  { name, resolved_version: result.version, channel, conda_spec,
    description, homepage, input_types, output_types, check_command }
  ```

### Phase 2 — Install
- Call `create_conda_env` with name `bioinf_{pipeline_name}`.
- **For conda tools** (the default): call `install_packages` with all packages at once. Fall back to one-by-one if needed.
- **For Java tools** (Exomiser, Picard, GATK, …):
  1. Include `openjdk` (conda-forge) in the `install_packages` call — the JVM lives in the conda env.
  2. Use `run_in_env` to download the JAR from GitHub releases into `{env}/share/{tool}/`.
  3. Use `run_in_env` to write a thin wrapper script at `{env}/bin/{tool}` that calls
     `java <flags> -jar /path/to/tool.jar "$@"` — this makes the tool usable like any conda binary.
  4. `conda-pack` will bundle the JVM + JAR + wrapper → Docker image is self-contained, no `module load` needed.
  - Set `install_method: {type: "jar", jar_url: "...", jar_path: "..."}` on the tool's `PackageRecord`.
  - Set `runtime_environment: {type: "jar", java_flags: ["-Xmx12g"], jar_path: "...", wrapper_script: "..."}` on the spec.
- **For database-heavy tools** (tools needing >1 GB reference data beyond the genome FASTA):
  - Download the data with `run_in_env` (curl / wget).
  - Add a `ReferenceDatabase` entry to the spec with `name`, `version`, `size_gb`, `source_url`, `local_path`.
  - Add the data directory to `docker.volume_mounts` — it is NOT baked into the Docker image.
- Call `verify_installation` for each package.
- **Capture from each verify result** → add to that package's entry: `verify_command`, `verify_output`.

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
  - Pre-built pipeline outputs: `core_test_data_hg38/pipeline_outputs/{pipeline}/`
  - Read `core_test_data_hg38/manifest.yaml` to discover exactly what is available.
- Default strategy: use chr22 reference, 10K reads, write outputs to `data/{pipeline_name}_test_data/`.
- If genome index is missing for this tool, build it with `run_in_env` before the main test run.
- Only call `download_resource` if the needed genome is not already on disk.
- **Capture from the manifest/resources** → build the `test_data` dict to carry into Phase 6:
  ```
  { genome_build, chromosome_subset, read_type, end_type, assay_type,
    sample, accession, subset, num_reads, upstream_pipelines }
  ```

### Phase 4 — Validation loop
For each package in pipeline order:
- Build a test command with sensible defaults for small data. Use absolute paths.
- Call `run_in_env` to execute it.
- Call `run_in_env` with:
  - `inputs`: filenames going into this step (raw inputs for step 1; previous step's `detected_outputs` plus any additional files for step N>1)
  - `watch_dir`: the directory where outputs will land (absolute path)
- **Capture from the return** → append to `pipeline_steps`:
  ```
  { step, tool, command: result.command, returncode: result.returncode,
    runtime_seconds: result.runtime_seconds,
    inputs:  result.inputs,
    outputs: result.detected_outputs,
    validation: { filename: validate_output_result, ... } }
  ```
- Call `validate_output` for each filename in `result.detected_outputs`; store results keyed by filename in `validation`.
- **You do not need to set `validation_status` on the step manually** — `save_pipeline_report` derives it from the `validation` dict (all passed → "passed", any failed → "failed", empty → None).
- Pass `result.detected_outputs` as `inputs` to the next `run_in_env` call (full lineage).
- On failure, diagnose and retry up to 2 times.
- **Why this matters**: the pipeline-level `status` (returned by `save_pipeline_report`) can only land on `fully_validated` if every step has `validation_status="passed"`. A step that exits 0 but never gets a `validate_output` call counts as unvalidated and the pipeline drops to `partially_validated` or `complete`. Validate every output.

### Phase 5 — Docker
- Call `build_docker_image`. Pass `version` = the resolved version of the primary package.
- **Capture the entire return value** — use it directly as the `docker` field in the spec.
  The return already includes `build_attempted`, `build_success`, `image_tag`, `registry`, `reason`.

### Phase 6 — Report
- Call `save_pipeline_report` with the spec assembled from phases 1–5:
  ```
  {
    pipeline_name, description, conda_env,
    created_at: <now ISO>,
    packages:             <list built in phases 1–2>,
    runtime_environment:  <only if non-conda; e.g. {type:"jar", java_flags:[...], jar_path:"..."}>,
    reference_databases:  <list of {name, version, size_gb, source_url, local_path} if any>,
    runtime_configs:      <list of {name, format, path, content?} for global config files>,
    test_data:            <dict built in phase 3>,
    pipeline_steps:       <list built in phase 4>,
    docker:               <return value from phase 5>,
  }
  ```
- **Do NOT set `status`** — it is derived from the steps. The return value includes the derived `status` so you can report it back to the user.
- Use the exact field names above (e.g. `pipeline_name`, not `name`; `created_at`, not `created`; `docker` dict, not flat `docker_image`). There is no longer a backward-compatibility shim — wrong field names will fail validation.
- **Capture `saved_yaml` from the return** — this is `pipeline_spec_path` needed in Phase 7.

### Phase 7 — Provenance
- Call `write_pipeline_provenance` with the exact output files produced.
- Pass `pipeline_spec_path` = `saved_yaml` returned by Phase 6.
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
- `install_strategy` — concrete ordered steps

Install order from the strategy:
1. Call `search_package` for each dep to find what's on conda-forge (`r-{pkg}`) or bioconda (`bioconductor-{pkg}`). Install all confirmed conda packages in one `install_packages` call.
2. For anything not on conda, use `BiocManager::install()` — it is the authoritative resolver for both CRAN and Bioconductor packages and needs no pre-categorisation on our side.
3. GitHub install last, with `dependencies=FALSE`.

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
- Primary outputs are `.gfa` (assembly graph) and `.fa` (contig FASTA). Use `validate_output` with `expected_type: gfa` for GFA files.
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
| `env_reports/{name}_{version}.yaml` | Pipeline spec: packages, versions, test steps, validation |
| `env_reports/{name}_{version}.html` | Human-readable install report |
| `docker_images/{name}/` | Dockerfile + conda-pack tarball |
| `data/core_test_data_{build}/` | Reference genome, reads, pipeline outputs |

## Configuration

Edit `config/agent_config.yaml` to change Docker base image, conda channels, default Python version, or agent model/timeouts.

## HPC / Singularity

Docker images are built `--platform linux/amd64`, no `USER` directive, `/data` as WORKDIR.

```bash
singularity pull bioinf_samtools.sif docker://samtools:1.21
```
