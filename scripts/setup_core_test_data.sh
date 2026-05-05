#!/usr/bin/env bash
# setup_core_test_data.sh — Bootstrap conda environments and reference genome.
#
# Usage:
#   ./scripts/setup_core_test_data.sh [--genome-build BUILD]
#
# Options:
#   --genome-build BUILD   hg38 | mm10 | ecoli (default: hg38)
#
# This script is infrastructure-only:
#   - Installs bioinf_validators  (samtools + bcftools + seqkit)
#   - Installs bioinf_bwa_samtools (bwa + samtools, needed for genome indexing)
#   - Downloads the reference genome chromosome and builds indexes
#
# Reads, pipeline runs, and HTML report generation are handled by the agent:
#   python -m agent.main
#   > add test data: SRR1517830, exome, hg38
#   > install bwa_samtools and freebayes as my wgs_variant_pipeline

set -euo pipefail

GENOME_BUILD="hg38"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --genome-build) GENOME_BUILD="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$PROJECT_ROOT/data"
CORE_DIR="$DATA_DIR/core_test_data_${GENOME_BUILD}"
VALIDATORS_ENV="$PROJECT_ROOT/envs/bioinf_validators"
BWA_ENV="$PROJECT_ROOT/envs/bioinf_bwa_samtools"

CONDA_EXE=$(which conda 2>/dev/null || echo "")
if [[ -z "$CONDA_EXE" ]]; then
  echo "ERROR: conda not found in PATH. Activate your conda base environment first." >&2
  exit 1
fi

log()  { echo "[setup] $*"; }
skip() { echo "[setup] SKIP (exists): $*"; }

# ---------------------------------------------------------------------------
# 1. Conda environments
# ---------------------------------------------------------------------------
if [[ ! -d "$VALIDATORS_ENV" ]]; then
  log "Installing bioinf_validators (samtools + bcftools + seqkit)..."
  conda create -y -p "$VALIDATORS_ENV" \
    -c bioconda -c conda-forge \
    samtools bcftools seqkit conda-pack 'python=3.11' \
    2>&1 | grep -E "^(Preparing|Downloading|Executing|done|ERROR)" || true
  log "bioinf_validators installed."
else
  skip "bioinf_validators ($VALIDATORS_ENV)"
fi

if [[ ! -d "$BWA_ENV" ]]; then
  log "Installing bioinf_bwa_samtools (bwa + samtools)..."
  conda create -y -p "$BWA_ENV" \
    -c bioconda -c conda-forge \
    bwa samtools conda-pack 'python=3.11' \
    2>&1 | grep -E "^(Preparing|Downloading|Executing|done|ERROR)" || true
  log "bioinf_bwa_samtools installed."
else
  skip "bioinf_bwa_samtools ($BWA_ENV)"
fi

# ---------------------------------------------------------------------------
# 2. Reference genome
# ---------------------------------------------------------------------------
mkdir -p "$CORE_DIR/genome"

case "$GENOME_BUILD" in
  hg38)
    GENOME_FA="$CORE_DIR/genome/chr22.fa"
    GENOME_URL="https://hgdownload.soe.ucsc.edu/goldenPath/hg38/chromosomes/chr22.fa.gz"
    ;;
  mm10)
    GENOME_FA="$CORE_DIR/genome/chr19.fa"
    GENOME_URL="https://hgdownload.soe.ucsc.edu/goldenPath/mm10/chromosomes/chr19.fa.gz"
    ;;
  ecoli)
    GENOME_FA="$CORE_DIR/genome/genome.fa"
    GENOME_URL="https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/005/845/GCF_000005845.2_ASM584v2/GCF_000005845.2_ASM584v2_genomic.fna.gz"
    ;;
  *)
    echo "ERROR: unsupported --genome-build '$GENOME_BUILD'. Use hg38, mm10, or ecoli." >&2
    exit 1
    ;;
esac

if [[ ! -f "$GENOME_FA" ]]; then
  log "Downloading $(basename "$GENOME_FA") ($GENOME_BUILD)..."
  curl -fsSL --retry 3 "$GENOME_URL" | gunzip > "$GENOME_FA"
  log "Downloaded: $(du -sh "$GENOME_FA" | cut -f1)"
else
  skip "$GENOME_FA"
fi

# ---------------------------------------------------------------------------
# 3. Index genome
# ---------------------------------------------------------------------------
if [[ ! -f "$GENOME_FA.fai" ]]; then
  log "Building samtools fai index..."
  "$VALIDATORS_ENV/bin/samtools" faidx "$GENOME_FA"
else
  skip "$GENOME_FA.fai"
fi

if [[ ! -f "$GENOME_FA.bwt" ]]; then
  log "Building BWA index (~20s for chr22)..."
  "$BWA_ENV/bin/bwa" index "$GENOME_FA"
else
  skip "BWA index"
fi

# ---------------------------------------------------------------------------
# 4. Core sequencing test datasets (10K reads each, idempotent)
# ---------------------------------------------------------------------------
log "Adding core sequencing test datasets (10K reads each)..."
cd "$PROJECT_ROOT"
python3 - <<'PYEOF'
import sys, yaml
from pathlib import Path

sys.path.insert(0, str(Path.cwd()))
from agent.skills.core_test_data import add_core_test_data

config = yaml.safe_load(open("config/agent_config.yaml"))

short_read_datasets = [
    # Exome paired-end — 1000 Genomes HG00096
    dict(accession="SRR1517830", assay_type="exome",  end_type="paired_end", sample="HG00096"),
    # RNA-seq single-end — airway smooth muscle (Himes et al. 2014)
    dict(accession="SRR1039508", assay_type="rnaseq", end_type="single_end", sample="airway"),
    # RNA-seq paired-end — GEUVADIS NA20503
    dict(accession="ERR188297",  assay_type="rnaseq", end_type="paired_end", sample="NA20503"),
    # Hi-C paired-end — GM12878 in-situ Hi-C (Rao et al. 2014)
    dict(accession="SRR1658581", assay_type="hic",    end_type="paired_end", sample="GM12878"),
    # WGS paired-end — NA12878 (1000 Genomes)
    dict(accession="ERR001268",  assay_type="wgs",    end_type="paired_end", sample="NA12878"),
    # WGBS paired-end — ENCODE ENCSR890UQO (GSE86765)
    dict(accession="SRR4235788", assay_type="wgbs",   end_type="paired_end", sample="ENCSR890UQO"),
]

ok = True
for d in short_read_datasets:
    label = f"{d['accession']} ({d['assay_type']} {d['end_type']})"
    print(f"[setup] Adding {label}...")
    result = add_core_test_data(config, subset="10K", **d)
    if result.get("success"):
        print(f"[setup] OK: {d['accession']}")
    else:
        print(f"[setup] WARN: {result.get('error', 'unknown error')}", file=sys.stderr)
        ok = False

# Long-read datasets — 500 reads each, best-effort (failures are logged but don't abort)
# PacBio HiFi: EBI SRA only stores raw subreads; use GIAB NCBI FTP for CCS FASTQ directly.
GIAB_HIFI_BASE = (
    "https://ftp-trace.ncbi.nlm.nih.gov/ReferenceSamples/giab/data/"
    "AshkenazimTrio/HG002_NA24385_son/PacBio_CCS_15kb_20kb_chemistry2/reads/"
)
long_read_datasets = [
    # ONT WGS — NA12878 ultralong reads (Jain et al. 2018, PRJEB26791)
    dict(accession="ERR3152364", assay_type="ont_wgs", sample="NA12878",
         platform="ont", subset="500"),
    # PacBio HiFi — GIAB HG002, 15–20 kb CCS reads (NCBI FTP direct)
    dict(accession="HG002_CCS_15kb", assay_type="pacbio_hifi", sample="HG002",
         platform="pacbio_hifi", subset="500",
         source_url=f"{GIAB_HIFI_BASE}m64011_190830_220126.fastq.gz"),
]

for d in long_read_datasets:
    label = f"{d['accession']} ({d['assay_type']})"
    print(f"[setup] Adding {label} (best-effort)...")
    result = add_core_test_data(config, **d)
    if result.get("success"):
        print(f"[setup] OK: {d['accession']}")
    else:
        print(f"[setup] SKIP (long-read): {result.get('error', 'unknown error')}", file=sys.stderr)

sys.exit(0 if ok else 1)
PYEOF

log ""
log "Done. Core test data ready at: $CORE_DIR/"
log ""
log "Next — install pipelines via Claude Code:"
log "  install bwa_samtools and freebayes as my wgs_variant_pipeline"
