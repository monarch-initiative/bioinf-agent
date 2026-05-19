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
from agent.skills.core_test_data import phenopacket_to_vcf as _phenopacket_to_vcf
from agent.validators.output_validator import OutputValidator
from agent.skills.spec_writer import save_pipeline_spec as _save_pipeline_spec
from agent.skills.spec_writer import write_provenance as _write_provenance
from agent.skills.resources import list_resources as _list_resources
from agent.skills.resources import list_pipelines as _list_pipelines
from agent.skills.pipeline_state import PipelineState
from agent.skills.job_manager import JobManager

_pkg_search     = PackageSearch(config)
_env_mgr        = EnvManager(config)
_test_runner    = TestRunner(config)
_docker         = DockerBuilder(config)
_validator      = OutputValidator(config)
_pipeline_state = PipelineState(config)
_job_manager    = JobManager(config)

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

    `packages` in the final spec is rebuilt at finalize-time from the live
    conda env + install_steps' installed_packages (the source of truth),
    NOT from search_package. So this tool is query-only: use the returned
    versions/channels to choose what to actually install via
    `install_packages` / `run_install_command`.

    If pipeline_id is supplied, description/homepage/input_types/output_types
    are cached on `draft.search_cache[package_name]` so the finalize pass can
    annotate the derived package record without re-querying."""
    result = _pkg_search.search(package_name, requested_version)
    if pipeline_id and result.get("found"):
        name = result.get("package_name", package_name)
        cache_entry = {
            "description":   result.get("description"),
            "homepage":      result.get("home"),
            "input_types":   result.get("input_types", []),
            "output_types":  result.get("output_types", []),
            "check_command": result.get("check_command"),
        }
        cache_entry = {k: v for k, v in cache_entry.items() if v}
        ok = _pipeline_state.cache_search_result(pipeline_id, name, cache_entry)
        result["pipeline_merge"] = (
            {"status": "cached", "pipeline_id": pipeline_id, "name": name}
            if ok else
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

    If pipeline_id is supplied, draft.conda_env and draft.python_version are
    set, and an entry is appended to draft.install_steps recording the env
    creation so the install journey is captured chronologically."""
    pv = python_version or config["conda"]["python_version"]
    result = _env_mgr.create(env_name, python_version=pv)
    if pipeline_id:
        _pipeline_state.set_conda_env(pipeline_id, env_name, python_version=pv)
        idx = _pipeline_state.add_install_step(pipeline_id, {
            "tool":               "conda",
            "subcommand":         "create",
            "purpose":            f"Create the {env_name} conda environment",
            "command":            f"conda create --prefix envs/{env_name} python={pv}",
            "returncode":         0 if result.get("success") else 1,
            "installed_packages": [{"name": "python", "version": pv, "channel": "conda-forge"}],
        })
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def install_packages(
    env_name: str,
    packages: list[dict],
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install packages into a conda env.
    packages: list of {spec: str, channel: str}, e.g. [{spec: 'samtools=1.21', channel: 'bioconda'}]
    conda-pack is added automatically.

    If pipeline_id is supplied, an entry is appended to draft.install_steps with
    installed_packages parsed from each spec (spec='samtools=1.21' → name=samtools
    version=1.21). conda-pack is filtered out of installed_packages since it is
    infrastructure, not a user-facing package.

    Pass `step=N` to replace install_step N (for retries after a solver
    conflict, etc). Default is append — same semantics as run_install_command."""
    result = _env_mgr.install(env_name, packages)
    if pipeline_id:
        from agent.skills.env_manager import parse_conda_spec
        installed: list[dict] = []
        for pkg in packages:
            parsed = parse_conda_spec(pkg.get("spec", ""))
            name = parsed["name"]
            if not name or name == "conda-pack":
                continue
            entry = {"name": name, "channel": pkg.get("channel", "")}
            if parsed["version"]:
                entry["version"] = parsed["version"]
            if parsed["constraint"] and parsed["constraint"] not in ("=", "=="):
                entry["version_constraint"] = parsed["constraint"]
            installed.append(entry)
        idx = _pipeline_state.add_install_step(pipeline_id, {
            "tool":               "conda",
            "subcommand":         "install",
            "purpose":            f"Install {len(installed)} package(s) into {env_name}",
            "command":            "conda install " + " ".join(p.get("spec", "") for p in packages),
            "returncode":         result.get("returncode"),
            "installed_packages": installed,
        }, replace_step=step)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def install_jar_tool(
    env_name: str,
    tool_name: str,
    jar_url: str,
    java_flags: list[str] = [],
    wrapper_name: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install a Java JAR-based tool end-to-end (Exomiser, Picard, GATK, snpEff, …).

    The env must already have `openjdk` (and `unzip` if `jar_url` is a .zip).
    Downloads the JAR (curl with a progress bar — watchdog-friendly), unpacks if
    it's a distribution zip, picks the primary jar (heuristic: name contains
    `tool_name`, shortest matches first), and writes a wrapper script at
    {env}/bin/{wrapper_name or tool_name} that runs `java {java_flags} -jar JAR "$@"`.

    java_flags default: ["-Xmx4g"]. Override for memory-hungry tools (Exomiser
    typically wants -Xmx6g or higher).

    If pipeline_id is supplied, records an install_step (tool=jar, subcommand=install)
    with installed_packages=[{name=tool_name, channel=github (if URL host is github.com),
    else external, source=jar_url}], so the finalize-time package derivation picks it
    up automatically and the spec ends up with install_method.type=jar.
    """
    flags = list(java_flags) if java_flags else ["-Xmx4g"]
    result = _env_mgr.install_jar_tool(
        env_name      = env_name,
        tool_name     = tool_name,
        jar_url       = jar_url,
        java_flags    = flags,
        wrapper_name  = wrapper_name,
    )
    if pipeline_id:
        from urllib.parse import urlparse
        host = urlparse(jar_url).netloc or ""
        channel = "github" if "github.com" in host else "external"
        step_data = {
            "tool":        "jar",
            "subcommand":  "install",
            "purpose":     f"Install {tool_name} JAR from {host}",
            "command":     f"install_jar_tool --jar-url {jar_url}",
            "returncode":  0 if result.get("success") else 1,
            "installed_packages": [{
                "name":    tool_name,
                "channel": channel,
                "source":  jar_url,
                "install_method": {"type": "jar", "source": jar_url},
            }],
        }
        if result.get("success"):
            step_data["installed_packages"][0]["install_method"]["jar_path"]        = result.get("jar_path")
            step_data["installed_packages"][0]["install_method"]["wrapper_script"]  = result.get("wrapper_path")
            step_data["installed_packages"][0]["install_method"]["java_flags"]      = flags
        idx = _pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def verify_installation(
    env_name: str,
    package_name: str,
    check_command: str,
    pipeline_id: str = "",
) -> dict:
    """Run a custom version/help command inside the env to confirm a package
    installed correctly.

    Advisory: env_status no longer requires every package to have a verify
    record — `conda list --json` plus successful install_steps are the
    structural truth. Use this when you want to capture a custom check
    (e.g. `samtools --version` for a CLI tool) so it appears in the report
    next to that package.

    If pipeline_id is supplied, the result is cached in
    draft.verifications[package_name] and stitched onto the derived package
    record at finalize-time."""
    result = _env_mgr.verify(env_name, package_name, check_command)
    if pipeline_id:
        ok = _pipeline_state.cache_verification(pipeline_id, package_name, {
            "verify_command": check_command,
            "verify_output":  result.get("output", ""),
        })
        result["pipeline_merge"] = (
            {"status": "cached", "pipeline_id": pipeline_id, "name": package_name}
            if ok else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
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
    inputs: list = [],
    watch_dir: str = "",
    pipeline_id: str = "",
    step: int = 0,
    tool: str = "",
    subcommand: str = "",
    purpose: str = "",
) -> dict:
    """Run an arbitrary shell command inside a conda environment. Always use absolute paths.

    inputs: list of files this step consumes. Each entry is either:
            - a plain string path: "/abs/path/to/file"
            - a structured dict:   {path: "/abs/path/to/run.R",
                                    references: ["/abs/path/data1.tsv", ...]}
              Use the dict form when an input is a script / config / wrapper that
              opens other files at runtime — the references become an indented
              sublist under that input in the HTML report and are kept in the
              spec so the lineage is programmatic (not memory-bound).
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
def phenopacket_to_vcf(
    phenopacket_id: str,
    output_vcf: str,
    genome_build: str = "hg38",
) -> dict:
    """Materialise a single-sample VCF from a registered phenopacket's variant block.

    Use this in Exomiser / variant-annotator pipelines where the test VCF should
    contain the exact variant(s) recorded in the phenopacket — no hand-written
    VCF synthesis required. Reads {data_dir}/core_test_data_{genome_build}/
    phenopackets/{phenopacket_id}_meta.yaml; writes a VCFv4.2 file to output_vcf.

    Inputs:
      phenopacket_id: as registered by add_phenopacket (PMID_30315159_Patient_N, …)
      output_vcf:     absolute path to write the VCF
      genome_build:   defaults to hg38

    Output keys: success, phenopacket_id, sample_id, output_vcf, num_variants,
                 contigs, genome_assembly. On failure: {success: false, error}.
    Phenotype-only phenopackets (no variant block) cannot be materialised — this
    is surfaced as a clear error rather than silently producing an empty VCF.
    """
    return _phenopacket_to_vcf(
        config, phenopacket_id=phenopacket_id,
        output_vcf=output_vcf, genome_build=genome_build,
    )


@mcp.tool()
def validate_output(
    file_path: str = "",
    expected_type: str = "",
    files: list[dict] = [],
    env_name: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Validate one or many bioinformatics output files are non-empty and parseable.

    expected_type: sam | bam | fastq | fasta | vcf | bcf | bed | bigwig |
                   bim | fam | ld | frq | prune | tsv | csv | txt | log | any

    Two call shapes:
      Single:  validate_output(file_path=..., expected_type=...)
      Batch:   validate_output(files=[{path, expected_type}, ...])

    In batch mode each file is validated independently — one failure never
    aborts the others. Returns per-file results in `validations` keyed by
    basename, plus aggregate `all_passed` / `passed_count` / `failed_count`.

    If pipeline_id+step are supplied, every result is merged into
    draft.pipeline_steps[step].validation[basename] — save_pipeline_spec
    then derives the step's validation_status from the aggregate."""
    # Batch path
    if files:
        validations: dict = {}
        passed_count = failed_count = 0
        merge_status = "merged" if pipeline_id and step > 0 else None
        for entry in files:
            path = entry.get("path", "")
            etype = entry.get("expected_type", "any")
            vr = _validator.validate(path, etype, env_name=env_name or None)
            filename = Path(path).name
            validations[filename] = vr
            if vr.get("passed") is True:
                passed_count += 1
            elif vr.get("passed") is False:
                failed_count += 1
            if merge_status == "merged":
                ok = _pipeline_state.add_validation(pipeline_id, step, filename, vr)
                if not ok:
                    merge_status = "step_not_found"
        out: dict = {
            "validations":   validations,
            "all_passed":    failed_count == 0 and passed_count == len(files),
            "passed_count":  passed_count,
            "failed_count":  failed_count,
            "total":         len(files),
        }
        if pipeline_id:
            if step <= 0:
                out["pipeline_merge"] = {"status": "step_required", "pipeline_id": pipeline_id}
            else:
                out["pipeline_merge"] = {
                    "status": merge_status, "pipeline_id": pipeline_id, "step": step,
                    "merged_files": passed_count + failed_count,
                }
        return out

    # Single-file path (unchanged behavior)
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
    return _save_pipeline_spec(spec, config, env_manager=_env_mgr)


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


_R_RUNTIME_INSTALL_RE = re.compile(
    r"""(?:BiocManager::install
        |install\.packages
        |requireNamespace            # if(!requireNamespace("X")) is the canonical lazy-install signal
        |library
        |require)
        \s*\(
    """,
    re.VERBOSE,
)

_R_RUNTIME_QUOTED_RE = re.compile(r"""(['"])([A-Za-z0-9._]+)\1""")


def _scan_r_runtime_installs(github_repo: str, ref: str) -> list[str]:
    """Look for `BiocManager::install("X")` / `install.packages("X")` calls in the
    R/ source directory of a GitHub R package. Returns a deduplicated list of
    package names. Best-effort — failures are silent (we still want the
    DESCRIPTION info to be returned)."""
    api_url = f"https://api.github.com/repos/{github_repo}/contents/R?ref={ref}"
    try:
        with urllib.request.urlopen(api_url, timeout=15) as resp:
            listing = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []

    found: set[str] = set()
    for entry in listing:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name", "")
        if not name.endswith(".R") and not name.endswith(".r"):
            continue
        raw_url = entry.get("download_url")
        if not raw_url:
            continue
        try:
            with urllib.request.urlopen(raw_url, timeout=15) as f:
                source = f.read().decode("utf-8", errors="replace")
        except Exception:
            continue
        # Strip comment lines so we don't grab examples from comments.
        cleaned = "\n".join(
            line for line in source.splitlines()
            if not line.lstrip().startswith("#")
        )
        # Find each call site, then pluck every quoted name from the argument list.
        for m in _R_RUNTIME_INSTALL_RE.finditer(cleaned):
            # The regex captures only the first name; scan the substring containing
            # the call's arg list for all quoted strings (handles c('a','b','c')).
            start = m.start()
            depth = 0
            i = start
            while i < len(cleaned) and cleaned[i] != "(":
                i += 1
            arglist_start = i + 1
            depth = 1
            i = arglist_start
            while i < len(cleaned) and depth > 0:
                if cleaned[i] == "(":
                    depth += 1
                elif cleaned[i] == ")":
                    depth -= 1
                i += 1
            arglist = cleaned[arglist_start:i-1]
            for q in _R_RUNTIME_QUOTED_RE.finditer(arglist):
                pkg = q.group(2)
                # Filter out obvious non-package strings (version numbers, paths, options)
                if pkg and not pkg.startswith(("/", "./", "http", "https")) and "." not in pkg[:1]:
                    if re.match(r"^[A-Za-z][A-Za-z0-9._]*$", pkg):
                        found.add(pkg)
    return sorted(found)


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

    # Hunt for *undeclared* transitive deps — R packages whose .onLoad hooks or
    # zzz.R do `BiocManager::install("X")` / `install.packages("X")` for things
    # not listed in DESCRIPTION. GAPIT does this with snpStats; without this
    # discovery the agent learns about it only by the install failing.
    undeclared_runtime_installs = _scan_r_runtime_installs(github_repo, ref)
    undeclared_set = sorted({
        pkg for pkg in undeclared_runtime_installs
        if pkg not in set(all_required + suggests) and pkg != fields.get("Package", "")
    })

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
        "undeclared_runtime_installs": undeclared_set,
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
    `resumed: true` is set in the return so you know. On resume, a `summary`
    block is included describing what's already in the draft so the agent can
    decide whether to continue from where the prior run left off or call
    discard_pipeline_draft and start fresh."""
    r = _pipeline_state.start(pipeline_name, description)
    if r.get("resumed"):
        draft = _pipeline_state.get_draft(pipeline_name) or {}
        install_steps = draft.get("install_steps", []) or []
        pipeline_steps = draft.get("pipeline_steps", []) or []
        last_install = install_steps[-1] if install_steps else None
        last_pipeline = pipeline_steps[-1] if pipeline_steps else None
        # Surface any pipeline step with no detected outputs and no validation —
        # the silent-empty-success pattern that bit the prior Exomiser run.
        suspect_steps = [
            s.get("step")
            for s in pipeline_steps
            if not (s.get("detected_outputs") or s.get("outputs") or s.get("validation"))
        ]
        r["summary"] = {
            "conda_env":              draft.get("conda_env"),
            "env_status":             draft.get("env_status"),
            "pipeline_status":        draft.get("pipeline_status"),
            "install_steps_count":    len(install_steps),
            "install_steps_failed":   sum(1 for s in install_steps if s.get("returncode") not in (None, 0)),
            "pipeline_steps_count":   len(pipeline_steps),
            "pipeline_steps_failed":  sum(1 for s in pipeline_steps if s.get("returncode") not in (None, 0)),
            "packages_recorded":      len(draft.get("packages", []) or []),
            "test_data":              draft.get("test_data") is not None,
            "docker":                 draft.get("docker") is not None,
            "usage":                  draft.get("usage") is not None,
            "last_install_tool":      last_install.get("tool") if last_install else None,
            "last_pipeline_tool":     last_pipeline.get("tool") if last_pipeline else None,
            "suspect_unvalidated_steps": suspect_steps,
        }
    return r


@mcp.tool()
def validate_pipeline_draft(pipeline_id: str) -> dict:
    """Dry-run finalize: validate the current draft against PipelineSpec without
    writing artifacts or deleting the draft. Returns:
      {valid: bool, validation_errors?: [...], env_status, pipeline_status}.

    Use before finalize_pipeline to catch schema problems early (notes shape,
    docker.volume_mounts type, runtime_configs.format, OutputFile.type, ...)
    without having to repair-finalize-repeat through the live save path."""
    draft = _pipeline_state.get_draft(pipeline_id)
    if draft is None:
        return {"error": f"unknown pipeline_id: {pipeline_id}"}
    if not draft.get("created_at"):
        draft = {**draft, "created_at": str(date.today())}
    try:
        PipelineSpec.model_validate(draft)
        return {
            "valid":           True,
            "env_status":      draft.get("env_status"),
            "pipeline_status": draft.get("pipeline_status"),
            "package_count":   len(draft.get("packages", []) or []),
            "install_steps":   len(draft.get("install_steps", []) or []),
            "pipeline_steps":  len(draft.get("pipeline_steps", []) or []),
        }
    except ValidationError as e:
        return {
            "valid":             False,
            "validation_errors": e.errors(),
        }


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

    result = _save_pipeline_spec(draft, config, env_manager=_env_mgr)
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
    for overriding server-derived values.

    Lists `pipeline_steps` and `install_steps` are merged element-by-element
    on the `step` field, so a partial patch like
    {"pipeline_steps": [{"step": 2, "validation_status": "passed"}]}
    updates just that step's validation_status without clobbering the rest.
    All other lists are replaced wholesale."""
    return _pipeline_state.patch(pipeline_id, patches)


@mcp.tool()
def mark_step_validated(
    pipeline_id: str,
    step: int,
    validation_status: str = "passed",
) -> dict:
    """Set the validation_status of a pipeline_step. The pipeline-level
    pipeline_status derives from these at finalize time.

    Use this when a step's outputs are known good but validate_output wasn't
    called for them (e.g. the step's outputs were checked by the next step's
    success, or the LLM verified them by other means). Operates only on
    pipeline_steps — install_steps don't carry a validation_status field;
    their success is captured by status (returncode==0)."""
    if validation_status not in {"passed", "failed"}:
        return {"error": "validation_status must be 'passed' or 'failed'",
                "got": validation_status}
    # Guard against the silent-empty-success trap: a step that exited 0 but
    # produced no detected outputs and was never validated cannot be honestly
    # called "passed". The agent should use "failed" or retry the step.
    if validation_status == "passed":
        draft = _pipeline_state.get_draft(pipeline_id) or {}
        steps = draft.get("pipeline_steps", [])
        if 1 <= step <= len(steps):
            s = steps[step - 1]
            outs  = s.get("detected_outputs") or s.get("outputs") or []
            vlds  = s.get("validation") or {}
            if not outs and not vlds:
                return {
                    "error": "cannot mark step 'passed' with no detected outputs and no validation entries — "
                             "this is the 'ran without error but produced nothing' pattern. Investigate the step "
                             "or use validation_status='failed' if the run was actually broken.",
                    "pipeline_id": pipeline_id, "step": step,
                }
    ok = _pipeline_state.mark_pipeline_step_validated(pipeline_id, step, validation_status)
    return (
        {"status": "set", "pipeline_id": pipeline_id, "step": step,
         "validation_status": validation_status}
        if ok else
        {"error": "unknown pipeline_id or step out of range",
         "pipeline_id": pipeline_id, "step": step}
    )


@mcp.tool()
def run_install_command(
    env_name: str,
    command: str,
    installed_packages: list[dict] = [],
    working_dir: str = "",
    timeout_seconds: int = 1800,
    pipeline_id: str = "",
    step: int = 0,
    tool: str = "",
    subcommand: str = "",
    purpose: str = "",
    verify_command: str = "",
) -> dict:
    """Run an install command inside a conda environment (BiocManager::install,
    remotes::install_github, pip install, downloading reference DBs, etc.).

    This is the install-side mirror of run_in_env: same shape, same semantics,
    but the resulting step lands in draft.install_steps (not pipeline_steps)
    so the environment-build journey is recorded separately from the actual
    algorithm/analysis runs.

    installed_packages should list what this command installed:
        [{name: 'GAPIT', version: '4.1.0', channel: 'github', source: '...'}, ...]
    These power the side-by-side "command → packages" rendering in the report,
    AND each entry is appended to draft.packages as a PackageRecord (with an
    install_method derived from `channel`) — closing the loop with verify_installation,
    which can then patch verify_command / verify_output onto that record.

    Pass `step=N` to replace install_step N (for retries). Default is append.

    verify_command: an optional shell command run AFTER the install command in the
    same env. If supplied AND the install command returns 0, but the verify command
    returns non-zero, the step is recorded as failed (returncode=verify exit code,
    stderr includes the verify output). This catches silent-success cases like
    `R install.packages` printing 'ERROR: lazy loading failed' while the Rscript
    process still exits 0. Recommended for R installs:
        verify_command="Rscript -e 'if(!requireNamespace(\"GAPIT\")) quit(status=1)'"
    and for pip-source installs:
        verify_command="python -c 'import mypkg'"

    Return keys: returncode, stdout, stderr, success, command, runtime_seconds,
                 inputs, detected_outputs, [verify_returncode, verify_output],
                 [pipeline_merge]."""
    result = _env_mgr.run_in_env(
        env_name, command,
        working_dir=working_dir or None,
        timeout=timeout_seconds,
        inputs=[],
        watch_dir=None,
    )
    if verify_command and result.get("returncode") == 0:
        vresult = _env_mgr.run_in_env(
            env_name, verify_command,
            working_dir=working_dir or None,
            timeout=120,
        )
        verify_rc  = vresult.get("returncode", 1)
        verify_out = ((vresult.get("stdout") or "") + (vresult.get("stderr") or ""))[:500]
        result["verify_command"]   = verify_command
        result["verify_returncode"] = verify_rc
        result["verify_output"]    = verify_out
        if verify_rc != 0:
            result["returncode"] = verify_rc
            result["success"]    = False
            result["stderr"]     = (result.get("stderr") or "") + (
                f"\n\n[verify_command failed: {verify_command}]\n{verify_out}"
            )
    if pipeline_id:
        step_data = {
            "tool":               tool or (command.split() or [""])[0],
            "subcommand":         subcommand or None,
            "purpose":            purpose or None,
            "command":            command,
            "returncode":         result.get("returncode"),
            "runtime_seconds":    result.get("runtime_seconds"),
            "installed_packages": installed_packages or [],
        }
        if verify_command:
            step_data["verify_command"]   = verify_command
            step_data["verify_returncode"] = result.get("verify_returncode")
        step_data = {k: v for k, v in step_data.items() if v is not None}
        idx = _pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        # No more dual-write to draft.packages. The finalize-time package
        # builder picks up every `installed_packages` entry from successful
        # install_steps automatically — single source of truth.
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


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
# Async background job tools — watchdog-proof execution
#
# Use these for any operation that may run silently for >5 minutes:
#   - large downloads (Exomiser data bundles, BLAST nt, full genome FASTAs)
#   - long conda solves on big dependency graphs
#   - multi-hour assembler / aligner runs on real data
#   - any tool whose normal output stream is sparse
#
# Pattern:
#   r = run_in_background("curl -L --progress-bar -o big.zip 'URL' && unzip big.zip", env_name="bioinf_x")
#   # job_id returned immediately; agent does other work
#   while check_job(r['job_id'])['state'] == 'running':
#       sleep(30)   # or do unrelated tool calls
#   final = check_job(r['job_id'])
# ---------------------------------------------------------------------------


@mcp.tool()
def run_in_background(
    command: str,
    env_name: str = "",
    job_id: str = "",
    working_dir: str = "",
) -> dict:
    """Spawn `command` as a background process. Returns immediately.

    Use this for any operation that may run silently for more than 5 minutes —
    the agent stream-watchdog kills tool calls that go silent that long. Big
    downloads, conda solves, assembler runs, etc.

    Arguments:
      command:     full shell command (bash -c executes it; pipes / redirects OK)
      env_name:    if set, runs inside that conda env via `conda run --prefix`
      job_id:      caller-supplied (must be unique among running jobs).
                   If empty, a 12-char hex ID is auto-generated.
      working_dir: subprocess cwd (default: project root)

    Returns:
      {job_id, status_path, log_path, pid, state="running"}

    Use `check_job(job_id)` to poll. The status file is durable — survives
    server restarts and is queryable indefinitely after the job ends.
    """
    return _job_manager.start(
        command, env_name=env_name, job_id=job_id, working_dir=working_dir,
    )


@mcp.tool()
def check_job(job_id: str, log_tail_lines: int = 30) -> dict:
    """Return the current state of a background job. Non-blocking; ~50 ms.

    States: running | exited | cancelled
    Always includes a `log_tail` (last N lines of combined stdout+stderr) so you
    can monitor progress (e.g. curl's progress bar) without reading the full log
    file. `bytes_logged` is the size of the log so far — a download's progress
    can be inferred from this.

    For a job that has just exited, this is also where you learn it terminated —
    the state flips from "running" to "exited" on the first check after the
    subprocess died.
    """
    return _job_manager.check(job_id, log_tail_lines=log_tail_lines)


@mcp.tool()
def cancel_job(job_id: str, force: bool = False) -> dict:
    """Terminate a running job. SIGTERM by default; force=True sends SIGKILL.

    Always signals the whole process group so children (e.g. unzip launched
    after curl in a chained command) also die. A SIGTERM is followed by a 5 s
    grace window before promoting to SIGKILL.
    """
    return _job_manager.cancel(job_id, force=force)


@mcp.tool()
def list_jobs(include_terminated: bool = True) -> dict:
    """List all jobs ever started on this machine, newest first.

    include_terminated: when False, only currently-running jobs are returned.
    """
    return {"jobs": _job_manager.list_jobs(include_terminated=include_terminated)}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
