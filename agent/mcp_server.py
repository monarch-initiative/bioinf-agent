"""
Bioinformatics Agent — MCP Server

Exposes all pipeline execution capabilities as MCP tools so Claude Code
can drive orchestration directly using your Claude subscription, with no
separate Anthropic API credits required.

Start with:
    python -m agent.mcp_server

Or register in .claude/settings.json (already done) so Claude Code
starts it automatically.
"""

from __future__ import annotations

import re
import subprocess
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Optional

import yaml
from fastmcp import FastMCP
from pydantic import ValidationError

from agent.models.core_data import PipelineSpec

# ---------------------------------------------------------------------------
# Config + skill singletons (initialised once at server startup)
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).parent.parent.resolve()


def _load_config() -> dict:
    with open(PROJECT_ROOT / "config" / "agent_config.yaml") as f:
        return yaml.safe_load(f)


config = _load_config()

from agent.skills.package_search import PackageSearch
from agent.skills.env_manager import EnvManager
from agent.skills.test_runner import TestRunner
from agent.skills.docker_builder import DockerBuilder
from agent.skills.core_test_data import add_core_test_data as _add_core_test_data
from agent.skills.core_test_data import add_phenopacket as _add_phenopacket
from agent.validators.output_validator import OutputValidator
from agent.skills.spec_writer import save_pipeline_spec as _save_pipeline_spec
from agent.skills.spec_writer import write_provenance as _write_provenance
from agent.skills.resources import list_resources as _list_resources
from agent.skills.resources import list_pipelines as _list_pipelines
from agent.skills.pipeline_state import PipelineState

_pkg_search     = PackageSearch(config)
_env_mgr        = EnvManager(config)
_test_runner    = TestRunner(config)
_docker         = DockerBuilder(config)
_validator      = OutputValidator(config)
_pipeline_state = PipelineState(config)

mcp = FastMCP("bioinf-agent")

# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

@mcp.tool()
def search_package(
    package_name: str,
    requested_version: str = "latest",
    pipeline_id: str = "",
) -> dict:
    """Search anaconda.org / bioconda / conda-forge / PyPI for a bioinformatics package.
    Returns channel, exact version, conda spec, install command, and brief description.

    If pipeline_id is supplied, a PackageRecord-shaped entry is also appended to
    draft.packages — you don't need to re-state the result in the final spec."""
    result = _pkg_search.search(package_name, requested_version)
    if pipeline_id and result.get("found"):
        package_record = {
            "name":              result.get("package_name", package_name),
            "requested_version": requested_version,
            "resolved_version":  result.get("version"),
            "channel":           result.get("channel"),
            "conda_spec":        result.get("conda_spec"),
            "description":       result.get("description"),
            "homepage":          result.get("home"),
            "input_types":       result.get("input_types", []),
            "output_types":      result.get("output_types", []),
            "check_command":     result.get("check_command"),
        }
        idx = _pipeline_state.add_package(pipeline_id, package_record)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "package_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result

# ---------------------------------------------------------------------------
# Environment management
# ---------------------------------------------------------------------------

@mcp.tool()
def create_conda_env(
    env_name: str,
    python_version: str = "",
    pipeline_id: str = "",
) -> dict:
    """Create a new isolated conda environment.

    If pipeline_id is supplied, draft.conda_env and draft.python_version are set."""
    pv = python_version or config["conda"]["python_version"]
    result = _env_mgr.create(env_name, python_version=pv)
    if pipeline_id:
        ok = _pipeline_state.set_conda_env(pipeline_id, env_name, python_version=pv)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id} if ok
            else {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def install_packages(env_name: str, packages: list[dict]) -> dict:
    """Install packages into a conda env.
    packages: list of {spec: str, channel: str}, e.g. [{spec: 'samtools=1.21', channel: 'bioconda'}]
    conda-pack is added automatically."""
    return _env_mgr.install(env_name, packages)


@mcp.tool()
def verify_installation(
    env_name: str,
    package_name: str,
    check_command: str,
    pipeline_id: str = "",
) -> dict:
    """Run a version/help command inside the env to confirm a package installed correctly.

    If pipeline_id is supplied, the named package's record in the draft is patched
    with verify_command + verify_output."""
    result = _env_mgr.verify(env_name, package_name, check_command)
    if pipeline_id:
        patched = _pipeline_state.patch_package(pipeline_id, package_name, {
            "verify_command": check_command,
            "verify_output":  result.get("output", ""),
        })
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id} if patched
            else {"status": "package_not_in_draft",
                  "pipeline_id": pipeline_id, "package_name": package_name}
        )
    return result


@mcp.tool()
def check_gpu() -> dict:
    """Check if an NVIDIA GPU is available for GPU-accelerated tools.
    Returns: available (bool), gpus (list of names), cuda_version, driver_version.
    If available=False, use CPU fallback mode for validation runs."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=name,driver_version,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
    except FileNotFoundError:
        return {"available": False, "reason": "nvidia-smi not found", "fallback": "Use CPU mode for testing"}
    if r.returncode != 0:
        return {"available": False, "reason": r.stderr.strip()[:200], "fallback": "Use CPU mode for testing"}

    gpus = []
    for line in r.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) >= 3:
            gpus.append({"name": parts[0], "driver_version": parts[1], "memory_mb": parts[2]})

    # Extract CUDA version from nvidia-smi header line
    header = subprocess.run(["nvidia-smi"], capture_output=True, text=True, timeout=10)
    cuda_ver = ""
    for ln in header.stdout.splitlines():
        if "CUDA Version:" in ln:
            cuda_ver = ln.split("CUDA Version:")[-1].strip().split()[0]
            break

    return {"available": bool(gpus), "gpus": gpus, "cuda_version": cuda_ver}


@mcp.tool()
def start_service(
    env_name: str,
    service_name: str,
    start_command: str,
    health_check_command: str,
    health_check_timeout_seconds: int = 30,
    working_dir: str = "",
    env_vars: dict[str, str] = {},
) -> dict:
    """Start a background service (web server, database, Spark) inside a conda env.
    Polls health_check_command until healthy or timeout.
    Returns: success, pid, log path."""
    return _env_mgr.start_service(
        env_name, service_name, start_command, health_check_command,
        health_check_timeout_seconds=health_check_timeout_seconds,
        working_dir=working_dir or None,
        env_vars=env_vars or None,
    )


@mcp.tool()
def stop_service(
    env_name: str,
    service_name: str,
    stop_command: str = "",
) -> dict:
    """Stop a background service started with start_service.
    Prefers stop_command if provided; falls back to killing by PID file."""
    return _env_mgr.stop_service(env_name, service_name, stop_command=stop_command)


@mcp.tool()
def check_service_health(
    env_name: str,
    health_check_command: str,
    working_dir: str = "",
) -> dict:
    """Run a health-check command to verify a background service is responding.
    Returns: healthy (bool), returncode, stdout, stderr."""
    return _env_mgr.check_service_health(env_name, health_check_command, working_dir=working_dir or None)


@mcp.tool()
def run_in_env(
    env_name: str,
    command: str,
    working_dir: str = "",
    timeout_seconds: int = 1800,
    inputs: list[str] = [],
    watch_dir: str = "",
    pipeline_id: str = "",
    step: int = 0,
    tool: str = "",
    subcommand: str = "",
    purpose: str = "",
) -> dict:
    """Run an arbitrary shell command inside a conda environment. Always use absolute paths.

    inputs:    filenames consumed by this step — echoed back in the return value.
    watch_dir: directory to snapshot before/after execution. New and modified files
               are returned as detected_outputs. Defaults to working_dir if omitted.

    If pipeline_id is supplied, a PipelineStep entry is appended to
    draft.pipeline_steps with the command, returncode, runtime, inputs, and
    detected outputs. Pass `step=N` to replace step N (for retries); default
    is append. The returned `pipeline_merge.step_index` is what you pass to
    validate_output(step=...) to attach validations to this step.

    Return keys: returncode, stdout, stderr, success, command, runtime_seconds,
                 inputs, detected_outputs, [pipeline_merge]."""
    result = _env_mgr.run_in_env(
        env_name, command,
        working_dir=working_dir or None,
        timeout=timeout_seconds,
        inputs=inputs,
        watch_dir=watch_dir or working_dir or None,
    )
    if pipeline_id:
        step_data = {
            "tool":            tool or (command.split() or [""])[0],
            "subcommand":      subcommand or None,
            "purpose":         purpose or None,
            "command":         command,
            "returncode":      result.get("returncode"),
            "runtime_seconds": result.get("runtime_seconds"),
            "inputs":          result.get("inputs", []),
            "outputs":         result.get("detected_outputs", []),
        }
        step_data = {k: v for k, v in step_data.items() if v is not None}
        idx = _pipeline_state.add_step(pipeline_id, step_data, replace_step=step)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result

# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.tool()
def list_available_resources(resource_type: str = "both") -> dict:
    """List genomes and/or test datasets on disk.
    resource_type: 'genomes' | 'test_data' | 'both'"""
    return _list_resources({"resource_type": resource_type}, config)


@mcp.tool()
def download_resource(resource_type: str, resource_id: str) -> dict:
    """Download a reference genome not yet on disk.
    resource_type: 'genome', resource_id: e.g. 'hg38_chr22'"""
    return _test_runner.download_resource(resource_type, resource_id)


@mcp.tool()
def add_core_test_data(
    accession: str,
    assay_type: str,
    end_type: str = "paired_end",
    genome_build: str = "hg38",
    sample: str = "",
    subset: str = "10K",
    platform: str = "illumina",
    source_url: str = "",
    source_url_r2: str = "",
) -> dict:
    """Stream-download and register a new sequencing dataset.
    assay_type:   exome | wgs | rnaseq | chipseq | atacseq | hic | amplicon | wgbs | ont_wgs | pacbio_hifi | direct_rna | isoseq | fiberseq
    platform:     illumina (default) | ont | pacbio_hifi | pacbio_isoseq | pacbio_fiberseq
    subset:       500 | 1K | 10K (default) | 50K | 100K | 500K | 1M  — use 500 for long-read platforms
    source_url:   override EBI URL builder (e.g. NCBI FTP, S3). For paired-end also supply source_url_r2."""
    return _add_core_test_data(
        config, accession, assay_type,
        end_type=end_type, genome_build=genome_build,
        sample=sample, subset=subset, platform=platform,
        source_url=source_url, source_url_r2=source_url_r2,
    )

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@mcp.tool()
def add_phenopacket(
    source_url: str,
    genome_build: str = "hg38",
) -> dict:
    """Download and register a GA4GH phenopacket JSON into core_test_data.

    The phenopacket ID, subject, HPO terms, diseases, genes, and variants are
    all extracted from the JSON itself via PhenopacketMeta.from_phenopacket() —
    nothing is supplied manually.  Idempotent: re-running refreshes the sidecar.

    source_url:   direct URL to a phenopacket JSON file (GitHub raw, HTTP, etc.)
    genome_build: target core_test_data directory, e.g. hg38 (default)"""
    return _add_phenopacket(config, source_url=source_url, genome_build=genome_build)


@mcp.tool()
def validate_output(
    file_path: str,
    expected_type: str,
    env_name: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Validate a bioinformatics output file is non-empty and parseable.
    expected_type: sam | bam | fastq | fasta | vcf | bcf | bed | bigwig |
                   bim | fam | ld | frq | prune | tsv | csv | txt | log | any

    If pipeline_id+step are supplied, the result is merged into
    draft.pipeline_steps[step].validation[basename(file_path)] — and
    save_pipeline_spec will derive the step's validation_status from this."""
    result = _validator.validate(file_path, expected_type, env_name=env_name or None)
    if pipeline_id:
        if step <= 0:
            result["pipeline_merge"] = {"status": "step_required", "pipeline_id": pipeline_id}
        else:
            filename = Path(file_path).name
            ok = _pipeline_state.add_validation(pipeline_id, step, filename, result)
            result["pipeline_merge"] = (
                {"status": "merged", "pipeline_id": pipeline_id,
                 "step": step, "filename": filename}
                if ok else
                {"status": "step_not_found", "pipeline_id": pipeline_id, "step": step}
            )
    return result

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

@mcp.tool()
def build_docker_image(
    env_name: str,
    pipeline_name: str,
    pipeline_description: str,
    version: str = "",
    gpu_required: bool = False,
    cuda_version: str = "",
    pipeline_id: str = "",
) -> dict:
    """Package a conda env into an HPC-compatible Docker image via conda-pack.
    version:      resolved version string for the image tag, e.g. '1.21'. Defaults to 'latest'.
    gpu_required: when True, uses a CUDA base image and sets NVIDIA runtime labels.
    cuda_version: CUDA version string, e.g. '12.1'. Defaults to config gpu.default_cuda_version.

    If pipeline_id is supplied, draft.docker is set to the DockerBuild-shaped subset
    of this return."""
    result = _docker.build(
        env_name, pipeline_name, pipeline_description,
        version=version, gpu_required=gpu_required, cuda_version=cuda_version,
    )
    if pipeline_id:
        ok = _pipeline_state.set_docker(pipeline_id, result)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id} if ok
            else {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result

# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

@mcp.tool()
def save_pipeline_report(spec: dict) -> dict:
    """Validate and write the pipeline spec as YAML + HTML report to env_reports/.

    Required fields: pipeline_name, description, conda_env, created_at,
    packages (list), pipeline_steps (list), docker (dict).

    The pipeline-level `status` field is derived automatically from step states —
    "fully_validated" requires every step to have validation_status="passed"
    (set by validate_output). If you set `status` it will be overwritten by the
    derived value, which also returned in the response."""
    return _save_pipeline_spec(spec, config)


@mcp.tool()
def write_pipeline_provenance(
    pipeline: str,
    conda_env_path: str,
    pipeline_spec_path: str,
    output_files: list[dict],
    output_dir: str,
    sample_key: str,
    # genome reference — optional for tools that don't use a reference FASTA
    genome_build: str = "",
    chromosome: str = "",
    reference_path: str = "",
    # input types — at least one must be provided
    reads: Optional[dict] = None,
    bam_input: Optional[dict] = None,
    vcf_input: Optional[dict] = None,
    assembly_input: Optional[dict] = None,
    phenotype: Optional[dict] = None,
    pedigree: Optional[dict] = None,
    genotype_array: Optional[dict] = None,
    quantitative_traits: Optional[dict] = None,
    upstream_pipelines: Optional[list[str]] = None,
    parameters: Optional[dict] = None,
) -> dict:
    """Write a validated provenance YAML for a completed pipeline run.

    output_files: list of {file: str, type: str, indexed: bool}

    Input types (at least one required):
      reads:               {r1, r2?, sample, accession, subset, num_reads, assay_type, end_type, database}
      bam_input:           {bam: str, bai: str}
      vcf_input:           {vcf: str, tbi?: str, genome_build: str, upstream_pipeline?: str, sample_ids?: []}
      assembly_input:      {assembly: str, upstream_pipeline?: str}
      phenotype:           {ontology?: str, terms: [str], source?: str}
      pedigree:            {ped: str, proband?: str}
      genotype_array:      {file: str, format: hapmap|plink_bed|vcf|dosage|bgen,
                            bim?: str, fam?: str, n_samples?: int, n_snps?: int,
                            genome_build?: str, upstream_pipeline?: str}
      quantitative_traits: {traits: [str], file: str, n_samples?: int,
                            measurement_type?: continuous|binary|ordinal}

    genome_build / chromosome / reference_path are optional for tools that do not
    consume a reference FASTA (e.g. variant prioritizers, phenotype scorers, GWAS)."""
    inputs: dict[str, Any] = {
        "pipeline":           pipeline,
        "conda_env_path":     conda_env_path,
        "pipeline_spec_path": pipeline_spec_path,
        "genome_build":       genome_build,
        "chromosome":         chromosome,
        "reference_path":     reference_path,
        "output_files":       output_files,
        "output_dir":         output_dir,
        "sample_key":         sample_key,
    }
    if reads:                inputs["reads"]                = reads
    if bam_input:            inputs["bam_input"]            = bam_input
    if vcf_input:            inputs["vcf_input"]            = vcf_input
    if assembly_input:       inputs["assembly_input"]       = assembly_input
    if phenotype:            inputs["phenotype"]            = phenotype
    if pedigree:             inputs["pedigree"]             = pedigree
    if genotype_array:       inputs["genotype_array"]       = genotype_array
    if quantitative_traits:  inputs["quantitative_traits"]  = quantitative_traits
    if upstream_pipelines:   inputs["upstream_pipelines"]   = upstream_pipelines
    if parameters:           inputs["parameters"]           = parameters
    return _write_provenance(inputs, config)


@mcp.tool()
def list_installed_pipelines() -> dict:
    """List all pipelines installed and validated, with Docker tags and validation status."""
    return _list_pipelines(config)


# ---------------------------------------------------------------------------
# R package utilities
# ---------------------------------------------------------------------------

def _parse_dcf(text: str) -> dict[str, str]:
    """Parse a Debian Control File (R DESCRIPTION format) into a flat dict."""
    fields: dict[str, str] = {}
    current_key: str | None = None
    for line in text.splitlines():
        if line.startswith((" ", "\t")):
            if current_key:
                fields[current_key] += " " + line.strip()
        elif ":" in line:
            key, _, val = line.partition(":")
            current_key = key.strip()
            fields[current_key] = val.strip()
    return fields


def _parse_pkg_list(raw: str) -> list[str]:
    """Extract bare package names from a comma-separated dep field, stripping version specs."""
    names = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        name = re.split(r"[\s(]", item)[0].strip()
        if name and name != "R":
            names.append(name)
    return names


@mcp.tool()
def fetch_r_package_deps(github_repo: str, ref: str = "HEAD") -> dict:
    """Fetch the DESCRIPTION file from a GitHub R package and parse all dependencies.

    Use this BEFORE installing any R package from GitHub so you can pre-install
    all dependencies, then call remotes::install_github(..., dependencies=FALSE).

    github_repo: owner/repo, e.g. "jiabowang/GAPIT3"
    ref:         branch, tag, or commit SHA (default HEAD → main/master)

    Returns:
      package_name, version, r_version_required
      imports, depends, suggests, linking_to — raw dep name lists
      all_required  — union of imports + depends + linking_to (what must be installed)
      install_strategy — ordered steps: conda first, then BiocManager for everything
                         else (BiocManager resolves both CRAN and Bioconductor),
                         GitHub last with dependencies=FALSE."""
    url = f"https://raw.githubusercontent.com/{github_repo}/{ref}/DESCRIPTION"
    try:
        with urllib.request.urlopen(url, timeout=15) as resp:
            content = resp.read().decode("utf-8")
    except Exception as e:
        return {"success": False, "error": str(e), "url": url}

    fields = _parse_dcf(content)

    imports    = _parse_pkg_list(fields.get("Imports", ""))
    depends    = _parse_pkg_list(fields.get("Depends", ""))
    suggests   = _parse_pkg_list(fields.get("Suggests", ""))
    linking_to = _parse_pkg_list(fields.get("LinkingTo", ""))

    r_ver_match = re.search(r"R\s*\(>=[^)]*\)", fields.get("Depends", ""))
    r_version_required = r_ver_match.group(0) if r_ver_match else ""

    all_required = sorted(set(imports + depends + linking_to))

    return {
        "success": True,
        "github_repo": github_repo,
        "ref": ref,
        "url": url,
        "package_name": fields.get("Package", ""),
        "version": fields.get("Version", ""),
        "r_version_required": r_version_required,
        "imports": imports,
        "depends": depends,
        "suggests": suggests,
        "linking_to": linking_to,
        "all_required": all_required,
        "install_strategy": [
            "1. For each dep in all_required, call search_package to check conda-forge "
            "(r-{lowercase}) and bioconda (bioconductor-{lowercase}) availability.",
            "2. Install all conda-available deps in one install_packages call.",
            "3. For deps not found on conda, install via BiocManager — it resolves both "
            "CRAN and Bioconductor packages without needing to know which is which: "
            "Rscript -e \"lib<-file.path(Sys.getenv('CONDA_PREFIX'),'lib','R','library'); "
            "if(!requireNamespace('BiocManager',quietly=TRUE)) "
            "install.packages('BiocManager',lib=lib); "
            "BiocManager::install(c('pkg1','pkg2'), lib=lib, ask=FALSE, update=FALSE)\"",
            f"4. Finally: remotes::install_github('{github_repo}', "
            "lib=file.path(Sys.getenv('CONDA_PREFIX'),'lib','R','library'), "
            "dependencies=FALSE)",
        ],
    }


# ---------------------------------------------------------------------------
# Pipeline state accumulator
# ---------------------------------------------------------------------------

@mcp.tool()
def start_pipeline(pipeline_name: str, description: str) -> dict:
    """Start a new pipeline draft (or silently resume an existing one).

    Returns pipeline_id (= pipeline_name). Pass pipeline_id to subsequent
    tools (search_package, create_conda_env, verify_installation, run_in_env,
    validate_output, build_docker_image, select_test_data) so their results
    are auto-merged into a server-side draft. You don't have to hand-assemble
    the final spec at the end — call finalize_pipeline(pipeline_id) instead.

    Resume semantics: if a draft for this pipeline_name already exists (e.g.
    after an MCP restart mid-install), it is loaded and resumed silently;
    `resumed: true` is set in the return so you know."""
    return _pipeline_state.start(pipeline_name, description)


@mcp.tool()
def finalize_pipeline(pipeline_id: str) -> dict:
    """Validate the accumulated draft against PipelineSpec, write the final
    YAML + HTML report to env_reports/{name}_{version}.yaml, and delete the
    draft. Returns the saved paths and the derived status.

    If validation fails, the draft is preserved and validation_errors are
    returned — call patch_pipeline to fix and retry, or discard_pipeline_draft
    to start over."""
    draft = _pipeline_state.get_draft(pipeline_id)
    if draft is None:
        return {"error": f"unknown pipeline_id: {pipeline_id}"}

    if not draft.get("created_at"):
        draft["created_at"] = str(date.today())

    try:
        PipelineSpec.model_validate(draft)
    except ValidationError as e:
        return {
            "error":             "PipelineSpec validation failed",
            "validation_errors": e.errors(),
            "draft_path":        str(_pipeline_state._draft_path(pipeline_id)),
        }

    result = _save_pipeline_spec(draft, config)
    _pipeline_state.pop_for_finalize(pipeline_id)
    _pipeline_state.delete_draft_file(pipeline_id)
    return result


@mcp.tool()
def discard_pipeline_draft(pipeline_id: str) -> dict:
    """Delete a pipeline draft without finalizing. Use when you want to start
    fresh with the same pipeline_name and don't want resume semantics."""
    return _pipeline_state.discard(pipeline_id)


@mcp.tool()
def show_pipeline_draft(pipeline_id: str) -> dict:
    """Return the current accumulated draft for inspection. Does not modify
    or finalize anything."""
    draft = _pipeline_state.get_draft(pipeline_id)
    if draft is None:
        return {"error": f"unknown pipeline_id: {pipeline_id}"}
    return {"pipeline_id": pipeline_id, "draft": draft}


@mcp.tool()
def patch_pipeline(pipeline_id: str, patches: dict) -> dict:
    """Deep-merge arbitrary patches into the draft. Escape hatch for fields
    no tool produces directly — runtime_environment, runtime_configs,
    reference_databases, service_dependencies, notes, final_summary — or
    for overriding server-derived values."""
    return _pipeline_state.patch(pipeline_id, patches)


@mcp.tool()
def select_test_data(
    genome_build: str = "hg38",
    assay_type: str = "",
    end_type: str = "",
    sample: str = "",
    accession: str = "",
    subset: str = "",
    pipeline_id: str = "",
) -> dict:
    """Find a matching test dataset on disk and return a TestDataRef-shaped
    dict ready to drop into the spec. If pipeline_id is supplied, also sets
    draft.test_data.

    Match is best-effort: each criterion scores points; the highest-scoring
    AVAILABLE dataset wins. Returns {test_data, available, match_score}, or
    {error} if nothing on disk matches at all. Inspect the result and call
    again with different criteria if the match is wrong."""
    all_data = _list_resources({"resource_type": "test_data"}, config).get("test_data", [])
    sequencing = [d for d in all_data if d.get("type") not in ("phenopacket", "pipeline_output")]

    def _score(d: dict) -> int:
        s = 0
        if genome_build and d.get("genome_build") == genome_build: s += 32
        if assay_type   and d.get("assay_type")   == assay_type:   s += 16
        if end_type     and d.get("end_type")     == end_type:     s += 8
        if sample       and d.get("sample")       == sample:       s += 4
        if accession    and d.get("accession")    == accession:    s += 2
        if subset       and d.get("subset")       == subset:       s += 1
        return s

    scored = [(d, _score(d), bool(d.get("available"))) for d in sequencing]
    if not scored:
        return {"error": "no sequencing test data on disk"}
    scored.sort(key=lambda x: (x[2], x[1]), reverse=True)
    best, score, available = scored[0]
    if score == 0:
        return {
            "error": "no test data matches the requested criteria",
            "criteria": {
                "genome_build": genome_build, "assay_type": assay_type,
                "end_type": end_type, "sample": sample,
                "accession": accession, "subset": subset,
            },
        }

    test_data_ref = {
        "genome_build":  best.get("genome_build", ""),
        "read_type":     best.get("read_type"),
        "end_type":      best.get("end_type"),
        "assay_type":    best.get("assay_type"),
        "sample":        best.get("sample"),
        "accession":     best.get("accession"),
        "subset":        best.get("subset"),
        "num_reads":     best.get("num_reads"),
        "r1":            best.get("r1"),
        "r2":            best.get("r2"),
        "core_data_dir": best.get("core_dir"),
    }
    test_data_ref = {k: v for k, v in test_data_ref.items() if v is not None}

    result = {"test_data": test_data_ref, "available": available, "match_score": score}
    if pipeline_id:
        ok = _pipeline_state.set_test_data(pipeline_id, test_data_ref)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id} if ok
            else {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
