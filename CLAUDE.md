# Bioinformatics Agent

Installs bioinformatics tools into isolated conda environments, validates them against test data, and packages them as HPC-compatible Docker images.

## Primary mode — Claude Code + MCP (no API credits needed)

Claude Code drives all orchestration directly using your subscription. The MCP server is registered in `.claude/settings.json` and starts automatically.

```bash
pip install -r requirements.txt
./scripts/setup_core_test_data.sh   # one-time bootstrap: conda envs + reference genome
```

Then just talk to Claude Code:
```
install latest samtools
install bwa_samtools and freebayes as my wgs_variant_pipeline
add test data: SRR1517830, exome, hg38
what test data is available?
what pipelines have been installed?
```

## Fallback mode — standalone CLI (uses Anthropic API credits)

```bash
python -m agent.main
```

---

## MCP tools available to Claude Code

| Tool | What it does |
|------|-------------|
| `search_package` | Find package on bioconda/conda-forge/PyPI |
| `create_conda_env` | Create isolated conda env |
| `install_packages` | conda install one or more packages |
| `verify_installation` | Run version/help command to confirm install |
| `run_in_env` | Run any shell command inside the conda env |
| `validate_output` | Check output file is valid (bam/vcf/fastq/bim/ld/…) |
| `list_available_resources` | What genomes and test data are on disk |
| `download_resource` | Download a reference genome |
| `add_core_test_data` | Stream-download + subset reads from EBI SRA |
| `build_docker_image` | conda-pack → HPC Docker image |
| `save_pipeline_report` | Write YAML + HTML report to env_reports/ |
| `write_pipeline_provenance` | Write provenance YAML for a pipeline run |
| `list_installed_pipelines` | List installed pipelines from env_reports/ |

---

## How to install a pipeline (phases Claude Code follows)

When the user asks to install a tool or pipeline, execute ALL phases in order:

### Phase 1 — Research
- Call `search_package` for each requested package to get the conda channel, exact version, and understand its input/output types.

### Phase 2 — Install
- Call `create_conda_env` with name `bioinf_{pipeline_name}`.
- Call `install_packages` with all packages at once (better dependency resolution). Fall back to one-by-one if needed.
- Call `verify_installation` for each package.

### Phase 3 — Test data
- Call `list_available_resources(both)` to see what's on disk.
- Canonical test data lives at `data/core_test_data_{genome_build}/`:
  - Genome + indexes: `core_test_data_hg38/genome/chr22.fa` (+ .fai, bwa indexes)
  - Reads: `core_test_data_hg38/short_read/paired_end/exome/HG00096_SRR1517830_100K_R1.fastq.gz`
  - Pre-built pipeline outputs: `core_test_data_hg38/pipeline_outputs/{pipeline}/`
  - Read `core_test_data_hg38/manifest.yaml` to discover exactly what is available.
- Default strategy: use chr22 reference, 100K paired-end reads, write outputs to `data/{pipeline_name}_test_data/`.
- If genome index is missing for this tool, build it with `run_in_env` before the main test run.
- Only call `download_resource` if the needed genome is not already on disk.

### Phase 4 — Validation loop
For each package in pipeline order:
- Build a test command with sensible defaults for small data. Use absolute paths.
- Call `run_in_env` to execute it.
- Call `validate_output` on the primary output file.
- The output of step N is the input to step N+1.
- On failure, diagnose and retry up to 2 times.

### Phase 5 — Provenance
- Call `write_pipeline_provenance` with the exact output files produced.
- Pass all absolute paths; relative paths inside the YAML are computed automatically.
- Required fields: pipeline, conda_env_path, pipeline_spec_path, genome_build, chromosome, reference_path, output_files, output_dir, sample_key.

### Phase 6 — Docker
- Call `build_docker_image`. Pass `version` = the resolved version of the primary package.

### Phase 7 — Report
- Call `save_pipeline_report` with the complete spec dict.
- Required top-level fields: `pipeline_name`, `description`, `conda_env`, `created_at`, `status`, `packages`, `pipeline_steps`, `docker`.
- `pipeline_steps`: list of `{step, tool, command, status, returncode, runtime_seconds}`.
- `status`: `"fully_validated"` if all steps passed, else `"failed"`.

### Rules
- Always use absolute paths in `run_in_env` commands.
- Prefer bioconda > conda-forge > defaults channel priority.
- conda-pack is added to every env automatically by `install_packages`.
- For tools that link against htslib (samtools, bcftools, bwa), install them in one `install_packages` call for compatible dependency resolution.
- Always pass `env_name` to `validate_output` so samtools/bcftools resolve from inside the pipeline env.

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
├── main.py                     # Standalone CLI (fallback mode)
├── mcp_server.py               # MCP server — primary mode
├── tools.py                    # Outer tool dispatcher (used by main.py + mcp_server)
├── skills/
│   ├── install_pipeline.py     # Sub-agent loop (fallback mode only)
│   ├── package_search.py       # anaconda.org / PyPI lookup
│   ├── env_manager.py          # conda create / install / run
│   ├── test_runner.py          # Reference genome downloads
│   ├── core_test_data.py       # EBI SRA stream-download + subset
│   ├── docker_builder.py       # conda-pack → Docker image
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
