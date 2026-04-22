# Bioinformatics Agent

Conversational agent that installs bioinformatics tools/pipelines into isolated conda environments, validates them against test data, and packages them as HPC-compatible Docker images.

## Running the agent

```bash
# Install Python dependencies
pip install -r requirements.txt

# Bootstrap core test data (run once — downloads hg38/chr22, exome reads, runs bwa+freebayes)
./scripts/setup_core_test_data.sh

# Start conversational agent
python -m agent.main

# Single-shot mode
python -m agent.main --once "install latest bwa"
```

## Example conversations

```
You: install latest bwa
You: install STAR version 2.7.11b and featureCounts as my rnaseq_pipeline
You: install samtools, bwa-mem2, and GATK as wgs_variant_pipeline
You: what test data is available?
You: what pipelines have been installed?
```

## Architecture

```
agent/
├── main.py                    # Conversational loop (outer agent)
├── tools.py                   # Outer agent tool definitions + dispatcher
├── skills/
│   ├── install_pipeline.py    # Core skill — sub-agent loop for installs
│   ├── package_search.py      # anaconda.org / PyPI package lookup
│   ├── env_manager.py         # conda env create / install / run
│   ├── test_runner.py         # Download genomes + generate test data
│   └── docker_builder.py      # conda-pack → HPC Docker image
└── validators/
    └── output_validator.py    # SAM/BAM/VCF/FASTQ/BED/etc. checks
```

## How an install works

1. **Outer agent** receives user message, calls `install_pipeline` tool
2. **InstallPipelineSkill** launches a sub-agent loop with execution tools
3. Sub-agent phases:
   - **Research** — `search_package` queries anaconda.org + PyPI for each package
   - **Install** — `create_conda_env` + `install_packages` + `verify_installation`
   - **Test data** — `list_available_resources` → pick best fit → `download_resource` if needed
   - **Validate** — `run_command` executes each tool, `validate_output` checks results
   - **Chain** — output of step N becomes input to step N+1
   - **Docker** — `build_docker_image` (conda-pack → Dockerfile → docker buildx)
   - **Artifact** — `save_pipeline_spec` writes `config/pipelines/{name}.yaml`

## Shared data directories

| Directory | Purpose |
|-----------|---------|
| `data/genomes/` | Reference genomes + indexes. Checked before downloading. |
| `data/test_data/` | Small curated datasets per assay type. Generated synthetically from genomes via wgsim. |
| `data/genomes/manifest.yaml` | What genomes are available + index locations |
| `data/test_data/manifest.yaml` | What datasets exist + compatible tools/genomes |

## Generated artifacts

| Location | What it is |
|----------|-----------|
| `envs/bioinf_{name}/` | Conda environment |
| `config/pipelines/{name}.yaml` | Full record: packages, versions, test run, validation status |
| `docker_images/{name}/` | Dockerfile + conda-pack tarball |

## Configuration

Edit `config/agent_config.yaml` to change:
- Docker base image and registry
- HPC compatibility settings
- Default conda channels and Python version
- Agent model and timeouts

## HPC / Singularity notes

Docker images are built with:
- `--platform linux/amd64` for x86 HPC clusters
- No `USER` directive (Singularity runs as the calling user)
- No bind-mounted volumes at build time
- `/data` as WORKDIR (override with `-v` or Singularity `--bind`)

Convert to Singularity:
```bash
singularity pull bioinf_bwa.sif docker://bioinf_bwa:latest
```
