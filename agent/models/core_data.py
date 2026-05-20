"""
Core data models for the bioinformatics agent.

Single source of truth for:
  - Controlled vocabulary (ReadType, EndType, AssayType, FileType, Database)
  - InstallMethod      (conda | jar | pip | r_install | docker_pull | source | manual)
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
  - agent/skills/spec_writer.py (save_pipeline_spec + write_provenance)
  - agent/skills/resources.py   (list_available_resources reader)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, field_validator, model_validator

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

    type: Literal["conda", "jar", "pip", "r_install", "docker_pull", "source", "manual"] = "conda"
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
    local_path: Optional[str] = None   # absolute path to the clone, {env}/share/{tool}
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
    build/test the pipeline; usage records the canonical command a downstream
    user (or Nextflow generator) should run on *their* data.

    LLM authors this via patch_pipeline after Phase 4. The command_template
    uses {PLACEHOLDER} substitutions whose names line up with `inputs` and
    `outputs` entries.

    trials: optional list of explicit input-shape test cases. When non-empty
    the finalize self-test runs every trial and only marks usage_verified=True
    if all pass. When empty the runtime infers a single trial from
    pipeline_steps' inputs (backward-compatible).
    """
    model_config = ConfigDict(extra="allow")

    description:      str
    command_template: str
    inputs:           list[UsageInput] = []
    outputs:          list[UsageOutput] = []
    trials:           list[UsageTrial] = []   # I4 — multi-shape self-test cases
    example:          Optional[str] = None    # concrete invocation example


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

    build_attempted:      bool = False
    build_success:        bool = False
    image_tag:            Optional[str] = None
    registry:             str = "local"
    pushed_to_registry:   bool = False
    reason:               Optional[str] = None
    nvidia_runtime:       bool = False        # True → image requires --gpus / nvidia runtime
    volume_mounts:        list[str] = []      # e.g. ["/data/exomiser"]
    runtime_data_env:     Optional[str] = None


class PipelineSpec(BaseModel):
    """
    Complete record of an installed, validated pipeline.
    Written to config/pipelines/{name}_{version}.yaml after a successful install.

    runtime_environment: describes how the primary tool is invoked.
      - type="conda"  → standard (default for most tools)
      - type="jar"    → Java tool; openjdk is in the conda env, JAR at jar_path.
                        conda-pack bundles the JVM → Docker image is self-contained.

    reference_free: True for de novo assemblers (hifiasm, Flye, Canu) and tools
      that produce output without any reference genome.  Phase 3 skips the genome
      reference step when this is True.  Hi-C scaffolding tools set this False and
      supply assembly_input in their Provenance records instead.

    reference_databases: large external databases beyond the genome FASTA.
      These are documented here but NOT baked into the Docker image.
      Mount them at the paths listed in docker.volume_mounts.

    runtime_configs: global config files written during installation
      (e.g. application.properties for Exomiser). For agent-authored files
      (driver scripts, generated test data, hand-staged transformations),
      use stage_authored_artifact → authored_artifacts[*] instead — those
      carry a sha256 anchor (I9) that runtime_configs lacks.

    service_dependencies: companion processes (web server, database, Spark) that
      must be running before the pipeline executes.  Managed by
      EnvManager.start_service / stop_service during Phase 4.
    """
    model_config = ConfigDict(extra="allow")

    pipeline_name:        str
    description:          str
    conda_env:            str
    python_version:       Optional[str] = None
    created_at:           str
    # Two orthogonal status fields. env_status reflects whether the conda env
    # was built and verified successfully; pipeline_status reflects whether the
    # algorithm/analysis runs succeeded AND had their outputs validated.
    env_status:           PipelineStatus = "in_progress"
    pipeline_status:      PipelineStatus = "in_progress"
    docker_status:        Literal["not_attempted", "built", "failed"] = "not_attempted"
    lock_sha256:          Optional[str] = None    # sha256 of the .lock file — verify bit-exact env reproduction
    usage_verified:       bool = False             # True after self-test runs usage.command_template successfully
    packages:             list[PackageRecord]
    reference_free:       bool = False
    runtime_environment:  Optional[RuntimeEnvironment] = None   # None → conda (default)
    reference_databases:  list[ReferenceDatabase] = []
    runtime_configs:      list[RuntimeConfig] = []
    service_dependencies: list[ServiceDependency] = []
    authored_artifacts:   list[AuthoredArtifact] = []
    test_data:            Optional[TestDataRef] = None
    install_steps:        list[InstallStep] = []   # env-build journey (chronological by `step`)
    pipeline_steps:       list[PipelineStep] = []  # algorithm/analysis runs
    docker:               Optional[DockerBuild] = None
    usage:                Optional[UsageTemplate] = None  # canonical "run on new data" contract
    notes:                list[str] = []
    final_summary:        Optional[str] = None

    @field_validator("notes", mode="before")
    @classmethod
    def _wrap_str_notes(cls, v):
        if isinstance(v, str):
            return [v]
        return v

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
