"""
Core data models for the bioinformatics agent.

Single source of truth for:
  - Controlled vocabulary (ReadType, EndType, AssayType, FileType, Database)
  - InstallMethod      (conda | jar | pip | docker_pull | source | manual)
  - ReferenceDatabase  (large external databases beyond the genome FASTA)
  - RuntimeEnvironment (conda | jar-in-conda | docker | native)
  - RuntimeConfig      (config files the tool needs at runtime)
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
AssayType = Literal[
    # Short-read
    "exome", "wgs", "rnaseq", "chipseq", "atacseq", "hic", "amplicon", "wgbs",
    # Long-read DNA
    "ont_wgs", "pacbio_hifi",
    # Long-read RNA
    "direct_rna", "isoseq",
    # Long-read epigenomics
    "fiberseq",
]
Platform  = Literal["illumina", "ont", "pacbio_hifi", "pacbio_isoseq", "pacbio_fiberseq"]
FileType  = Literal[
    # Sequencing / alignment
    "fastq", "fasta", "bam", "sam", "bai", "tbi",
    # Variants
    "vcf", "bcf",
    # Genomic intervals / coverage
    "bed", "bigwig",
    # Feature annotations
    "gtf", "gff", "counts_matrix",
    # Long-read raw formats
    "pod5", "fast5",
    # Pedigree
    "ped",
    # PLINK binary / LD output
    "bim", "fam", "ld", "frq", "prune",
    # Reports / structured data
    "html", "json", "yaml", "properties",
    # Generic tabular / text / logs
    "tsv", "csv", "txt", "log", "gz",
]
Database  = Literal["EBI_SRA", "NCBI_SRA", "ENCODE", "GEO", "local"]

# Map platform → read_type (long_read for ONT and PacBio)
PLATFORM_READ_TYPE: dict[str, ReadType] = {
    "illumina":        "short_read",
    "ont":             "long_read",
    "pacbio_hifi":     "long_read",
    "pacbio_isoseq":   "long_read",
    "pacbio_fiberseq": "long_read",
}

# Map platform → directory family used under long_read/
PLATFORM_FAMILY: dict[str, str] = {
    "ont":             "ont",
    "pacbio_hifi":     "pacbio",
    "pacbio_isoseq":   "pacbio",
    "pacbio_fiberseq": "pacbio",
}

KNOWN_PIPELINES: frozenset[str] = frozenset({
    "bwa_samtools", "freebayes", "star", "gatk", "fastqc",
    "featurecounts", "bcftools", "trimmomatic", "fastp", "minimap2",
})


# ---------------------------------------------------------------------------
# Install method
# ---------------------------------------------------------------------------

class InstallMethod(BaseModel):
    """
    How a single package is installed.

    Default is conda.  For Java tools (e.g. Exomiser, Picard, GATK):
      - type = "jar"
      - openjdk is installed via conda in the same env
      - the JAR is downloaded to {env}/share/{tool}/{tool}.jar
      - a wrapper script is written to {env}/bin/{tool}
    This means conda-pack captures the full JVM → Docker image is self-contained.
    """
    model_config = ConfigDict(extra="allow")

    type: Literal["conda", "jar", "pip", "docker_pull", "source", "manual"] = "conda"
    # conda
    conda_spec: Optional[str] = None   # e.g. "samtools=1.21"
    channel:    Optional[str] = None   # e.g. "bioconda"
    # jar — Java tool, JAR downloaded from GitHub releases or similar
    jar_url:    Optional[str] = None   # download URL for the JAR
    jar_path:   Optional[str] = None   # resolved absolute path after download
    # pip
    pip_spec:   Optional[str] = None   # e.g. "multiqc==1.21"
    # docker_pull — tool only available as a pulled image (no conda/JAR path)
    docker_image: Optional[str] = None


# ---------------------------------------------------------------------------
# Reference databases
# ---------------------------------------------------------------------------

class ReferenceDatabase(BaseModel):
    """
    A large external database required by a tool beyond the genome FASTA
    (e.g. Exomiser data bundle, VEP cache, Kraken2 database, STAR genome index).

    These are tracked separately from the genome reference because they:
      - Can be very large (tens to hundreds of GB)
      - Are versioned independently of the tool
      - Must often match the tool version exactly (coupled_to_version)
      - Are mounted at runtime rather than baked into the Docker image
    """
    model_config = ConfigDict(extra="allow")

    name:               str            # e.g. "exomiser_hg38_2402", "vep_cache_111_hg38"
    version:            str            # data bundle version, e.g. "2402", "111"
    size_gb:            Optional[float] = None
    source_url:         str            # where to download it
    local_path:         Optional[str] = None   # absolute path on this machine once downloaded
    available:          bool = False
    description:        Optional[str] = None
    coupled_to_version: Optional[str] = None   # tool version this data bundle is designed for


# ---------------------------------------------------------------------------
# Runtime environment
# ---------------------------------------------------------------------------

class RuntimeEnvironment(BaseModel):
    """
    How the pipeline is executed at runtime.

    type="conda"   — standard: activate env, call binary directly (default).
    type="jar"     — Java tool: conda env contains openjdk (from conda-forge);
                     tool is invoked as `java <java_flags> -jar <jar_path>`.
                     conda-pack bundles the JVM so the Docker image is self-contained.
    type="docker"  — tool is only available via a pre-pulled Docker image;
                     no conda env (use docker_image field).
    type="native"  — tool is a system binary, no special env needed.

    resource hints (min_ram_gb, min_cpu) are informational and written into
    the pipeline spec so HPC job schedulers can pick them up.
    """
    model_config = ConfigDict(extra="allow")

    type: Literal["conda", "jar", "docker", "native"] = "conda"
    # jar: JVM lives in the conda env; these fields describe invocation
    java_flags:     list[str] = []        # e.g. ["-Xmx12g", "-Djava.awt.headless=true"]
    jar_path:       Optional[str] = None  # absolute path to the JAR (in {env}/share/{tool}/)
    wrapper_script: Optional[str] = None  # {env}/bin/{tool} wrapper created during install
    # docker: tool only available as a pulled image
    docker_image:   Optional[str] = None
    # resource hints
    min_ram_gb:     Optional[float] = None
    min_cpu:        Optional[int] = None


# ---------------------------------------------------------------------------
# Runtime configuration files
# ---------------------------------------------------------------------------

class RuntimeConfig(BaseModel):
    """
    A configuration file the tool needs at runtime.

    Examples:
      - Exomiser analysis YAML  (per-run: HPO terms, VCF path, filters)
      - Exomiser application.properties  (per-installation: data dir, memory)
      - Bismark genome preparation config
      - GATK scatter-gather interval list

    Stored at the PipelineSpec level (global) and/or PipelineStep level (per-step).
    """
    model_config = ConfigDict(extra="allow")

    name:    str   # logical name, e.g. "analysis_yaml", "application_properties"
    format:  Literal["yaml", "properties", "ini", "json", "xml", "tsv", "txt"]
    path:    str   # absolute path to the written config file
    content: Optional[str] = None   # inline content snapshot (for small configs)


# ---------------------------------------------------------------------------
# Provenance sub-models — input types
# ---------------------------------------------------------------------------

class ReadInput(BaseModel):
    """FASTQ read inputs consumed by an alignment-type pipeline."""
    read_type:  ReadType
    end_type:   EndType
    assay_type: AssayType
    platform:   Platform = "illumina"
    subset:     str          # e.g. "10K", "500", "1M", "full"
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


class VcfInput(BaseModel):
    """VCF (+ optional tabix index) consumed by annotation/prioritization pipelines."""
    vcf:               str             # path relative to the provenance file
    tbi:               Optional[str] = None
    genome_build:      str
    upstream_pipeline: Optional[str] = None   # which pipeline produced it
    sample_ids:        list[str] = []


class PhenotypeInput(BaseModel):
    """
    Ontology-based phenotype terms used by prioritization tools (Exomiser, Phenomizer, …).
    Terms are the primary clinical input — they drive gene-phenotype scoring.
    """
    ontology: Literal["HPO", "GO", "MP", "DOID"] = "HPO"
    terms:    list[str]    # e.g. ["HP:0001250", "HP:0001263"]
    source:   Optional[str] = None   # "manual" | "phenopacket" | "clinical_record"


class PedigreeInput(BaseModel):
    """PED file for family/trio analysis."""
    ped:     str            # path relative to the provenance file
    proband: Optional[str] = None   # sample ID of the affected individual


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

    Relative paths (reference, reads, bam_input, vcf_input, pedigree, pipeline_spec)
    are always expressed relative to the directory that will contain this provenance
    file.  Use Provenance.resolve_paths(provenance_dir) to get absolute Path objects.

    At least one input type must be present:
      reads, bam_input, vcf_input, phenotype, or pedigree.

    genome is Optional because some tools (variant prioritizers, phenotype scorers)
    do not consume a reference FASTA.
    """
    pipeline:           str
    pipeline_spec:      str                  # relative path to config/pipelines/*.yaml
    conda_env:          str                  # env directory basename
    created_at:         str                  # ISO date YYYY-MM-DD
    tool_versions:      dict[str, str]
    genome:             Optional[GenomeRef] = None   # None for reference-free tools
    reads:              Optional[list[ReadInput]] = None
    bam_input:          Optional[BamInput] = None
    vcf_input:          Optional[VcfInput] = None
    phenotype:          Optional[PhenotypeInput] = None
    pedigree:           Optional[PedigreeInput] = None
    upstream_pipelines: list[str] = []
    parameters:         Optional[dict[str, Any]] = None
    outputs:            list[OutputFile]

    @model_validator(mode="after")
    def _require_input(self) -> "Provenance":
        has_input = any([
            self.reads, self.bam_input, self.vcf_input,
            self.phenotype, self.pedigree,
        ])
        if not has_input:
            raise ValueError(
                "Provenance must specify at least one input: "
                "reads, bam_input, vcf_input, phenotype, or pedigree"
            )
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
        }
        if self.genome:
            paths["reference"]     = (base / self.genome.reference).resolve()
            paths["reference_fai"] = (base / self.genome.reference_fai).resolve()
        if self.reads:
            for i, r in enumerate(self.reads):
                paths[f"reads[{i}].r1"] = (base / r.r1).resolve()
                if r.r2:
                    paths[f"reads[{i}].r2"] = (base / r.r2).resolve()
        if self.bam_input:
            paths["bam"] = (base / self.bam_input.bam).resolve()
            paths["bai"] = (base / self.bam_input.bai).resolve()
        if self.vcf_input:
            paths["vcf"] = (base / self.vcf_input.vcf).resolve()
            if self.vcf_input.tbi:
                paths["tbi"] = (base / self.vcf_input.tbi).resolve()
        if self.pedigree:
            paths["ped"] = (base / self.pedigree.ped).resolve()
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
    platform:    Platform = "illumina"
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
    """
    One package installed as part of a pipeline.

    install_method describes HOW it was installed (conda, jar download, pip, …).
    If absent, conda is assumed (backward compatible with existing specs).
    """
    model_config = ConfigDict(extra="allow")

    name:              str
    requested_version: str = "latest"
    resolved_version:  Optional[str] = None
    install_method:    Optional[InstallMethod] = None   # None → conda (default)
    # kept for backward compatibility; also populated from install_method for conda packages
    conda_spec:        Optional[str] = None
    channel:           Optional[str] = None
    description:       Optional[str] = None
    homepage:          Optional[str] = None
    verify_command:    Optional[str] = None
    verify_output:     Optional[str] = None
    platform_note:     Optional[str] = None
    input_types:       list[str] = []
    output_types:      list[str] = []


class TestDataRef(BaseModel):
    """Reference to the test dataset used during pipeline validation."""
    model_config = ConfigDict(extra="allow")

    genome_build:       str
    chromosome_subset:  Optional[str] = None
    read_type:          Optional[ReadType] = None
    end_type:           Optional[EndType] = None
    assay_type:         Optional[AssayType] = None
    platform:           Optional[Platform] = None
    sample:             Optional[str] = None
    accession:          Optional[str] = None
    subset:             Optional[str] = None
    num_reads:          Optional[int] = None
    r1:                 Optional[str] = None
    r2:                 Optional[str] = None
    reference_fasta:    Optional[str] = None
    core_data_dir:      Optional[str] = None
    upstream_pipelines: list[str] = []


class PipelineStep(BaseModel):
    """One execution step within a pipeline run."""
    model_config = ConfigDict(extra="allow")

    step:        int
    tool:        str
    subcommand:  Optional[str] = None
    purpose:     Optional[str] = None
    command:     str
    status:      Literal["validated", "failed", "skipped"] = "validated"
    returncode:  Optional[int] = None

    @model_validator(mode="after")
    def _derive_status_from_returncode(self) -> "PipelineStep":
        if self.returncode is not None:
            self.status = "validated" if self.returncode == 0 else "failed"
        return self

    inputs:           list[str] = []         # filenames consumed (full lineage)
    outputs:          list[str] = []         # filenames produced
    config_files:     list[RuntimeConfig] = []  # config files written for this step
    runtime_seconds:  Optional[float] = None
    output_size_bytes: Optional[int] = None
    validation:       Optional[Any] = None


class DockerBuild(BaseModel):
    """
    Docker image build result.

    volume_mounts lists directories that must be bind-mounted at runtime
    (e.g. the Exomiser data directory).  These are NOT baked into the image.
    runtime_data_env is the environment variable the tool reads to locate
    its data directory (e.g. EXOMISER_DATA_DIR), so downstream users know
    what to set when running the container.
    """
    model_config = ConfigDict(extra="allow")

    build_attempted:  bool = False
    build_success:    bool = False
    image_tag:        Optional[str] = None
    registry:         str = "local"
    reason:           Optional[str] = None
    volume_mounts:    list[str] = []         # e.g. ["/data/exomiser"]
    runtime_data_env: Optional[str] = None   # e.g. "EXOMISER_DATA_DIR"


class PipelineSpec(BaseModel):
    """
    Complete record of an installed, validated pipeline.
    Written to config/pipelines/{name}_{version}.yaml after a successful install.

    runtime_environment: describes how the primary tool is invoked.
      - type="conda"  → standard (default for most tools)
      - type="jar"    → Java tool; openjdk is in the conda env, JAR at jar_path.
                        conda-pack bundles the JVM → Docker image is self-contained.

    reference_databases: large external databases beyond the genome FASTA.
      These are documented here but NOT baked into the Docker image.
      Mount them at the paths listed in docker.volume_mounts.

    runtime_configs: global config files written during installation
      (e.g. application.properties for Exomiser).
      Per-step configs live in PipelineStep.config_files.
    """
    model_config = ConfigDict(extra="allow")

    pipeline_name:       str
    description:         str
    conda_env:           str
    python_version:      Optional[str] = None
    created_at:          str
    status:              PipelineStatus
    packages:            list[PackageRecord]
    runtime_environment: Optional[RuntimeEnvironment] = None   # None → conda (default)
    reference_databases: list[ReferenceDatabase] = []
    runtime_configs:     list[RuntimeConfig] = []
    test_data:           Optional[TestDataRef] = None
    pipeline_steps:      list[PipelineStep] = []
    docker:              Optional[DockerBuild] = None
    notes:               list[str] = []
    final_summary:       Optional[str] = None

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
