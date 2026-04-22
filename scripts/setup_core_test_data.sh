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

log ""
log "Done. Reference genome ready at: $CORE_DIR/genome/"
log ""
log "Next — run the agent to add test data and install pipelines:"
log "  python -m agent.main"
log "  > add test data: SRR1517830, exome, hg38"
log "  > install bwa_samtools and freebayes as my wgs_variant_pipeline"
