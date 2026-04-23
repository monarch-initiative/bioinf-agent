"""
Core data models for the bioinformatics agent.

Single source of truth for:
  - Controlled vocabulary (ReadType, EndType, AssayType, FileType, Database)
  - Provenance schema  (one pipeline run on one sample)
  - SampleMeta schema  (source metadata for a sequencing run)

Used by:
  - scripts/gen_provenance.py   (setup script path)
  - scripts/gen_manifest.py     (manifest rebuilder)
  - agent/skills/install_pipeline.py  (write_pipeline_provenance sub-tool)
  - agent/tools.py              (list_available_resources reader)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, model_validator

# ---------------------------------------------------------------------------
# Controlled vocabulary
# ---------------------------------------------------------------------------

ReadType  = Literal["short_read", "long_read"]
EndType   = Literal["paired_end", "single_end", "mate_pair"]
AssayType = Literal["exome", "wgs", "rnaseq", "chipseq", "atacseq", "hic", "amplicon"]
FileType  = Literal["fastq", "bam", "sam", "bai", "vcf", "bcf", "bed", "bigwig",
                    "pod5", "fast5", "log", "yaml",
                    # PLINK binary / LD output formats
                    "bim", "fam", "ld", "frq", "prune",
                    # Generic tabular / text
                    "tsv", "csv", "txt", "gz"]
Database  = Literal["EBI_SRA", "NCBI_SRA", "ENCODE", "GEO", "local"]

KNOWN_PIPELINES: frozenset[str] = frozenset({
    "bwa_samtools", "freebayes", "star", "gatk", "fastqc",
    "featurecounts", "bcftools", "trimmomatic", "fastp", "minimap2",
})

# ---------------------------------------------------------------------------
# Provenance sub-models
# ---------------------------------------------------------------------------


class ReadInput(BaseModel):
    """FASTQ read inputs consumed by an alignment-type pipeline."""
    read_type:  ReadType
    end_type:   EndType
    assay_type: AssayType
    subset:     str          # e.g. "100K", "1M", "full"
    num_reads:  int
    r1:         str          # path relative to the provenance file
    r2:         Optional[str] = None
    sample:     str
    accession:  str
    database:   Database


class GenomeRef(BaseModel):
    """Reference genome used in a pipeline run."""
    genome_build:      str
    chromosome_subset: str
    reference:         str   # path relative to the provenance file
    reference_fai:     str   # path relative to the provenance file


class BamInput(BaseModel):
    """Sorted BAM + index consumed by variant-calling-type pipelines."""
    bam: str   # path relative to the provenance file
    bai: str


class OutputFile(BaseModel):
    """One output file produced by the pipeline."""
    file:    str       # filename only — no directory component
    type:    FileType
    indexed: bool = False


# ---------------------------------------------------------------------------
# Provenance — one pipeline run on one sample
# ---------------------------------------------------------------------------


class Provenance(BaseModel):
    """
    Complete, validated provenance for a single pipeline run.

    Relative paths (reference, reads, bam_input, pipeline_spec) are always
    expressed relative to the directory that will contain this provenance file.
    Use Provenance.resolve_paths(provenance_dir) to get absolute Path objects.
    """
    pipeline:           str
    pipeline_spec:      str                  # relative path to config/pipelines/*.yaml
    conda_env:          str                  # env directory basename
    created_at:         str                  # ISO date YYYY-MM-DD
    tool_versions:      dict[str, str]
    genome:             GenomeRef
    reads:              Optional[list[ReadInput]] = None
    bam_input:          Optional[BamInput] = None
    upstream_pipelines: list[str] = []
    parameters:         Optional[dict[str, Any]] = None
    outputs:            list[OutputFile]

    @model_validator(mode="after")
    def _require_reads_or_bam(self) -> "Provenance":
        if self.reads is None and self.bam_input is None:
            raise ValueError("Provenance must specify either 'reads' or 'bam_input'")
        return self

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_yaml(self) -> str:
        data = self.model_dump(exclude_none=True)
        return yaml.dump(data, default_flow_style=False, sort_keys=False)

    def write(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.to_yaml())
        return out

    @classmethod
    def from_yaml(cls, path: str | Path) -> "Provenance":
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls(**data)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def resolve_paths(self, provenance_dir: Path) -> dict[str, Path]:
        """Return absolute Paths for all file references in this provenance."""
        base = Path(provenance_dir)
        paths: dict[str, Path] = {
            "pipeline_spec": (base / self.pipeline_spec).resolve(),
            "reference":     (base / self.genome.reference).resolve(),
            "reference_fai": (base / self.genome.reference_fai).resolve(),
        }
        if self.reads:
            for i, r in enumerate(self.reads):
                paths[f"reads[{i}].r1"] = (base / r.r1).resolve()
                if r.r2:
                    paths[f"reads[{i}].r2"] = (base / r.r2).resolve()
        if self.bam_input:
            paths["bam"] = (base / self.bam_input.bam).resolve()
            paths["bai"] = (base / self.bam_input.bai).resolve()
        return paths


# ---------------------------------------------------------------------------
# Sample metadata — source metadata for a sequencing run
# ---------------------------------------------------------------------------


class SubsetInfo(BaseModel):
    """One subset (downsampled) version of a sequencing run."""
    r1:        str
    r2:        Optional[str] = None
    num_reads: int
    available: bool = False


class SampleMeta(BaseModel):
    """
    Source metadata for one sequencing run.
    Written alongside FASTQ subsets so gen_manifest.py can rebuild the manifest.
    """
    sample:      str
    accession:   str
    read_type:   ReadType
    end_type:    EndType
    assay_type:  AssayType
    sex:         Optional[str] = None
    database:    Database
    protocol:    Optional[str] = None
    capture:     Optional[str] = None
    read_length: Optional[int] = None
    source_urls: Optional[dict[str, str]] = None
    subsets:     dict[str, SubsetInfo]

    def to_yaml(self) -> str:
        data = self.model_dump(exclude_none=True)
        return yaml.dump(data, default_flow_style=False, sort_keys=False)

    def write(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.to_yaml())
        return out

    @classmethod
    def from_yaml(cls, path: str | Path) -> "SampleMeta":
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls(**data)


# ---------------------------------------------------------------------------
# Pipeline spec — one installed + validated pipeline
# ---------------------------------------------------------------------------

PipelineStatus = Literal["fully_validated", "complete", "in_progress", "failed", "timeout"]


class PackageRecord(BaseModel):
    """One package installed as part of a pipeline."""
    model_config = ConfigDict(extra="allow")

    name: str
    requested_version: str = "latest"
    resolved_version: Optional[str] = None
    conda_spec: Optional[str] = None
    channel: Optional[str] = None
    description: Optional[str] = None
    homepage: Optional[str] = None
    verify_command: Optional[str] = None
    verify_output: Optional[str] = None
    platform_note: Optional[str] = None
    input_types: list[str] = []
    output_types: list[str] = []


class TestDataRef(BaseModel):
    """Reference to the test dataset used during pipeline validation."""
    model_config = ConfigDict(extra="allow")

    genome_build: str
    chromosome_subset: Optional[str] = None
    read_type: Optional[ReadType] = None
    end_type: Optional[EndType] = None
    assay_type: Optional[AssayType] = None
    sample: Optional[str] = None
    accession: Optional[str] = None
    subset: Optional[str] = None
    num_reads: Optional[int] = None
    r1: Optional[str] = None
    r2: Optional[str] = None
    reference_fasta: Optional[str] = None
    core_data_dir: Optional[str] = None
    upstream_pipelines: list[str] = []


class PipelineStep(BaseModel):
    """One execution step within a pipeline run."""
    model_config = ConfigDict(extra="allow")

    step: int
    tool: str
    subcommand: Optional[str] = None
    purpose: Optional[str] = None
    command: str
    status: Literal["validated", "failed", "skipped"] = "validated"
    returncode: Optional[int] = None

    @model_validator(mode="after")
    def _derive_status_from_returncode(self) -> "PipelineStep":
        if self.returncode is not None:
            self.status = "validated" if self.returncode == 0 else "failed"
        return self
    runtime_seconds: Optional[float] = None
    output_size_bytes: Optional[int] = None
    validation: Optional[Any] = None


class DockerBuild(BaseModel):
    """Docker image build result."""
    build_attempted: bool = False
    build_success: bool = False
    image_tag: Optional[str] = None
    registry: str = "local"
    reason: Optional[str] = None


class PipelineSpec(BaseModel):
    """
    Complete record of an installed, validated pipeline.
    Written to config/pipelines/{name}_{version}.yaml after a successful install.
    """
    model_config = ConfigDict(extra="allow")

    pipeline_name: str
    description: str
    conda_env: str
    python_version: Optional[str] = None
    created_at: str
    status: PipelineStatus
    packages: list[PackageRecord]
    test_data: Optional[TestDataRef] = None
    pipeline_steps: list[PipelineStep] = []
    docker: Optional[DockerBuild] = None
    notes: list[str] = []
    final_summary: Optional[str] = None

    def to_yaml(self) -> str:
        data = self.model_dump(exclude_none=True)
        return yaml.dump(data, default_flow_style=False, sort_keys=False)

    def write(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.to_yaml())
        return out

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PipelineSpec":
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls(**data)
