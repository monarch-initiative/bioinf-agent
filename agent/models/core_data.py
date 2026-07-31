"""
Core data models for the bioinformatics agent.

SCOPE NOTE (tier 7, read before adding to this file). `PipelineSpec` — the
pre-respine combined model — was DELETED here: its producers (finalize_pipeline /
save_pipeline_spec) were retired in the re-spine, and the last reader
(resources.list_pipelines) was repointed at the artifacts that actually exist
(the EnvCache + `*.workflow.yaml`). `WorkflowSpec` is now the only spec model
with a real producer.

Consequence to be honest about: five models were reachable ONLY through
PipelineSpec and are now unreferenced by any model — `Accelerator`,
`InstallStep`, `PackageRecord`, `RuntimeEnvironment`, `ServiceDependency` (and
`InstallMethod` under PackageRecord). They are NOT dead weight in the ordinary
sense: they are the declared SHAPE of records the runtime really does produce and
gate on (I12 reads accelerator dicts, I10 reads service_dependencies, freeze
reads install_method dicts) — the runtime just passes plain dicts and never
validates against these classes. So they are schema-as-documentation whose drift
nothing catches, which is exactly how InstallMethod's Literal came to name two
tiers with no producer while omitting one that had. Deleting them, or wiring them
in as real validators, is a deliberate call that needs its own verification pass —
NOT a quiet cleanup. Flagged, not swept.

Single source of truth for:
  - Controlled vocabulary (ReadType, EndType, AssayType, FileType, Database)
  - InstallMethod      (conda | jar | pip | r_install | source | binary | perl |
                        cargo | go | synthesized)
  - ReferenceDatabase  (large external databases beyond the genome FASTA)
  - RuntimeEnvironment (conda | jar-in-conda | r | docker | native; GPU fields)
  - RuntimeConfig      (config files the tool needs at runtime)
  - ServiceDependency  (companion processes: web server, database, Spark)
  - Provenance schema  (one pipeline run on one sample; AssemblyInput for scaffolding)
  - SampleMeta schema  (source metadata for a sequencing run)
  - PhenopacketMeta    (GA4GH phenopacket clinical/genomic record)

Used by:
  - scripts/gen_provenance.py   (setup script path)
  - scripts/gen_manifest.py     (manifest rebuilder)
  - agent/skills/spec_writer.py (write_workflow_spec + write_provenance)
  - agent/skills/resources.py   (list_available_resources reader)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional, Union

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

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
    # Array / GWAS
    "gwas_array", "gwas_wgs", "qtl_array", "expression_array", "snp_array",
]
Platform  = Literal["illumina", "ont", "pacbio_hifi", "pacbio_isoseq", "pacbio_fiberseq"]
FileType  = Literal[
    # Sequencing / alignment
    "fastq", "fasta", "bam", "sam", "bai", "tbi",
    # Variants
    "vcf", "bcf",
    # Genomic intervals / coverage
    "bed", "bedgraph", "bigwig",
    # Methylation / epigenetic
    "methylation_report", "cpg_report", "cov", "bismark_cov",
    # Feature annotations
    "gtf", "gff", "counts_matrix",
    # Assembly graphs
    "gfa", "gaf",
    # Long-read raw formats
    "pod5", "fast5",
    # Pedigree
    "ped",
    # PLINK binary / LD output
    "bim", "fam", "ld", "frq", "prune",
    # Visualization / plots
    "pdf", "png", "svg", "eps", "tiff", "jpeg",
    # Reports / structured data
    "html", "json", "jsonl", "ndjson", "yaml", "properties",
    # Generic tabular / text / logs
    "tsv", "csv", "txt", "log", "gz",
    # Databases / structured persistence
    "sqlite", "h5", "hdf5", "parquet",
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
    The container-native freeze installs the JVM (openjdk) INTO the ship image, so
    the shipped image is self-contained.
    """
    model_config = ConfigDict(extra="allow")

    # Keep this in step with the tier dispatchers that actually RUN — env_freeze
    # `_map_install_spec` / `_replay_assurance` and env_recipe_render. Tier 7 found
    # this list drifted in BOTH directions at once: it named `docker_pull`/`manual`
    # (zero producers, ever) while OMITTING `synthesized`, which env_tools' synth_build
    # writes and two dispatchers consume. It never exploded only because InstallMethod
    # is not pydantic-validated on the live path — i.e. the copy that CLAIMED authority
    # was stale and the copies that RAN were right. Adding a tier here is not optional
    # bookkeeping; it is what stops that trap from arming again.
    type: Literal["conda", "jar", "pip", "r_install", "source",
                  "binary", "perl", "cargo", "go", "synthesized"] = "conda"
    # conda
    conda_spec: Optional[str] = None   # e.g. "samtools=1.21"
    channel:    Optional[str] = None   # e.g. "bioconda"
    # jar — Java tool, JAR downloaded from GitHub releases or similar
    jar_url:    Optional[str] = None   # download URL for the JAR
    jar_path:   Optional[str] = None   # resolved absolute path after download
    # pip
    pip_spec:   Optional[str] = None   # e.g. "multiqc==1.21"
    # source — git repo vendored as the tool (install_git_repo)
    source:     Optional[str] = None   # repo URL (also reused as a generic source field)
    commit_sha: Optional[str] = None   # resolved HEAD at clone time — immutable content anchor (I11)
    ref:        Optional[str] = None   # branch / tag / commit requested
    local_path: Optional[str] = None   # absolute path to the clone / binary on disk
    # binary — precompiled release binary (mosdepth/dorado/sylph/cellranger pattern)
    binary_url:   Optional[str] = None  # download URL of the release asset
    sha256:       Optional[str] = None  # sha256 of the EXTRACTED binary at local_path — I14 re-hashes this
    asset_sha256: Optional[str] = None  # sha256 of the downloaded asset (the .tar.gz/.zip); == sha256 for
                                        # single-binary downloads. Provenance anchor checked vs the publisher
                                        # checksum at download. I14 anchors the runnable binary, not the archive.


# ---------------------------------------------------------------------------
# Hardware acceleration
# ---------------------------------------------------------------------------

class Accelerator(BaseModel):
    """Hardware-acceleration contract for the env.

    Honest because the runtime distinction is enforced by I12: a spec may not
    CLAIM a kernel ran on a real device (runtime="runtime_verified") without a
    recorded runtime_probe. Default is build_only — the toolchain/image is
    correct, but execution was not verified on hardware (the case when building
    a CUDA image on a machine with no GPU). True runtime verification needs the
    GPU runner (Phase 3).

    MPS (Apple Metal) is dev-only: it does NOT survive containerization to the
    shipped linux/amd64 image, so I12 forces dev_only=true for it — a local MPS
    run can never become a property of the deliverable.
    """
    model_config = ConfigDict(extra="allow")

    type:                Literal["none", "cuda", "rocm", "mps"] = "none"
    toolkit_version:     Optional[str] = None   # CUDA/ROCm toolkit baked into the image, e.g. "12.1"
    min_driver_version:  Optional[str] = None   # host driver floor required for the image to run
    compute_capability:  Optional[str] = None   # e.g. "8.0" (A100), "9.0" (H100)
    runtime:             Literal["build_only", "runtime_verified"] = "build_only"
    runtime_probe:       Optional[str] = None   # captured nvidia-smi/nvcc/device output — the proof
    dev_only:            bool = False            # mps: local-dev accel that does not containerize


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
    source_url:         Optional[str] = None   # where it was downloaded from; None for a
                                               # locally-staged reference with no download
                                               # origin (download_reference_database always
                                               # sets it — a fabricated file:// URL is worse
                                               # than an honest absent one). I5 pins content
                                               # by sha256, not by URL, so provenance survives.
    local_path:         Optional[str] = None   # absolute path on this machine once downloaded
    available:          bool = False
    description:        Optional[str] = None
    coupled_to_version: Optional[str] = None   # tool version this data bundle is designed for
    sha256:             Optional[str] = None   # content hash of the downloaded artifact —
                                               # the reproducibility anchor: a bundle named
                                               # "vep_cache_111_hg38" is not self-verifying
                                               # (the URL can serve different bytes over time),
                                               # so downstream re-runs pin the DB by this hash.
    size_bytes:         Optional[int] = None   # exact byte size of the downloaded artifact


# ---------------------------------------------------------------------------
# Runtime environment
# ---------------------------------------------------------------------------

class RuntimeEnvironment(BaseModel):
    """
    How the pipeline is executed at runtime.

    type="conda"   — standard: activate env, call binary directly (default).
    type="jar"     — Java tool: conda env contains openjdk (from conda-forge);
                     tool is invoked as `java <java_flags> -jar <jar_path>`.
                     The container-native freeze bakes the JVM into the ship image.
    type="docker"  — tool is only available via a pre-pulled Docker image;
                     no conda env (use docker_image field).
    type="native"  — tool is a system binary, no special env needed.

    resource hints (min_ram_gb, min_cpu) are informational and written into
    the pipeline spec so HPC job schedulers can pick them up.
    """
    model_config = ConfigDict(extra="allow")

    type: Literal["conda", "jar", "r", "docker", "native"] = "conda"
    # jar: JVM lives in the conda env; these fields describe invocation
    java_flags:     list[str] = []        # e.g. ["-Xmx12g", "-Djava.awt.headless=true"]
    jar_path:       Optional[str] = None  # absolute path to the JAR (in {env}/share/{tool}/)
    wrapper_script: Optional[str] = None  # {env}/bin/{tool} wrapper created during install
    # docker: tool only available as a pulled image
    docker_image:   Optional[str] = None
    # GPU — when True, DockerBuilder uses a CUDA base image
    gpu_required:   bool = False
    cuda_version:   Optional[str] = None  # e.g. "12.1" — selects CUDA base image
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
    format:  Literal["yaml", "properties", "java_properties", "ini", "json", "xml", "tsv", "txt"]
    path:    str   # absolute path to the written config file
    content: Optional[str] = None   # inline content snapshot (for small configs)


# ---------------------------------------------------------------------------
# Authored artifacts — captures the "agent went outside MCP to write a file"
# blind spot. Driver scripts, synthetic test inputs, hand-staged transformations,
# generated data, sed/awk-massaged configs all live here. Recording them in the
# spec makes the entire pipeline reproducible: every artifact consumed by a
# step traces to a real source.
#
# Honesty invariants (I8 + I9):
#   - I8 universe includes `authored_artifacts[*].path` — orphan inputs that
#     reference these files now compose correctly.
#   - I9 re-computes sha256 at finalize and refuses to write a spec whose
#     on-disk artifact has drifted from the recorded content.
# ---------------------------------------------------------------------------

class AuthoredArtifact(BaseModel):
    """
    A file the agent wrote into existence during the install — the spec's
    record of "I generated this." Two modes:

      content mode    — agent supplies the full text content; the runtime
                        writes the file AND records the verbatim content in
                        the spec so a reviewer can audit what the agent
                        authored.
      generated_by    — file is binary or large (BAM, FASTA, indexed db) and
                        was produced by a shell command the agent ran. The
                        primitive records the genesis command instead of the
                        bytes.

    In both cases the runtime computes sha256(file_on_disk) at stage-time;
    finalize re-computes and refuses to write the spec if it has drifted.
    """
    model_config = ConfigDict(extra="allow")

    path:                  str         # absolute path to the on-disk artifact
    role:                  str         # free-form: "driver_script", "synthetic_test_input", "config", "staged_input", ...
    description:           str         # WHY this file exists (audit trail)
    sha256:                str         # hex digest of the bytes on disk at stage-time
    size_bytes:            int
    created_at:            str
    language:              Optional[str] = None   # hint: "r", "python", "bash", "tsv", "json", ...
    content_excerpt:       Optional[str] = None   # first N bytes for human review (text mode)
    content_full_in_spec:  bool = False           # True if content fit fully in spec
    generated_by:          Optional[str] = None   # shell command that produced the file (binary mode)


# ---------------------------------------------------------------------------
# Service dependencies — companion processes required during a pipeline run
# ---------------------------------------------------------------------------

class HealthCheckProbe(BaseModel):
    """
    A single observed health-check probe for a ServiceDependency.

    Runtime-captured by `start_service` (initial readiness probe) and
    `verify_service_dependency` (manual re-probes). The honesty contract (I10)
    requires every declared service to have ≥1 entry here with healthy=true —
    a declaration without a successful probe is an unverified claim.
    """
    model_config = ConfigDict(extra="allow")

    timestamp:      str
    command:        str           # the exact health-check command run
    returncode:     int
    healthy:        bool
    output_excerpt: Optional[str] = None   # last ~500 chars of stdout+stderr


class ServiceDependency(BaseModel):
    """
    A background process that must be running while the pipeline executes.

    Examples:
      - OpenCRAVAT web server (type="web_server")
      - Cromwell + MySQL backend (type="database")
      - Hail / Spark driver (type="spark")
      - Redis cache (type="cache")

    start_command is run inside the conda env before the pipeline; stop_command
    is run after.  health_check_command is polled until healthy or timeout.
    The PID file lives at /tmp/bioinf_services/{name}.pid (managed by EnvManager).

    Honesty-related fields are populated by the runtime, NOT the agent:
      - health_check_log: every observed probe; I10 requires ≥1 healthy
      - pid / started_at / stopped_at: lifecycle observation
      - status: derived from the lifecycle (declared → running → stopped/failed)
    """
    model_config = ConfigDict(extra="allow")

    type:                         Literal["web_server", "database", "spark", "cache", "custom"]
    name:                         str             # e.g. "opencravat", "mongodb", "mysql"
    version:                      Optional[str] = None
    start_command:                str
    stop_command:                 str
    health_check_command:         str
    health_check_timeout_seconds: int = 30
    port:                         Optional[int] = None
    env_vars:                     dict[str, str] = {}
    data_dir:                     Optional[str] = None
    # Runtime-captured (do NOT hand-supply via patch_pipeline)
    pid:                          Optional[int] = None
    started_at:                   Optional[str] = None
    stopped_at:                   Optional[str] = None
    status:                       Literal["declared", "running", "stopped", "failed"] = "declared"
    health_check_log:             list[HealthCheckProbe] = []


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


class AssemblyInput(BaseModel):
    """
    A draft or primary assembly FASTA consumed by scaffolding or polishing pipelines.

    Distinct from GenomeRef: GenomeRef is the well-known reference genome (e.g. hg38 chr22).
    AssemblyInput is a draft contig FASTA that was produced by a prior pipeline step (e.g.
    hifiasm → contigs → 3D-DNA + Hi-C → scaffolded chromosomes).

    Use this when the tool's 'reference' is a draft assembly, not the canonical genome.
    Set PipelineSpec.reference_free=False and include assembly_input in Provenance.
    """
    assembly:          str             # path relative to the provenance file
    upstream_pipeline: Optional[str] = None  # pipeline that produced this assembly


class QuantitativeTraitInput(BaseModel):
    """
    Quantitative phenotype measurements for GWAS/QTL/regression tools.

    Distinct from PhenotypeInput (ontology-coded disease terms): traits here are
    continuous measurements stored in a tabular file, not HPO/GO term IDs.
    Examples: ear height, BMI, grain yield, blood pressure.
    """
    traits:           list[str]          # trait column names in the phenotype file
    file:             str                # path relative to the provenance file
    n_samples:        Optional[int] = None
    measurement_type: Literal["continuous", "binary", "ordinal"] = "continuous"


class GenotypeArrayInput(BaseModel):
    """
    Population-level genotype matrix consumed by GWAS/QTL tools.

    Distinct from VcfInput (per-sample variant calls): this is a population-wide
    genotype table (HapMap, PLINK BED triplet, dosage matrix, BGEN).
    """
    file:              str               # primary genotype file (hapmap txt, .bed, .bgen, …)
    format:            Literal["hapmap", "plink_bed", "vcf", "dosage", "bgen"]
    bim:               Optional[str] = None   # PLINK .bim (relative path)
    fam:               Optional[str] = None   # PLINK .fam (relative path)
    n_samples:         Optional[int] = None
    n_snps:            Optional[int] = None
    genome_build:      Optional[str] = None
    upstream_pipeline: Optional[str] = None   # pipeline that produced this genotype file


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
      reads, bam_input, vcf_input, phenotype, pedigree, or assembly_input.

    genome is Optional because:
      - reference-free assemblers (hifiasm, Flye) need no reference at all
      - phenotype scorers / prioritizers don't use a FASTA
    assembly_input captures the case where the 'reference' is a draft assembly from a
    prior pipeline step (Hi-C scaffolding, polishing) rather than the canonical genome.
    """
    pipeline:           str
    pipeline_spec:      str                  # relative path to config/pipelines/*.yaml
    conda_env:          str                  # env directory basename
    created_at:         str                  # ISO date YYYY-MM-DD
    tool_versions:      dict[str, str]
    genome:              Optional[GenomeRef] = None    # None for reference-free tools
    reads:               Optional[list[ReadInput]] = None
    bam_input:           Optional[BamInput] = None
    vcf_input:           Optional[VcfInput] = None
    assembly_input:      Optional[AssemblyInput] = None   # draft contig FASTA as primary input
    phenotype:           Optional[PhenotypeInput] = None
    pedigree:            Optional[PedigreeInput] = None
    genotype_array:      Optional[GenotypeArrayInput] = None   # HapMap/PLINK/BGEN for GWAS
    quantitative_traits: Optional[QuantitativeTraitInput] = None  # continuous trait measurements
    upstream_pipelines:  list[str] = []
    parameters:          Optional[dict[str, Any]] = None
    outputs:             list[OutputFile]

    @model_validator(mode="after")
    def _require_input(self) -> "Provenance":
        has_input = any([
            self.reads, self.bam_input, self.vcf_input,
            self.assembly_input, self.phenotype, self.pedigree,
            self.genotype_array, self.quantitative_traits,
        ])
        if not has_input:
            raise ValueError(
                "Provenance must specify at least one input: "
                "reads, bam_input, vcf_input, assembly_input, phenotype, pedigree, "
                "genotype_array, or quantitative_traits"
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
        if self.assembly_input:
            paths["assembly"] = (base / self.assembly_input.assembly).resolve()
        if self.pedigree:
            paths["ped"] = (base / self.pedigree.ped).resolve()
        if self.genotype_array:
            paths["genotype"] = (base / self.genotype_array.file).resolve()
            if self.genotype_array.bim:
                paths["genotype_bim"] = (base / self.genotype_array.bim).resolve()
            if self.genotype_array.fam:
                paths["genotype_fam"] = (base / self.genotype_array.fam).resolve()
        if self.quantitative_traits:
            paths["traits_file"] = (base / self.quantitative_traits.file).resolve()
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
    # Pod5 / nanopore raw-signal metadata. All optional — populated by
    # add_core_pod5_data; absent on FASTQ entries. Recorded so downstream
    # basecaller pipelines (dorado, bonito, remora) know which model to use
    # without re-probing the binary. Methylation/base-mod models (5mC, 6mA,
    # etc.) are derived from `chemistry` + the basecaller's paired modbase
    # model registry at run time — not pre-declared here.
    file_format:     Optional[str] = None    # e.g. "pod5", "fast5"; omitted for fastq
    chemistry:       Optional[str] = None    # e.g. "dna_r10.4.1_e8.2_400bps_5khz"
    flowcell:        Optional[str] = None    # e.g. "FLO-PRO114M"
    kit:             Optional[str] = None    # e.g. "SQK-LSK114"
    suggested_model: Optional[str] = None    # e.g. "dna_r10.4.1_e8.2_400bps_hac@v5.0.0"

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
# Phenopacket metadata — GA4GH phenopacket clinical/genomic record
# ---------------------------------------------------------------------------


class PhenopacketMeta(BaseModel):
    """
    Parsed metadata for a GA4GH Phenopacket v2.x JSON.

    Populated exclusively from the phenopacket dict via from_phenopacket() —
    nothing is filled in manually.  Written as a YAML sidecar alongside the
    downloaded JSON so gen_manifest.py can rebuild the manifest without
    re-parsing the JSON.
    """
    phenopacket_id:     str
    subject_id:         str
    sex:                Optional[str] = None
    diseases:           list[dict[str, Any]] = []  # [{id, label, onset?}]
    genes:              list[str] = []              # HGNC symbols, in order of appearance
    hpo_terms:          list[str] = []              # present HP: IDs
    hpo_terms_excluded: list[str] = []              # excluded HP: IDs
    variants:           list[dict[str, Any]] = []   # one entry per genomicInterpretation
    genome_assembly:    str = ""
    schema_version:     str = ""
    source_url:         str
    file:               str                         # path relative to core_test_data dir

    @classmethod
    def from_phenopacket(
        cls,
        data: dict[str, Any],
        source_url: str,
        rel_file: str,
    ) -> "PhenopacketMeta":
        """Parse a raw GA4GH phenopacket dict — the only place field extraction lives."""
        subject  = data.get("subject", {})
        meta_raw = data.get("metaData", {})

        # Phenotypic features → present / excluded HPO term lists
        hpo_present: list[str] = []
        hpo_excluded: list[str] = []
        for pf in data.get("phenotypicFeatures", []):
            term_id = pf.get("type", {}).get("id", "")
            if not term_id:
                continue
            (hpo_excluded if pf.get("excluded", False) else hpo_present).append(term_id)

        # Diseases
        diseases: list[dict[str, Any]] = []
        for d in data.get("diseases", []):
            entry: dict[str, Any] = {
                "id":    d.get("term", {}).get("id", ""),
                "label": d.get("term", {}).get("label", ""),
            }
            onset = d.get("onset", {}).get("age", {}).get("iso8601duration")
            if onset:
                entry["onset"] = onset
            diseases.append(entry)

        # Genes + variants from interpretations
        genes: list[str] = []
        variants: list[dict[str, Any]] = []
        genome_assembly = ""

        for interp in data.get("interpretations", []):
            for gi in interp.get("diagnosis", {}).get("genomicInterpretations", []):
                vi = gi.get("variantInterpretation", {})
                vd = vi.get("variationDescriptor", {})

                gene = vd.get("geneContext", {}).get("symbol", "")
                if gene and gene not in genes:
                    genes.append(gene)

                hgvs = {e["syntax"]: e["value"] for e in vd.get("expressions", [])}

                vcf_rec = vd.get("vcfRecord", {})
                if vcf_rec.get("genomeAssembly"):
                    genome_assembly = vcf_rec["genomeAssembly"]

                variant: dict[str, Any] = {"gene": gene} if gene else {}
                for syntax_key, field in [("hgvs.c", "hgvs_c"), ("hgvs.g", "hgvs_g"), ("hgvs.p", "hgvs_p")]:
                    if syntax_key in hgvs:
                        variant[field] = hgvs[syntax_key]
                if vcf_rec:
                    variant["chrom"] = vcf_rec.get("chrom", "")
                    variant["pos"]   = vcf_rec.get("pos")
                    variant["ref"]   = vcf_rec.get("ref", "")
                    variant["alt"]   = vcf_rec.get("alt", "")
                allelic_state = vd.get("allelicState", {}).get("label")
                if allelic_state:
                    variant["allelic_state"] = allelic_state
                acmg = vi.get("acmgPathogenicityClassification")
                if acmg and acmg != "NOT_PROVIDED":
                    variant["acmg_classification"] = acmg
                if variant:
                    variants.append(variant)

        return cls(
            phenopacket_id=data.get("id", ""),
            subject_id=subject.get("id", ""),
            sex=subject.get("sex"),
            diseases=diseases,
            genes=genes,
            hpo_terms=hpo_present,
            hpo_terms_excluded=hpo_excluded,
            variants=variants,
            genome_assembly=genome_assembly,
            schema_version=meta_raw.get("phenopacketSchemaVersion", ""),
            source_url=source_url,
            file=rel_file,
        )

    def to_yaml(self) -> str:
        data = self.model_dump(exclude_none=True)
        return yaml.dump(data, default_flow_style=False, sort_keys=False)

    def write(self, path: str | Path) -> Path:
        out = Path(path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(self.to_yaml())
        return out

    @classmethod
    def from_yaml(cls, path: str | Path) -> "PhenopacketMeta":
        with open(path) as f:
            data = yaml.safe_load(f) or {}
        return cls(**data)


# ---------------------------------------------------------------------------
# Pipeline spec — one installed + validated pipeline
# ---------------------------------------------------------------------------

PipelineStatus = Literal[
    "fully_validated",       # every step's outputs passed validate_output
    "partially_validated",   # some steps validated, others ran but were not validated
    "complete",              # all steps ran cleanly but no validation was recorded
    "in_progress",
    "failed",                # any step exited non-zero, or validate_output failed
    "timeout",
]


class PackageRecord(BaseModel):
    """
    One package installed as part of a pipeline.

    install_method describes HOW it was installed (conda, jar download, pip, …).
    If absent, conda is assumed (backward compatible with existing specs).
    """
    model_config = ConfigDict(extra="allow")

    name:              str
    requested_version: str = "latest"
    resolved_version:  Optional[str] = None   # conda recipe / install version
    runtime_version:   Optional[str] = None   # what packageVersion('X') / pip show returns at library() time — only set when it differs from resolved_version
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


class ShippedBinary(BaseModel):
    """One tool baked into the shipped image OUTSIDE the package-manager closure —
    a source build, a release binary, a jar, a synthesized script. The SBOM cannot
    see these (a hand-compiled C binary carries no metadata in any packaging system,
    ever), so this record is the only place their identity exists.

    THIS MODEL IS A GATE, NOT DOCUMENTATION. It is validated at the Layer-1 disk
    seam (`EnvCache.register`), the analog of `spec_writer.py`'s `model_validate` for
    Layer 2. Read it through `freeze.shipped_binaries(record)` — never `.get()` a key
    off the raw dict, which is how the drift below happened.

    Two design rules, both bought with a real defect:

    1. `extra="forbid"`. Three producers wrote three key-dialects; `user_guide.py`
       read `platform`/`sha256`, keys NO producer has ever written, and rendered
       `shipped binary: None (None, sha256 …)` into a user-facing guide. Forbidding
       extras turns a producer's private dialect into a write-time error.

    2. NO FABRICATING DEFAULTS — every field is REQUIRED, and `None` is a legal value
       a producer must state explicitly. `WorkflowSpec` is the counter-example in this
       repo: its `outputs: list[str] = []` stamps an empty list into every sealed spec
       while the truth lives elsewhere. A permissive model with defaults does not catch
       drift, it AUTHORS it. "I did not capture this" is a fact; it must be written
       down, not defaulted into existence.

    `tool` vs `install_command` are SEPARATE fields because the old shared `command`
    key carried both meanings: `freeze_tools` wrote the literal shell line
    ("git clone https://…"), `freeze_from_image` wrote the binary name ("bcftools").
    One key, opposite semantics — so the ENV report rendered four binaries labelled
    "tool" with `bcftools` inside a <pre> shell block. Splitting them is the fix.
    """
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1)
    """The command as it exists on PATH in the shipped image — `bcftools`, `seqtk`.
    The one thing a user types. Never a shell line, never prose.

    NON-EMPTY, enforced: a shipped binary nobody can name is not a record of anything,
    and `tool=""` would sail through a bare `str` and land in a report as a blank cell.
    Every install_commands generator emits this; if it arrives empty, a producer
    dropped it and that must be loud here rather than rendered as nothing."""

    version: Optional[str]
    """Semantic version or commit sha, as the tool itself reports it. `None` means
    NOT CAPTURED — and readers MUST render that as absence ("unrecorded"), never as a
    value. Absence of data must never render as data. Do NOT scrape this from a
    multi-line banner: `bcftools --version` prints "bcftools 9cef4057\\nUsing htslib
    1.23.1", and a regex over that returns HTSLIB's version for bcftools — the exact
    lie that motivated this model. The producer captures; the reader must not guess."""

    provenance: Optional[str]
    """Prose: where these bytes came from ("populationgenomics/bcftools csq fork,
    compiled from source (htslib 1.23.1)"). For a long-tail step the baked command IS
    the provenance, so this may be None when `install_command` carries it."""

    install_command: Optional[str]
    """The literal shell line baked into the image, verbatim, when this binary came
    from a long-tail build step. `None` for a binary adopted from an authors' image —
    we did not build it, so there is no command to show."""

    tier: Optional[str]
    """Which install tier produced it (binary/source/jar/synthesized/…)."""

    verified: Optional[bool]
    """Whether the tier's install→ship integrity chain was verified."""

    assurance: Optional[str]
    """The C5 ship-assurance key (`authenticated`, `commit_pinned`, `unpinned`, …) —
    HOW this tool is anchored. Rendered as a pill by `_assurance_badge`. `None` = the
    tier disclosed nothing, which is itself a disclosure."""


def shipped_binaries(record: dict) -> list[ShippedBinary]:
    """THE reader for `record["shipped_binaries"]`. Every consumer goes through here.

    Rule 4 ("one truth, one definition, read at every use") in its mechanical form.
    Four readers used to `.get()` keys straight off the raw dicts and each invented its
    own dialect — `name or purpose`, `platform`, `sha256` — so each one degraded
    silently and differently against the same record. Attribute access on a validated
    model turns every one of those into an error at the seam instead of a `None` in a
    shipped deliverable.

    STRICT BY DESIGN: raises `pydantic.ValidationError` on any record whose shape does
    not conform. That is Rule 3 (fail closed on an unrecognized shape) — a record we
    cannot read is not a record we may render. `EnvCache.register` validates on the way
    in, so anything freeze just wrote parses here; a record on disk that does NOT parse
    predates the schema and is surfaced by `env_honesty.check_build`'s WELL_FORMED
    clause as `contract_ok: false` — re-freeze it, do not backfill it.
    """
    return [ShippedBinary.model_validate(b) for b in (record.get("shipped_binaries") or [])]


class ToolIdentity(BaseModel):
    """What a requested tool says it IS — its OWN self-description, captured at freeze
    from the registry that ships it. This is the audit's finding #8 ("identity never
    reaches disk"): the Layer-1 contract proves a tool WORKS (VALIDATED_IN_IMAGE) but
    never that it is the tool you MEANT. A human reading ENV.html could not tell
    `cellranger`-the-Cell-Ranger from CRAN's `cellranger`, "Translate Spreadsheet Cell
    Ranges". Persisting the resolved package's own words is what lets them.

    AGENT-ASSERTED, NEVER runtime-verified. `self_description` is the registry's words,
    fetched at freeze — a claim to READ, not a validated capability. It is the same
    category as `notes`/`description`: honestly labelled, explicitly OUTSIDE the verified
    surface. It gates nothing (a bad description never fails a freeze); it discloses.

    Tied to the INSTALL, not a bare-name guess. `source` is the registry the shipped
    package actually came from (learned from the in-image SBOM), so a correctly-installed
    ONT `dorado` binary is NOT mislabelled with astronomy-PyPI's summary — a tool with no
    registry package in the image gets `self_description: None` and honest silence, never
    a borrowed identity.

    Same seam discipline as `ShippedBinary`: `extra="forbid"` + NO fabricating defaults —
    every field REQUIRED, `None` a value the producer must STATE. Validated at BOTH ends
    (`EnvCache.register` on write, `check_build` WELL_FORMED on serve)."""
    model_config = ConfigDict(extra="forbid")

    tool: str = Field(min_length=1)
    """The REQUESTED tool name, as the user asked for it. Non-empty: an identity record
    for a tool nobody can name discloses nothing."""

    self_description: Optional[str]
    """The package's OWN one-line words, verbatim from the registry (conda `about.summary`,
    PyPI `Summary`). `None` = NOT CAPTURED — either the tool ships from a non-registry tier
    (binary/source/jar: there is no registry blurb) or the published entry carried none, or
    the freeze-time probe failed. Absence is a fact; readers render it as "(no self-
    description)", never as an empty capability claim."""

    source: Optional[str]
    """Which registry the description was read from and which the shipped package came from:
    `conda` | `pypi` | `None`. `None` pairs with `self_description=None` — no registry
    package was matched in the image, so there is nothing to attribute and nothing is."""

    package: Optional[str]
    """The exact installed package name the description belongs to (`r-seurat`,
    `pysam`) — may differ from `tool`. `None` when no registry package matched."""

    version: Optional[str]
    """The installed version of `package`, from the in-image SBOM. `None` when unmatched."""


def tool_identities(record: dict) -> list[ToolIdentity]:
    """THE reader for `record["tool_identities"]` — the ToolIdentity analog of
    `shipped_binaries`. Every consumer (ENV.html, attestation, recipe) goes through here,
    so the disclosure has ONE definition read at every use (Rule 4). STRICT: raises
    `pydantic.ValidationError` on a record whose shape does not conform, so a record we
    cannot read is never rendered. Absent key ⇒ `[]` (records frozen before identity-to-
    disk existed are grandfathered, not backfilled — [[feedback-existing-installs-not-precious]])."""
    return [ToolIdentity.model_validate(t) for t in (record.get("tool_identities") or [])]


def record_is_gated(record: dict) -> bool:
    """THE reader for "is this EnvCache record a license-gated artifact?" (I13).

    Reads the canonical `license_gated`, falling back to the legacy `gated` key that
    records written before the 2026-07-16 unification carry on disk. Every consumer of
    "is this gated" goes through here so the two names can never drift apart again —
    drifting apart is exactly how I13 stopped firing on the authors'-image path.

    LIVES HERE, beside the other record readers, and not in `freeze` — because the
    consumer that matters most is the CONTRACT (`env_honesty._clause_license`), and
    `env_honesty` cannot import `freeze` (freeze imports it). While the canonical reader
    sat behind that cycle the contract kept its own one-key copy, so a record carrying
    only the legacy `gated` key passed I13 by default: the deduplication had reached the
    four rendering callers and stopped one import short of the gate it was written for.
    A shared fact belongs in a leaf, or it is not actually shared."""
    if "license_gated" in record:
        return bool(record.get("license_gated"))
    return bool(record.get("gated", False))


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


class InstallStep(BaseModel):
    """One environment-install command in the pipeline build journey.

    Distinct from PipelineStep: install_steps record HOW the environment was
    built (conda create, conda install, BiocManager::install, remotes::install_github,
    apt-get install, downloading reference databases, etc.). They do not have a
    validation_status because installs don't produce data outputs to validate —
    install success is captured by returncode==0 in `status`, and the functional
    check that the installed package actually works is captured separately in
    each PackageRecord's verify_command / verify_output.

    installed_packages records which package(s) this command installed, so the
    HTML report can show "command → packages installed" side-by-side."""
    model_config = ConfigDict(extra="allow")

    step:               int                   # 1-based; sequential within install_steps
    tool:               str                   # e.g. "conda", "R", "pip", "apt"
    subcommand:         Optional[str] = None  # e.g. "create", "install", "BiocManager::install"
    purpose:            Optional[str] = None
    command:            str
    installed_packages: list[dict[str, Any]] = []  # [{name, version, channel?}, ...]
    status:             Literal["installed", "failed", "skipped"] = "installed"
    returncode:         Optional[int] = None
    runtime_seconds:    Optional[float] = None

    @model_validator(mode="after")
    def _derive_status_from_returncode(self) -> "InstallStep":
        if self.returncode is not None:
            self.status = "installed" if self.returncode == 0 else "failed"
        return self


class ResourceUsage(BaseModel):
    """Observed resource consumption of one pipeline_step subprocess.

    Populated by env_manager._run_monitored() — wall time from monotonic clock,
    peak_rss_mb from psutil polling the process tree (Popen + all descendants),
    max_cpu_percent likewise. These are observations of a real execution; the
    agent cannot synthesize them without bypassing the run primitive.

    Invariant I7 refuses to finalize a spec if any rc=0 pipeline_step lacks
    resource_usage — HPC schedulers downstream of us need honest cost data.
    """
    model_config = ConfigDict(extra="allow")

    wall_seconds:    float
    peak_rss_mb:     float
    max_cpu_percent: float = 0.0
    sample_count:    int   = 0    # how many polls fed the peaks; 0 means monitoring failed
    peak_gpu_mb:     Optional[float] = None   # set only when nvidia-smi is available


class StepInput(BaseModel):
    """One input file to a pipeline step.

    `references` captures files the input *opens at runtime*. A bare R / Python /
    bash script appears as a `path`, and the data files it reads (via system.file,
    open(), source(), etc.) go in `references`. Same shape works for a tool config
    that points at reference FASTAs, a workflow YAML that names other inputs, etc.

    Tool calls may pass either a string (treated as path-only) or a dict; the
    PipelineStep validator coerces strings to StepInput(path=s).
    """
    model_config = ConfigDict(extra="allow")

    path:       str
    references: list[str] = []


class PipelineStep(BaseModel):
    """One algorithm / analysis step within a pipeline run.

    Distinct from InstallStep: pipeline_steps are the data-producing steps
    of the pipeline itself (bwa mem, GATK HaplotypeCaller, GAPIT::GAPIT, etc.).

    Two orthogonal status fields:
      - status: did the command exit cleanly? Derived from returncode.
      - validation_status: did validate_output confirm the produced files?
        None means validate_output was never called for this step's outputs.

    A pipeline can only claim PipelineStatus.fully_validated if every step
    has validation_status="passed" — exited 0 alone is not enough."""
    model_config = ConfigDict(extra="allow")

    step:              int
    tool:              str
    subcommand:        Optional[str] = None
    purpose:           Optional[str] = None
    command:           str
    status:            Literal["validated", "failed", "skipped"] = "validated"
    validation_status: Optional[Literal["passed", "failed"]] = None
    returncode:        Optional[int] = None

    @model_validator(mode="after")
    def _derive_status_from_returncode(self) -> "PipelineStep":
        if self.returncode is not None:
            self.status = "validated" if self.returncode == 0 else "failed"
        return self

    inputs:            list[StepInput] = []   # files consumed (with optional script-references)
    outputs:           list[str] = []         # filenames produced
    depends_on:        list[int] = []         # step numbers this step depends on (1-based); derived at finalize from input/output overlap if absent
    runtime_seconds:   Optional[float] = None
    output_size_bytes: Optional[int] = None
    validation:        Optional[Any] = None
    resource_usage:    Optional[ResourceUsage] = None   # I7 — observed wall/RSS/CPU from psutil monitor
    # L11 universal-lineage anchor: sha256 of each detected_output at
    # production time. Hash is computed by env_manager.hash_outputs after
    # the step completes; seal-time _check_lineage_integrity uses it to
    # verify same-path-same-bytes across consumer steps. Optional for
    # back-compat with older recorded runs.
    output_sha256:     Optional[dict[str, str]] = None
    # Where an OFF-HOST step's outputs live at the locus that produced them.
    # `detected_outputs` above holds the DOWNLOADED LOCAL copies — right for
    # validation, since those are the bytes we hashed and typed — but a second
    # cluster step consumes the remote originals, and no local path can match
    # one. I8 unions this into the lineage universe and I6 holds it to the same
    # absoluteness rule, which is exactly why it is DECLARED here rather than
    # left to ride on `extra="allow"`: a field two invariants read is
    # load-bearing, and an undeclared one lets a typo'd key vanish into extras
    # while the suite stays green. (The shipped_binaries lesson, one layer down.)
    # Optional/None, NOT `= []`: `to_yaml` excludes None, so a local step does
    # not carry a meaningless empty key. `outputs: list[str] = []` is the
    # counter-example in this very model — it stamps an empty list into every
    # sealed spec while the truth lives elsewhere, which is a default AUTHORING
    # drift rather than catching it.
    remote_outputs:    Optional[list[str]] = None
    # The scheduler's verdict on the job that produced this step, STATED rather
    # than left to be re-derived from cluster_state + cluster_exit_code. Deriving
    # it is the defect: SLURM reports a signal death as `<rc>:<signal>`, so a
    # reader who checks the exit code first reads every scheduler-killed job as
    # clean. Absent on a step that did not run off-host.
    cluster_job_verdict: Optional[str] = None

    @field_validator("inputs", mode="before")
    @classmethod
    def _coerce_inputs(cls, v: Any) -> Any:
        """Accept either ['foo.R', ...] or [{path, references}, ...] from the wire."""
        if not isinstance(v, list):
            return v
        out: list[Any] = []
        for item in v:
            if isinstance(item, str):
                out.append({"path": item, "references": []})
            else:
                out.append(item)
        return out


class UsageInput(BaseModel):
    """One named slot in a usage command template."""
    model_config = ConfigDict(extra="allow")

    name:        str                          # placeholder name, e.g. "INPUT_GENOTYPE"
    format:      Optional[str] = None         # e.g. "hapmap", "vcf", "fastq"
    description: Optional[str] = None
    required:    bool = True


class UsageOutput(BaseModel):
    """One named output slot in a usage command template."""
    model_config = ConfigDict(extra="allow")

    name:        str                          # placeholder, e.g. "OUTPUT_DIR"
    files:       list[str] = []               # expected output filename patterns
    description: Optional[str] = None


class UsageTrial(BaseModel):
    """One explicit input shape the usage.command_template must handle.

    Declaring multiple trials lets the finalize-time self-test prove the
    assembled machinery isn't a one-trick — it works across the input
    variations a downstream user will throw at it (paired-gz vs uncompressed,
    short vs long reads, etc.). Each trial's `substitutions` map fills the
    {PLACEHOLDER} slots in `command_template`; the runtime executes each
    trial in a fresh scratch dir and confirms declared outputs are produced.

    Invariant I4 only sets usage_verified=True if every declared trial passes.
    If `usage.trials` is empty, the runtime falls back to a single inferred
    trial (backward-compatible — same as before the multi-shape extension).
    """
    model_config = ConfigDict(extra="allow")

    name:          str                       # human label, e.g. "paired_gz", "single_uncompressed"
    substitutions: dict[str, str]            # {PLACEHOLDER: absolute_path_or_value}
    description:   Optional[str] = None


class UsageTemplate(BaseModel):
    """How to invoke the pipeline on new data.

    Distinct from pipeline_steps: pipeline_steps records what we ran to
    build/test the pipeline; usage records the canonical command(s) a downstream
    user (or Nextflow generator) should run on *their* data.

    LLM authors this via patch_pipeline after Phase 4. The command_template
    uses {PLACEHOLDER} substitutions whose names line up with `inputs` and
    `outputs` entries.

    command_template is a str OR a list[str] — one entry per command, run IN
    ORDER, sharing one working directory (so step 2 consumes step 1's output).
    It was `str` alone, which made a real multi-phase pipeline INEXPRESSIBLE:
    `pipeline_steps` is a list and I8 lineage already holds across a chain, but
    the how-to contract — the thing a user actually reads and runs, and the thing
    the guides will render — could only ever say one command. No amount of later
    guide design can fix data that cannot say what you mean (audit 2026-07-16).

    A bare str is still valid and means exactly what it always did; read either
    shape through `usage_commands()`, never by branching on the type at the call
    site. Two spellings of one concept is precisely how this codebase's defects
    are born — see usage_commands.

    trials: optional list of explicit input-shape test cases. When non-empty
    the finalize self-test runs every trial and only marks usage_verified=True
    if all pass. When empty the runtime infers a single trial from
    pipeline_steps' inputs (backward-compatible).
    """
    model_config = ConfigDict(extra="allow")

    description:      str
    command_template: Union[str, list[str]]
    inputs:           list[UsageInput] = []
    outputs:          list[UsageOutput] = []
    trials:           list[UsageTrial] = []   # I4 — multi-shape self-test cases
    example:          Optional[str] = None    # concrete invocation example


def usage_commands(usage: Any) -> list[str]:
    """The ordered, non-empty commands in a usage block — THE one reading of
    `command_template`, for every consumer (I4 self-test, I6 placeholder scan, the
    RUN dashboard, the user guide).

    `command_template` accepts a str or a list[str]. That is two shapes, and two
    shapes invite two readings: the moment a renderer does `usage["command_template"]
    .strip()` and the self-test does something subtly different, they disagree about
    what the workflow is — which is this project's entire disease (one truth, N
    definitions, the stale one in the load path). So the shape is normalized in
    exactly one function and nothing else may branch on the type.

    Accepts a dict or a UsageTemplate; tolerates None/garbage by returning []."""
    if usage is None:
        return []
    ct = (usage.get("command_template") if isinstance(usage, dict)
          else getattr(usage, "command_template", None))
    if isinstance(ct, str):
        ct = [ct]
    if not isinstance(ct, list):
        return []
    return [c.strip() for c in ct if isinstance(c, str) and c.strip()]


#: What the I4 self-test actually concluded. `""` only for a spec so malformed it has
#: neither field.
USAGE_VERIFIED = "verified"
USAGE_FAILED = "failed"
USAGE_NOT_ATTEMPTED = "not_attempted"


#: How each state is SHOWN. In the leaf with `usage_status` for the same reason the
#: status is: the dashboard rendered "not attempted" while the markdown guide printed
#: the raw enum, so two artifacts about one workflow read differently. One fact, one
#: reading, one wording. "" is the pre-three-state fallback and means the same thing.
USAGE_LABELS = {
    USAGE_VERIFIED:      "yes",
    USAGE_FAILED:        "NO — self-test failed",
    USAGE_NOT_ATTEMPTED: "not attempted",
    "":                  "not attempted",
}


def usage_label(spec: Any) -> str:
    """The human wording for a spec's I4 verdict — what a REPORT should print."""
    st = usage_status(spec)
    return USAGE_LABELS.get(st, st)


def usage_status(spec: Any) -> str:
    """THE one reading of whether a sealed workflow's how-to was actually proven —
    for every consumer (the RUN dashboard, the markdown guide, the inventory row).

    Same rule, same reason as `usage_commands` directly above: one field, one reading.
    `usage_verified` is a bool over a THREE-state fact, and the collapse is not
    symmetric — seal REFUSES a genuine I4 failure, so `false` on a sealed spec has only
    ever meant "never attempted", a verdict nobody reached being rendered as one.
    `usage_verification.status` carries the truth; the bool is the fallback for specs
    sealed before that field existed.

    This exists as a leaf because the alternative was measured, not imagined: the
    dashboard had already grown its own private derivation while the guide printed the
    raw bool, so one page said "not attempted — <reason>" and another said `False`
    about the same workflow. A shared fact belongs in a leaf, or it is not shared.

    Accepts a dict or a WorkflowSpec."""
    if spec is None:
        return ""
    get = (spec.get if isinstance(spec, dict)
           else lambda k, d=None: getattr(spec, k, d))
    uv = get("usage_verification") or {}
    status = uv.get("status") if isinstance(uv, dict) else getattr(uv, "status", None)
    if status:
        return str(status)
    return USAGE_VERIFIED if get("usage_verified") else ""


class WorkflowSpec(BaseModel):
    """Layer 2 — a workflow that runs on a FROZEN environment, pinned by digest.

    The two-layer split: an ENVIRONMENT is solved once (Layer 1 — finalize +
    freeze + EnvCache, content-addressed) and a WORKFLOW *consumes* it. The
    workflow is the run-many, per-experiment artifact; pinning the env by
    content_digest is the wall that lets the biology layer ignore install
    concerns entirely. Where PipelineSpec embeds the env build (packages,
    install_steps, docker), a WorkflowSpec only REFERENCES the env by digest and
    carries the validated run + usage contract + the rendered user guide + the
    driver env (the env that runs the workflow — the gap nf-core/Snakemake
    leave open).
    """
    model_config = ConfigDict(extra="allow")

    workflow_name:      str
    description:        str
    created_at:         str
    # The environment this workflow runs on — pinned by digest (Layer-1 artifact).
    env_request_key:    str
    env_content_digest: str
    env_image:          str                       # adopt/built shipping handle (by digest)
    env_hpc_delivery:   dict = {}                  # how to get the env onto the HPC
    # The validated run. REQUIRED, no default: the producer (seal) must DERIVE and
    # STATE the real run status from the steps — a defaulted "in_progress" stamped
    # into every sealed spec is the "permissive model with defaults authors drift"
    # anti-pattern (the outputs: list[str]=[] lesson). seal computes it via
    # spec_writer.derive_pipeline_status; a spec that omits it fails model_validate.
    pipeline_status:    PipelineStatus
    usage_verified:     bool = False
    # WHY usage_verified is what it is. The bool alone conflates "tested and it FAILED"
    # with "never tested" — and since seal REFUSES the former, every usage_verified=False
    # that reached disk meant the latter, while dashboards rendered it as a verdict on the
    # how-to. {status: verified|failed|not_attempted, reason, locus, trial_count, passed}.
    # Renderers must consult this before making any claim about the how-to.
    usage_verification: Optional[dict] = None
    # validated == shipped: every validated step ran INSIDE the pinned env image
    # (matched by digest), so the bytes the user runs are the bytes we tested.
    validated_in_shipped_image: bool = False
    usage:              Optional[UsageTemplate] = None
    pipeline_steps:     list[PipelineStep] = []
    # External input sources the run consumes. Carried so the WorkflowSpec is
    # SELF-VERIFYING: I8 (every step input traces to a prior output or an external
    # source) can be re-checked against the artifact alone, not just the draft it
    # was sealed from.
    test_data:            Optional[TestDataRef] = None
    reference_databases:  list[ReferenceDatabase] = []
    runtime_configs:      list[RuntimeConfig] = []
    authored_artifacts:   list[AuthoredArtifact] = []
    # Not an input SOURCE — a runtime PREREQUISITE, carried for the same reason.
    # I10 (every declared service has a healthy probe) ran at seal against the
    # draft and then the field was dropped from the artifact, so the sealed spec
    # re-verified it against nothing: a workflow that genuinely depends on a
    # running Redis/Postgres/Spark read, standalone, as one that depends on no
    # service at all. Carrying it makes seal's own self-verify (which validates
    # the constructed spec, not the draft) actually re-check the clause.
    service_dependencies: list[ServiceDependency] = []
    # Driver env: what runs the workflow (records the orchestrator's env, too).
    driver_env:         dict = {}                  # {conda_env, python_version, key_packages}
    user_guide:         Optional[str] = None       # rendered markdown (from the passing run)
    user_guide_path:    Optional[str] = None

    def to_yaml(self) -> str:
        return yaml.dump(self.model_dump(exclude_none=True),
                         default_flow_style=False, sort_keys=False)


