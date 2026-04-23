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

from pathlib import Path
from typing import Any, Optional

import yaml
from fastmcp import FastMCP

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
from agent.validators.output_validator import OutputValidator
from agent.skills.install_pipeline import InstallPipelineSkill  # for _save_spec / _write_provenance only

_pkg_search  = PackageSearch(config)
_env_mgr     = EnvManager(config)
_test_runner = TestRunner(config)
_docker      = DockerBuilder(config)
_validator   = OutputValidator(config)
_skill       = InstallPipelineSkill(config)   # Anthropic client stays None until run() is called

mcp = FastMCP("bioinf-agent")

# ---------------------------------------------------------------------------
# Research
# ---------------------------------------------------------------------------

@mcp.tool()
def search_package(package_name: str, requested_version: str = "latest") -> dict:
    """Search anaconda.org / bioconda / conda-forge / PyPI for a bioinformatics package.
    Returns channel, exact version, conda spec, install command, and brief description."""
    return _pkg_search.search(package_name, requested_version)

# ---------------------------------------------------------------------------
# Environment management
# ---------------------------------------------------------------------------

@mcp.tool()
def create_conda_env(env_name: str, python_version: str = "") -> dict:
    """Create a new isolated conda environment."""
    pv = python_version or config["conda"]["python_version"]
    return _env_mgr.create(env_name, python_version=pv)


@mcp.tool()
def install_packages(env_name: str, packages: list[dict]) -> dict:
    """Install packages into a conda env.
    packages: list of {spec: str, channel: str}, e.g. [{spec: 'samtools=1.21', channel: 'bioconda'}]
    conda-pack is added automatically."""
    return _env_mgr.install(env_name, packages)


@mcp.tool()
def verify_installation(env_name: str, package_name: str, check_command: str) -> dict:
    """Run a version/help command inside the env to confirm a package installed correctly."""
    return _env_mgr.verify(env_name, package_name, check_command)


@mcp.tool()
def run_in_env(
    env_name: str,
    command: str,
    working_dir: str = "",
    timeout_seconds: int = 1800,
) -> dict:
    """Run an arbitrary shell command inside a conda environment. Always use absolute paths."""
    return _env_mgr.run_in_env(
        env_name, command,
        working_dir=working_dir or None,
        timeout=timeout_seconds,
    )

# ---------------------------------------------------------------------------
# Resources
# ---------------------------------------------------------------------------

@mcp.tool()
def list_available_resources(resource_type: str = "both") -> dict:
    """List genomes and/or test datasets on disk.
    resource_type: 'genomes' | 'test_data' | 'both'"""
    from agent.tools import _tool_list_resources
    return _tool_list_resources({"resource_type": resource_type}, config)


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
    subset: str = "100K",
) -> dict:
    """Stream-download and register a new sequencing dataset from EBI SRA.
    assay_type: exome | wgs | rnaseq | chipseq | atacseq | hic | amplicon"""
    return _add_core_test_data(
        config, accession, assay_type,
        end_type=end_type, genome_build=genome_build,
        sample=sample, subset=subset,
    )

# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

@mcp.tool()
def validate_output(file_path: str, expected_type: str, env_name: str = "") -> dict:
    """Validate a bioinformatics output file is non-empty and parseable.
    expected_type: sam | bam | fastq | fasta | vcf | bcf | bed | bigwig |
                   bim | fam | ld | frq | prune | tsv | csv | txt | log | any"""
    return _validator.validate(file_path, expected_type, env_name=env_name or None)

# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

@mcp.tool()
def build_docker_image(
    env_name: str,
    pipeline_name: str,
    pipeline_description: str,
    version: str = "",
) -> dict:
    """Package a conda env into an HPC-compatible Docker image via conda-pack.
    version: resolved version string for the image tag, e.g. '1.21'. Defaults to 'latest'."""
    return _docker.build(env_name, pipeline_name, pipeline_description, version=version)

# ---------------------------------------------------------------------------
# Artifacts
# ---------------------------------------------------------------------------

@mcp.tool()
def save_pipeline_report(spec: dict) -> dict:
    """Validate and write the pipeline spec as YAML + HTML report to env_reports/.
    spec must include: pipeline_name, description, conda_env, created_at, status,
    packages (list), pipeline_steps (list), docker (dict)."""
    return _skill._save_spec(spec)


@mcp.tool()
def write_pipeline_provenance(
    pipeline: str,
    conda_env_path: str,
    pipeline_spec_path: str,
    genome_build: str,
    chromosome: str,
    reference_path: str,
    output_files: list[dict],
    output_dir: str,
    sample_key: str,
    reads: Optional[dict] = None,
    bam_input: Optional[dict] = None,
    upstream_pipelines: Optional[list[str]] = None,
    parameters: Optional[dict] = None,
) -> dict:
    """Write a validated provenance YAML for a completed pipeline run.
    output_files: list of {file: str, type: str, indexed: bool}
    reads: {r1, r2?, sample, accession, subset, num_reads, assay_type, end_type, database}
    bam_input: {bam: str, bai: str}"""
    inputs: dict[str, Any] = {
        "pipeline":          pipeline,
        "conda_env_path":    conda_env_path,
        "pipeline_spec_path": pipeline_spec_path,
        "genome_build":      genome_build,
        "chromosome":        chromosome,
        "reference_path":    reference_path,
        "output_files":      output_files,
        "output_dir":        output_dir,
        "sample_key":        sample_key,
    }
    if reads:               inputs["reads"]               = reads
    if bam_input:           inputs["bam_input"]           = bam_input
    if upstream_pipelines:  inputs["upstream_pipelines"]  = upstream_pipelines
    if parameters:          inputs["parameters"]          = parameters
    return _skill._write_provenance(inputs)


@mcp.tool()
def list_installed_pipelines() -> dict:
    """List all pipelines installed and validated, with Docker tags and validation status."""
    from agent.tools import _tool_list_pipelines
    return _tool_list_pipelines(config)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    mcp.run()
