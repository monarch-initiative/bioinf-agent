"""
InstallPipelineSkill — the core skill.

Runs a sub-agent loop (its own Claude tool-use conversation) that:
  1. Searches the internet for each requested package
  2. Creates an isolated conda environment
  3. Installs each package in order
  4. Figures out what the tool does and which test data fits best
  5. Runs the tool on test data with reasonable defaults
  6. Validates the output; chains it to the next step if pipelined
  7. Builds an HPC-compatible Docker image on success
  8. Saves a pipeline spec YAML as an artifact
"""

import json
import subprocess
import textwrap
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import anthropic
import yaml

from agent.skills.docker_builder import DockerBuilder
from agent.skills.env_manager import EnvManager
from agent.skills.package_search import PackageSearch
from agent.skills.report_builder import generate as generate_report
from agent.skills.test_runner import TestRunner
from agent.validators.output_validator import OutputValidator


# ---------------------------------------------------------------------------
# Sub-agent tool schemas
# ---------------------------------------------------------------------------

SUB_TOOLS = [
    {
        "name": "search_package",
        "description": (
            "Search the internet (anaconda.org, bioconda, conda-forge, PyPI, GitHub) "
            "to find the correct conda channel, latest version (or a specific version), "
            "and install command for a bioinformatics package. "
            "Also returns a brief description of what the package does and what types "
            "of input/output it expects."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "package_name": {"type": "string"},
                "requested_version": {
                    "type": "string",
                    "description": "'latest' or a specific version string like '2.7.11b'",
                },
            },
            "required": ["package_name", "requested_version"],
        },
    },
    {
        "name": "create_conda_env",
        "description": "Create a new isolated conda environment for this pipeline.",
        "input_schema": {
            "type": "object",
            "properties": {
                "env_name": {"type": "string"},
                "python_version": {
                    "type": "string",
                    "description": "Python version, e.g. '3.11'. Use the config default if unsure.",
                },
            },
            "required": ["env_name"],
        },
    },
    {
        "name": "install_packages",
        "description": "Install one or more packages into the conda environment.",
        "input_schema": {
            "type": "object",
            "properties": {
                "env_name": {"type": "string"},
                "packages": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "spec": {
                                "type": "string",
                                "description": "Full conda package spec, e.g. 'bwa=0.7.17' or 'star=2.7.11b'",
                            },
                            "channel": {
                                "type": "string",
                                "description": "conda channel, e.g. 'bioconda', 'conda-forge'",
                            },
                        },
                        "required": ["spec", "channel"],
                    },
                },
            },
            "required": ["env_name", "packages"],
        },
    },
    {
        "name": "verify_installation",
        "description": (
            "Verify a package installed correctly by running its help/version command "
            "inside the conda environment."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "env_name": {"type": "string"},
                "package_name": {"type": "string"},
                "check_command": {
                    "type": "string",
                    "description": "Command to run that confirms the install worked, e.g. 'bwa 2>&1 | head -5' or 'STAR --version'",
                },
            },
            "required": ["env_name", "package_name", "check_command"],
        },
    },
    {
        "name": "list_available_resources",
        "description": (
            "Read the genomes and test_data manifests to see what reference data and "
            "test datasets are currently on disk. Use this to pick the best test data "
            "for a given algorithm."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resource_type": {
                    "type": "string",
                    "enum": ["genomes", "test_data", "both"],
                }
            },
            "required": ["resource_type"],
        },
    },
    {
        "name": "download_resource",
        "description": (
            "Download a reference genome that is not yet on disk. "
            "Only call this when the genome FASTA is not already available — "
            "check list_available_resources first."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "resource_type": {
                    "type": "string",
                    "enum": ["genome"],
                },
                "resource_id": {
                    "type": "string",
                    "description": "Genome build id from the manifest, e.g. 'hg38_chr22'",
                },
            },
            "required": ["resource_type", "resource_id"],
        },
    },
    {
        "name": "run_command",
        "description": (
            "Run an arbitrary shell command inside the conda environment. "
            "Use this to execute algorithm steps, index genomes, or pre-process inputs. "
            "Always use absolute paths. stdout and stderr are captured and returned."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "env_name": {"type": "string"},
                "command": {"type": "string", "description": "Full shell command to run"},
                "working_dir": {
                    "type": "string",
                    "description": "Working directory for the command (absolute path)",
                },
                "timeout_seconds": {
                    "type": "integer",
                    "description": "Max seconds to wait. Default 1800.",
                },
            },
            "required": ["env_name", "command"],
        },
    },
    {
        "name": "validate_output",
        "description": (
            "Validate that an output file is non-empty, parseable, and of the expected "
            "bioinformatics file type. Returns pass/fail with details."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Absolute path to the output file"},
                "expected_type": {
                    "type": "string",
                    "description": (
                        "Expected file type: 'sam', 'bam', 'fastq', 'fasta', 'vcf', 'bcf', "
                        "'bed', 'bigwig', 'counts_matrix', 'gtf', 'gff', 'log', "
                        "'bim', 'fam', 'ld', 'frq', 'prune', 'tsv', 'csv', 'txt', 'any'"
                    ),
                },
                "env_name": {
                    "type": "string",
                    "description": (
                        "Conda env name. Always pass this — it lets the validator call "
                        "samtools/bcftools from inside the env rather than system PATH."
                    ),
                },
            },
            "required": ["file_path", "expected_type"],
        },
    },
    {
        "name": "build_docker_image",
        "description": (
            "Package the conda environment into an HPC-compatible Docker image using conda-pack. "
            "Call this only after all pipeline steps have been validated successfully."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "env_name": {"type": "string"},
                "pipeline_name": {"type": "string"},
                "pipeline_description": {"type": "string"},
                "version": {
                    "type": "string",
                    "description": "Resolved version string of the primary package, e.g. '1.90b7.7'. Used to tag the Docker image.",
                },
            },
            "required": ["env_name", "pipeline_name", "pipeline_description"],
        },
    },
    {
        "name": "save_pipeline_spec",
        "description": (
            "Save the final pipeline specification as a YAML artifact. "
            "Call this as the last step after Docker build succeeds."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "spec": {
                    "type": "object",
                    "description": "Full pipeline spec dict to serialize",
                }
            },
            "required": ["spec"],
        },
    },
    {
        "name": "write_pipeline_provenance",
        "description": (
            "Write a validated provenance YAML for a completed pipeline run using the shared "
            "core data model. Call this after validate_output succeeds. "
            "The provenance file is written as {output_dir}/{sample_key}_provenance.yaml. "
            "All input paths (reference, reads, bam) must be absolute — relative paths inside "
            "the YAML are computed automatically."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pipeline": {
                    "type": "string",
                    "description": "Pipeline name, e.g. 'bwa_samtools', 'freebayes', 'star'",
                },
                "conda_env_path": {
                    "type": "string",
                    "description": "Absolute path to the conda environment directory",
                },
                "pipeline_spec_path": {
                    "type": "string",
                    "description": "Absolute path to the pipeline spec YAML (config/pipelines/*.yaml)",
                },
                "genome_build": {"type": "string"},
                "chromosome":   {"type": "string", "description": "e.g. 'chr22' or 'all'"},
                "reference_path": {
                    "type": "string",
                    "description": "Absolute path to reference FASTA",
                },
                "reads": {
                    "type": "object",
                    "description": (
                        "Read inputs for alignment pipelines. "
                        "Fields: r1 (abs path), r2 (abs path, optional), sample, accession, "
                        "subset (e.g. '100K'), num_reads (int), assay_type, end_type, database"
                    ),
                },
                "bam_input": {
                    "type": "object",
                    "description": (
                        "BAM inputs for variant-calling pipelines. "
                        "Fields: bam (abs path), bai (abs path)"
                    ),
                },
                "upstream_pipelines": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Names of upstream pipelines whose outputs were consumed",
                },
                "parameters": {
                    "type": "object",
                    "description": "Key-value pairs of parameters passed to the tool",
                },
                "output_files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "file":    {"type": "string", "description": "Filename (no directory)"},
                            "type":    {"type": "string", "description": "fastq|bam|sam|bai|vcf|bcf|bed|bigwig|bim|fam|ld|frq|prune|tsv|csv|txt|log|yaml"},
                            "indexed": {"type": "boolean"},
                        },
                        "required": ["file", "type"],
                    },
                    "description": "Output files produced by this pipeline run",
                },
                "output_dir": {
                    "type": "string",
                    "description": "Absolute path to the directory where outputs were written",
                },
                "sample_key": {
                    "type": "string",
                    "description": "'{sample}_{accession}' key, e.g. 'HG00096_SRR1517830'",
                },
            },
            "required": [
                "pipeline", "conda_env_path", "pipeline_spec_path",
                "genome_build", "chromosome", "reference_path",
                "output_files", "output_dir", "sample_key",
            ],
        },
    },
]

_TOOL_PHASES = {
    "search_package":            "Phase 1 · Research",
    "create_conda_env":          "Phase 2 · Install",
    "install_packages":          "Phase 2 · Install",
    "verify_installation":       "Phase 2 · Install",
    "list_available_resources":  "Phase 3 · Test data",
    "download_resource":         "Phase 3 · Test data",
    "run_command":               "Phase 4 · Execution",
    "validate_output":           "Phase 4 · Validation",
    "write_pipeline_provenance": "Phase 5 · Provenance",
    "build_docker_image":        "Phase 6 · Docker",
    "save_pipeline_spec":        "Phase 7 · Save",
}

# ---------------------------------------------------------------------------
# Skill
# ---------------------------------------------------------------------------

class InstallPipelineSkill:
    def __init__(self, config: dict):
        self.config = config
        self._client = None  # lazy — only needed for the sub-agent run() path
        self.env_manager = EnvManager(config)
        self.package_search = PackageSearch(config)
        self.test_runner = TestRunner(config)
        self.docker_builder = DockerBuilder(config)
        self.validator = OutputValidator(config)

    def run(self, pipeline_name: str, packages: list[dict], description: str) -> dict:
        env_name = self.config["conda"]["env_prefix"] + pipeline_name
        project_root = Path(__file__).parent.parent.parent.resolve()

        system = textwrap.dedent(f"""
            You are a bioinformatics software engineer executing a pipeline installation job.

            Pipeline: {pipeline_name}
            Description: {description}
            Packages requested (in order): {json.dumps(packages)}
            Conda env name: {env_name}
            Project root: {project_root}
            Data dir: {project_root / self.config['paths']['data_dir']}

            ## Your job (execute ALL steps):

            ### Phase 1 — Research
            For each package:
            - Call search_package to find the correct conda channel, exact version, and
              understand what the tool does and what input/output types it expects.

            ### Phase 2 — Install
            - Call create_conda_env to make the environment.
            - Call install_packages with all packages at once if possible (better for
              dependency resolution). Fall back to one-by-one if needed.
            - Call verify_installation for each package.

            ### Phase 3 — Test data selection
            - Call list_available_resources(both) to see what's on disk.
            - Based on your understanding of what each tool does, select the most
              appropriate test dataset. Prefer data that is already available on disk.
            - If no suitable data is available, call download_resource.
            - **Default test strategy (always apply unless the tool makes it inappropriate):**
              1. Reference genome: extract a single chromosome to keep index builds fast.
                 Prefer "{self.config['testing']['preferred_chromosome']}", fall back to
                 "{self.config['testing']['fallback_chromosome']}", or the first sequence
                 in the FASTA for non-human/non-mouse organisms.
              2. Reads: subset to {self.config['testing']['max_reads']:,} paired-end reads
                 (or single-end if the dataset is single-end). Use head/gzip to subset
                 without downloading extra data.
              3. Write all test outputs to `{project_root / self.config['paths']['data_dir']}/{pipeline_name}_test_data/`.
              This keeps validation fast (<10 min) and reproducible.
            - If the genome needs an index for this tool and it doesn't exist yet,
              build it with run_command before the main test run.
            - **Reusing core test data:** The canonical test data lives at
              `{project_root / self.config['paths']['data_dir']}/core_test_data_{{genome_build}}/`
              (e.g. `core_test_data_hg38/`). Always check here first before generating new data:
                - Genome + indexes: `core_test_data_hg38/genome/chr22.fa` (+ .fai, .amb, .ann, .bwt, .pac, .sa)
                - Reads: `core_test_data_hg38/short_read/paired_end/exome/HG00096_SRR1517830_100K_R1.fastq.gz`
                - Pre-built pipeline outputs (BAM, VCF, etc.): `core_test_data_hg38/pipeline_outputs/{{pipeline}}/`
                  e.g. sorted BAM at `core_test_data_hg38/pipeline_outputs/bwa_samtools/HG00096_SRR1517830_100K_aligned_sorted.bam`
              Read `core_test_data_hg38/manifest.yaml` to discover exactly what is available.
              If core data exists, use it directly — do not re-extract or re-subset reads.
              Each `pipeline_outputs/{{name}}/` dir has `{{sample_key}}_provenance.yaml` recording the exact
              commands, versions, and upstream inputs that produced those files.
              Write your own outputs to `{project_root / self.config['paths']['data_dir']}/{pipeline_name}_test_data/`,
              but reference core inputs by their full core path (do not copy them).

            ### Phase 4 — Validation loop (one step per package)
            For each package in pipeline order:
            - Construct a reasonable test command using the test data.
              Use sensible default parameters appropriate for small test data.
              Write outputs to the {pipeline_name}_test_data/ subdirectory under data/.
            - Call run_command to execute it.
            - If the command succeeds, call validate_output on the primary output.
            - If validation passes, record the step as validated.
            - The output of this step becomes the input for the next step.

            ### Phase 5 — Provenance (REQUIRED after validation)
            - Call write_pipeline_provenance with the exact output files you produced.
              This is NOT optional. Every validated pipeline run must have a provenance record.
              Pass all absolute paths; relative paths inside the YAML are computed automatically.
              Use the test_data info from list_available_resources to fill genome_build,
              chromosome, reference_path, reads or bam_input, and sample_key.

            ### Phase 6 — Docker + Spec
            - Call build_docker_image.
            - Call save_pipeline_spec with the complete record of what was installed,
              how it was tested, and the Docker image tag.
            - The spec MUST use these exact top-level field names (PipelineSpec model):
              - `pipeline_name` (not "name")
              - `description` (REQUIRED — one-sentence summary of what this pipeline does)
              - `conda_env` (REQUIRED — the conda env name, e.g. "bioinf_plink")
              - `created_at` (not "created"), ISO date string
              - `status`: "fully_validated" if all steps passed, else "failed"
              - `packages`: list of package dicts with `name`, `resolved_version`, `channel`,
                `description`, `homepage` (optional), `verify_command` (optional)
              - `test_data`: dict with controlled vocab fields: `genome_build`,
                `chromosome_subset`, `read_type`, `end_type`, `assay_type`, `sample`,
                `accession`, `subset`, `num_reads`, `r1`, `r2`, `reference_fasta`,
                `core_data_dir`, `upstream_pipelines` (list)
              - `pipeline_steps`: list of step dicts, one per tool invocation, each with:
                `step` (int), `tool`, `subcommand` (if any), `command` (full command string),
                `status` ("validated" or "failed"), and optionally `returncode`,
                `validation` (dict or string), `output_size_bytes`, `runtime_seconds`
              - `docker`: dict with `build_attempted`, `build_success`, `image_tag`, `reason`
              - `notes`: list of strings for notable observations
              Do NOT use a flat `test` dict — always use the `pipeline_steps` list format
              so the HTML report renders correctly.

            ## Rules
            - Always use absolute paths in commands.
            - If a step fails, diagnose and retry up to 2 times before reporting failure.
            - Prefer bioconda > conda-forge > defaults channel priority.
            - conda-pack must be installed in the env before building the image.
            - Always pass env_name to validate_output so samtools/bcftools are resolved
              from inside the conda env rather than the system PATH.
            - htslib: samtools, bcftools, and bwa (for CRAM support) all link against htslib.
              Install them all in the same conda solve to guarantee a compatible htslib version.
              Do NOT install them in separate install_packages calls if it can be avoided.
        """).strip()

        messages = [
            {
                "role": "user",
                "content": (
                    f"Install the pipeline '{pipeline_name}': {description}. "
                    f"Packages: {[p['name'] + ('@' + p['version'] if p['version'] != 'latest' else '') for p in packages]}. "
                    "Execute all phases and return when done."
                ),
            }
        ]

        pipeline_spec = {
            "name": pipeline_name,
            "description": description,
            "conda_env": env_name,
            "created": datetime.now(timezone.utc).isoformat(),
            "steps": [],
            "docker_image": None,
            "status": "in_progress",
        }

        _current_phase = None
        _phase_start = time.time()
        _job_start = time.time()

        if self._client is None:
            self._client = anthropic.Anthropic()

        max_iterations = self.config["agent"]["max_iterations"]
        for iteration in range(max_iterations):
            response = self._client.messages.create(
                model=self.config["agent"]["model"],
                max_tokens=8096,
                system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                tools=SUB_TOOLS,
                messages=messages,
            )

            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "end_turn":
                pipeline_spec["status"] = "complete"
                elapsed = time.time() - _job_start
                print(f"\n  ✓ Pipeline complete ({elapsed:.0f}s total)")
                for block in response.content:
                    if hasattr(block, "text"):
                        pipeline_spec["final_summary"] = block.text
                break

            if response.stop_reason == "tool_use":
                tool_results = []
                for block in response.content:
                    if block.type == "tool_use":
                        phase = _TOOL_PHASES.get(block.name, "Running")
                        if phase != _current_phase:
                            if _current_phase:
                                print(f"    ({time.time() - _phase_start:.0f}s)")
                            print(f"\n  ── {phase} ", end="", flush=True)
                            _current_phase = phase
                            _phase_start = time.time()
                        _print_tool_call(block.name, block.input)
                        result = self._dispatch(block.name, block.input, pipeline_spec, env_name)
                        tool_results.append(
                            {
                                "type": "tool_result",
                                "tool_use_id": block.id,
                                "content": json.dumps(result),
                            }
                        )
                messages.append({"role": "user", "content": tool_results})
        else:
            pipeline_spec["status"] = "timeout"
            print(f"\n  ✗ Timed out after {time.time() - _job_start:.0f}s")

        return pipeline_spec

    # -----------------------------------------------------------------------
    # Tool dispatcher
    # -----------------------------------------------------------------------

    def _dispatch(
        self, name: str, inputs: dict, pipeline_spec: dict, env_name: str
    ) -> dict[str, Any]:
        if name == "search_package":
            return self.package_search.search(
                inputs["package_name"], inputs.get("requested_version", "latest")
            )

        if name == "create_conda_env":
            return self.env_manager.create(
                inputs["env_name"],
                python_version=inputs.get("python_version", self.config["conda"]["python_version"]),
            )

        if name == "install_packages":
            return self.env_manager.install(inputs["env_name"], inputs["packages"])

        if name == "verify_installation":
            return self.env_manager.verify(
                inputs["env_name"], inputs["package_name"], inputs["check_command"]
            )

        if name == "list_available_resources":
            return self._list_resources(inputs["resource_type"])

        if name == "download_resource":
            return self.test_runner.download_resource(
                inputs["resource_type"], inputs["resource_id"]
            )

        if name == "run_command":
            result = self.env_manager.run_in_env(
                inputs["env_name"],
                inputs["command"],
                working_dir=inputs.get("working_dir"),
                timeout=inputs.get("timeout_seconds", 1800),
            )
            return result

        if name == "validate_output":
            result = self.validator.validate(
                inputs["file_path"],
                inputs["expected_type"],
                env_name=inputs.get("env_name", env_name),
            )
            if result.get("passed"):
                self._record_step_validation(pipeline_spec, inputs["file_path"], result)
            return result

        if name == "build_docker_image":
            result = self.docker_builder.build(
                inputs["env_name"],
                inputs["pipeline_name"],
                inputs.get("pipeline_description", ""),
                version=inputs.get("version", ""),
            )
            if result.get("success"):
                pipeline_spec["docker_image"] = result["image_tag"]
            return result

        if name == "save_pipeline_spec":
            return self._save_spec(inputs["spec"])

        if name == "write_pipeline_provenance":
            return self._write_provenance(inputs)

        return {"error": f"Unknown sub-tool: {name}"}

    # -----------------------------------------------------------------------
    # Helpers
    # -----------------------------------------------------------------------

    def _list_resources(self, resource_type: str) -> dict:
        from agent.tools import _tool_list_resources
        return _tool_list_resources({"resource_type": resource_type}, self.config)

    def _record_step_validation(self, pipeline_spec: dict, file_path: str, result: dict):
        pipeline_spec["steps"].append(
            {
                "output_file": file_path,
                "validation": "passed",
                "details": result,
            }
        )

    def _save_spec(self, spec: dict) -> dict:
        from agent.models.core_data import PipelineSpec

        pipelines_dir = Path(__file__).parent.parent.parent / self.config["paths"]["pipelines_dir"]
        pipelines_dir.mkdir(parents=True, exist_ok=True)

        # Normalise legacy field names before validation
        if "name" in spec and "pipeline_name" not in spec:
            spec["pipeline_name"] = spec.pop("name")
        if "created" in spec and "created_at" not in spec:
            spec["created_at"] = spec.pop("created")
        # Wrap flat docker_image / docker_build_status into a docker dict
        if "docker_image" in spec and "docker" not in spec:
            tag = spec.pop("docker_image", None)
            note = spec.pop("docker_build_status", "")
            spec["docker"] = {
                "build_attempted": True,
                "build_success": bool(tag and str(tag) not in ("null", "None")),
                "image_tag": tag if tag and str(tag) not in ("null", "None") else None,
                "reason": note,
            }

        try:
            pspec = PipelineSpec.model_validate(spec)
            write_spec = pspec.model_dump(exclude_none=True)
        except Exception as e:
            import sys
            print(f"[install_pipeline] WARN: PipelineSpec validation failed: {e}", file=sys.stderr)
            write_spec = spec

        name = write_spec.get("pipeline_name", "pipeline")
        primary = next(
            (p for p in write_spec.get("packages", []) if p.get("name") != "conda-pack"), {}
        )
        version = primary.get("resolved_version") or primary.get("version", "")
        stem = f"{name}_{version}" if version else name

        yaml_path = pipelines_dir / f"{stem}.yaml"
        with open(yaml_path, "w") as f:
            yaml.dump(write_spec, f, default_flow_style=False, sort_keys=False)

        html_path = pipelines_dir / f"{stem}.html"
        html_path.write_text(generate_report(write_spec))

        return {"saved_yaml": str(yaml_path), "saved_html": str(html_path)}

    def _write_provenance(self, inputs: dict) -> dict:
        from agent.models.core_data import (
            BamInput, GenomeRef, OutputFile, Provenance, ReadInput,
        )
        from datetime import date

        project_root = Path(__file__).parent.parent.parent.resolve()
        output_dir = Path(inputs["output_dir"])
        sample_key = inputs["sample_key"]
        prov_path = output_dir / f"{sample_key}_provenance.yaml"

        def _rel(abs_path: str) -> str:
            import os
            return os.path.relpath(Path(abs_path).resolve(), output_dir.resolve())

        genome = GenomeRef(
            genome_build=inputs["genome_build"],
            chromosome_subset=inputs["chromosome"],
            reference=_rel(inputs["reference_path"]),
            reference_fai=_rel(inputs["reference_path"] + ".fai"),
        )

        _DB_ALIASES = {"SRA": "EBI_SRA", "NCBI": "NCBI_SRA", "EBI": "EBI_SRA"}

        reads = None
        if inputs.get("reads"):
            r = inputs["reads"]
            raw_db = r.get("database", "EBI_SRA")
            db = _DB_ALIASES.get(raw_db, raw_db)
            reads = [ReadInput(
                read_type=r.get("read_type", "short_read"),
                end_type=r.get("end_type", "paired_end"),
                assay_type=r.get("assay_type", "exome"),
                subset=r.get("subset", ""),
                num_reads=int(r.get("num_reads", 0)),
                r1=_rel(r["r1"]),
                r2=_rel(r["r2"]) if r.get("r2") else None,
                sample=r.get("sample", ""),
                accession=r.get("accession", ""),
                database=db,
            )]

        bam_input = None
        if inputs.get("bam_input"):
            b = inputs["bam_input"]
            bam_input = BamInput(bam=_rel(b["bam"]), bai=_rel(b["bai"]))

        pipeline_spec_path = Path(inputs["pipeline_spec_path"]).resolve()
        try:
            spec_rel = str(pipeline_spec_path.relative_to(output_dir.resolve()))
        except ValueError:
            spec_rel = str(pipeline_spec_path)

        outputs = [
            OutputFile(
                file=f["file"],
                type=f["type"],
                indexed=f.get("indexed", False),
            )
            for f in inputs.get("output_files", [])
        ]

        # Discover tool versions from the conda env
        conda_env_path = inputs["conda_env_path"]
        from scripts.gen_provenance import _PIPELINE_TOOLS, _discover_version
        tools = _PIPELINE_TOOLS.get(inputs["pipeline"], [])
        tool_versions = {t: _discover_version(conda_env_path, t) for t in tools}

        prov = Provenance(
            pipeline=inputs["pipeline"],
            pipeline_spec=spec_rel,
            conda_env=Path(conda_env_path).name,
            created_at=str(date.today()),
            tool_versions=tool_versions,
            genome=genome,
            reads=reads,
            bam_input=bam_input,
            upstream_pipelines=inputs.get("upstream_pipelines", []),
            parameters=inputs.get("parameters") or None,
            outputs=outputs,
        )

        written = prov.write(prov_path)
        return {"written": str(written), "sample_key": sample_key}


_TOOL_LABELS = {
    "search_package":           lambda i: i.get("package_name", ""),
    "create_conda_env":         lambda i: i.get("env_name", ""),
    "install_packages":         lambda i: ", ".join(p.get("spec", "") for p in i.get("packages", [])),
    "verify_installation":      lambda i: i.get("package_name", ""),
    "list_available_resources": lambda i: i.get("resource_type", ""),
    "download_resource":        lambda i: i.get("resource_id", ""),
    "run_command":              lambda i: (i.get("command", "")[:60] + "…") if len(i.get("command", "")) > 60 else i.get("command", ""),
    "validate_output":          lambda i: Path(i.get("file_path", "")).name + f" [{i.get('expected_type','')}]",
    "build_docker_image":           lambda i: i.get("pipeline_name", ""),
    "save_pipeline_spec":           lambda i: i.get("spec", {}).get("pipeline_name", ""),
    "write_pipeline_provenance":    lambda i: i.get("sample_key", ""),
}


def _print_tool_call(name: str, inputs: dict) -> None:
    label = _TOOL_LABELS.get(name, lambda i: "")(inputs)
    print(f"\n      · {name}: {label}" if label else f"\n      · {name}", end="", flush=True)


def _short(inp: dict) -> str:
    parts = []
    for k, v in inp.items():
        s = str(v)
        parts.append(f"{k}={s[:40] + '…' if len(s) > 40 else s}")
    return ", ".join(parts)
