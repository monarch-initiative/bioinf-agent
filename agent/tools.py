"""
Outer agent tool definitions and dispatcher.

These are the tools visible to the top-level conversational agent.
The install_pipeline tool internally spawns its own sub-agent loop
with a richer set of execution-level tools.
"""

from typing import Any

from agent.skills.core_test_data import add_core_test_data, add_phenopacket
from agent.skills.install_pipeline import InstallPipelineSkill
from agent.skills.resources import list_resources, list_pipelines

# ---------------------------------------------------------------------------
# Tool schemas (passed to Claude messages.create)
# ---------------------------------------------------------------------------

OUTER_TOOLS = [
    {
        "name": "add_core_test_data",
        "description": (
            "Download and register a new sequencing dataset from EBI SRA into core_test_data "
            "so it is available as test data for pipeline installations. "
            "Downloads the full run to a local cache, subsets to a manageable read count, "
            "writes a validated SampleMeta sidecar, and rebuilds the manifest. "
            "Use this when the user wants to add a new sample, assay type, or organism "
            "to the test data pool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "accession": {
                    "type": "string",
                    "description": "SRA accession (SRR/ERR/DRR prefix), e.g. SRR1517830",
                },
                "assay_type": {
                    "type": "string",
                    "enum": ["exome", "wgs", "rnaseq", "chipseq", "atacseq", "hic", "amplicon",
                             "wgbs", "ont_wgs", "pacbio_hifi", "direct_rna", "isoseq", "fiberseq"],
                    "description": "Type of sequencing assay",
                },
                "end_type": {
                    "type": "string",
                    "enum": ["paired_end", "single_end"],
                    "description": "Read layout (default: paired_end; long-read platforms force single_end)",
                },
                "genome_build": {
                    "type": "string",
                    "description": "Reference genome build, e.g. hg38, mm39 (default: hg38)",
                },
                "sample": {
                    "type": "string",
                    "description": (
                        "Sample ID, e.g. HG00096. Defaults to the accession if not provided. "
                        "Used as a prefix in all output filenames."
                    ),
                },
                "subset": {
                    "type": "string",
                    "description": "Read count: 500 | 1K | 10K (default) | 50K | 100K | 500K | 1M. Use 500 for long-read platforms.",
                },
                "platform": {
                    "type": "string",
                    "enum": ["illumina", "ont", "pacbio_hifi", "pacbio_isoseq", "pacbio_fiberseq"],
                    "description": "Sequencing platform (default: illumina). Long-read platforms store data under long_read/{ont|pacbio}/.",
                },
                "source_url": {
                    "type": "string",
                    "description": (
                        "Override the EBI SRA URL builder. Use for data on NCBI FTP, S3, or any other "
                        "public FASTQ URL. For paired-end data also supply source_url_r2."
                    ),
                },
                "source_url_r2": {
                    "type": "string",
                    "description": "R2 URL when source_url is specified and data is paired-end.",
                },
            },
            "required": ["accession", "assay_type"],
        },
    },
    {
        "name": "add_phenopacket",
        "description": (
            "Download and register a GA4GH phenopacket JSON into core_test_data. "
            "All metadata (HPO terms, diseases, genes, variants) is extracted from the "
            "JSON itself — nothing is supplied manually. Idempotent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "source_url": {
                    "type": "string",
                    "description": "Direct URL to a phenopacket JSON file.",
                },
                "genome_build": {
                    "type": "string",
                    "description": "Target core_test_data directory, e.g. hg38 (default).",
                },
            },
            "required": ["source_url"],
        },
    },
    {
        "name": "install_pipeline",
        "description": (
            "Install one or more bioinformatics tools as a named pipeline. "
            "Searches the internet for the correct package/version, creates an isolated "
            "conda environment, installs all packages, runs each tool against appropriate "
            "test data to validate it works, chains outputs between pipeline steps, and "
            "finally builds an HPC-compatible Docker image. "
            "Use this for any request to install a tool or pipeline, whether it's a single "
            "algorithm (e.g. 'bwa') or a multi-step pipeline (e.g. 'STAR + featureCounts')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pipeline_name": {
                    "type": "string",
                    "description": (
                        "Short snake_case name for this pipeline, e.g. 'bwa', "
                        "'rnaseq_star_featurecounts', 'variant_calling_gatk'. "
                        "Used as the conda env name and Docker image tag."
                    ),
                },
                "packages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {
                                "type": "string",
                                "description": "Package name as the user specified it, e.g. 'bwa', 'STAR', 'featureCounts'",
                            },
                            "version": {
                                "type": "string",
                                "description": "Specific version if the user requested one, otherwise 'latest'",
                            },
                        },
                        "required": ["name", "version"],
                    },
                    "description": "Ordered list of packages to install. Order matters — later steps receive output from earlier ones.",
                },
                "description": {
                    "type": "string",
                    "description": "One-sentence description of what this pipeline does, inferred from the user's request.",
                },
            },
            "required": ["pipeline_name", "packages", "description"],
        },
    },
    {
        "name": "list_available_resources",
        "description": (
            "List the genomes and test datasets currently available on disk. "
            "Use this when the user asks what reference data or test data is available, "
            "or before starting an install to understand what's already cached."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resource_type": {
                    "type": "string",
                    "enum": ["genomes", "test_data", "both"],
                    "description": "Which resource manifest to read.",
                }
            },
            "required": ["resource_type"],
        },
    },
    {
        "name": "list_installed_pipelines",
        "description": (
            "List all pipelines that have already been installed and validated, "
            "along with their Docker image tags and validation status."
        ),
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def dispatch_outer_tool(name: str, inputs: dict, config: dict) -> dict[str, Any]:
    if name == "add_core_test_data":
        return _tool_add_core_test_data(inputs, config)
    if name == "add_phenopacket":
        return _tool_add_phenopacket(inputs, config)
    if name == "install_pipeline":
        return _tool_install_pipeline(inputs, config)
    if name == "list_available_resources":
        return _tool_list_resources(inputs, config)
    if name == "list_installed_pipelines":
        return _tool_list_pipelines(config)
    return {"error": f"Unknown tool: {name}"}


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

def _tool_add_phenopacket(inputs: dict, config: dict) -> dict:
    return add_phenopacket(
        config=config,
        source_url=inputs["source_url"],
        genome_build=inputs.get("genome_build", "hg38"),
    )


def _tool_add_core_test_data(inputs: dict, config: dict) -> dict:
    return add_core_test_data(
        config=config,
        accession=inputs["accession"],
        assay_type=inputs["assay_type"],
        end_type=inputs.get("end_type", "paired_end"),
        genome_build=inputs.get("genome_build", "hg38"),
        sample=inputs.get("sample", ""),
        subset=inputs.get("subset", "10K"),
        platform=inputs.get("platform", "illumina"),
        source_url=inputs.get("source_url", ""),
        source_url_r2=inputs.get("source_url_r2", ""),
    )


def _tool_install_pipeline(inputs: dict, config: dict) -> dict:
    pkgs = ", ".join(
        p["name"] + (f"@{p['version']}" if p.get("version") and p["version"] != "latest" else "")
        for p in inputs["packages"]
    )
    print(f"\n[pipeline] {inputs['pipeline_name']}  ({pkgs})")

    skill = InstallPipelineSkill(config)
    return skill.run(
        pipeline_name=inputs["pipeline_name"],
        packages=inputs["packages"],
        description=inputs["description"],
    )


def _tool_list_resources(inputs: dict, config: dict) -> dict:
    return list_resources(inputs, config)


def _tool_list_pipelines(config: dict) -> dict:
    return list_pipelines(config)
