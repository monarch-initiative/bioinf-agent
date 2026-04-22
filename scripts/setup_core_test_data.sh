#!/usr/bin/env bash
# setup_core_test_data.sh — Bootstrap core test data for a genome build.
#
# Usage:
#   ./scripts/setup_core_test_data.sh [GENOME_BUILD]
#
# GENOME_BUILD defaults to hg38. Currently supports: hg38
#
# Output layout:
#   data/core_test_data_{build}/
#     genome/                              chr22.fa + indexes
#     short_read/paired_end/exome/         SRR1517830_R1_100K.fastq.gz
#                                          SRR1517830_sample_meta.yaml
#     pipeline_outputs/bwa_samtools/       HG00096_SRR1517830_aligned_sorted.bam  (+ prefix)
#                                          HG00096_SRR1517830_provenance.yaml
#     pipeline_outputs/freebayes/          HG00096_SRR1517830_variants.vcf
#                                          HG00096_SRR1517830_provenance.yaml
#     manifest.yaml
#
# Raw source reads are cached in data/sources/{accession}/ to avoid re-downloading.
# Idempotent: skips any step whose output files already exist.
#
# Conda envs bioinf_bwa_samtools and bioinf_freebayes are auto-installed if missing.

set -euo pipefail

GENOME_BUILD=${1:-hg38}
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$PROJECT_ROOT/data"
CORE_DIR="$DATA_DIR/core_test_data_${GENOME_BUILD}"
SOURCES_DIR="$DATA_DIR/sources"

BWA_ENV="$PROJECT_ROOT/envs/bioinf_bwa_samtools"
FREEBAYES_ENV="$PROJECT_ROOT/envs/bioinf_freebayes"

# Sample metadata (controlled vocab)
ACCESSION="SRR1517830"
SAMPLE="HG00096"
SAMPLE_KEY="${SAMPLE}_${ACCESSION}"
SUBSET="100K"
NUM_READS=100000
EBI_BASE="ftp://ftp.sra.ebi.ac.uk/vol1/fastq/SRR151/000/${ACCESSION}"

log()  { echo "[setup_core_test_data] $*"; }
skip() { echo "[setup_core_test_data] SKIP (exists): $*"; }

if [[ "$GENOME_BUILD" != "hg38" ]]; then
  echo "ERROR: only hg38 is currently supported." >&2
  exit 1
fi

CONDA_EXE=$(which conda 2>/dev/null || echo "")
if [[ -z "$CONDA_EXE" ]]; then
  echo "ERROR: conda not found in PATH. Activate your conda base environment first." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 0. Auto-install conda envs if missing
# ---------------------------------------------------------------------------

if [[ ! -d "$BWA_ENV" ]]; then
  log "bioinf_bwa_samtools env not found — installing (bwa + samtools)..."
  conda create -y -p "$BWA_ENV" \
    -c bioconda -c conda-forge \
    bwa samtools conda-pack 'python=3.11' \
    2>&1 | grep -E "^(Preparing|Downloading|Executing|done|ERROR)" || true
  log "bioinf_bwa_samtools installed."
else
  skip "bioinf_bwa_samtools env (already at $BWA_ENV)"
fi

if [[ ! -d "$FREEBAYES_ENV" ]]; then
  log "bioinf_freebayes env not found — installing (freebayes + samtools)..."
  conda create -y -p "$FREEBAYES_ENV" \
    -c bioconda -c conda-forge \
    'freebayes=1.3.10' samtools conda-pack 'python=3.11' \
    2>&1 | grep -E "^(Preparing|Downloading|Executing|done|ERROR)" || true
  log "bioinf_freebayes installed."
else
  skip "bioinf_freebayes env (already at $FREEBAYES_ENV)"
fi

# ---------------------------------------------------------------------------
# 1. Create directory tree
# ---------------------------------------------------------------------------
log "Creating directory structure for $GENOME_BUILD / $SAMPLE_KEY..."
mkdir -p \
  "$CORE_DIR/genome" \
  "$CORE_DIR/short_read/paired_end/exome" \
  "$CORE_DIR/short_read/single_end" \
  "$CORE_DIR/short_read/mate_pair" \
  "$CORE_DIR/long_read/single_end" \
  "$CORE_DIR/pipeline_outputs/bwa_samtools" \
  "$CORE_DIR/pipeline_outputs/freebayes" \
  "$SOURCES_DIR/$ACCESSION"

# ---------------------------------------------------------------------------
# 2. Genome: download chr22 from UCSC
# ---------------------------------------------------------------------------
GENOME_FA="$CORE_DIR/genome/chr22.fa"
CHROM="chr22"

if [[ ! -f "$GENOME_FA" ]]; then
  UCSC_URL="https://hgdownload.soe.ucsc.edu/goldenPath/hg38/chromosomes/${CHROM}.fa.gz"
  log "Downloading ${CHROM}.fa from UCSC..."
  curl -fsSL --retry 3 "$UCSC_URL" | gunzip > "$GENOME_FA"
  log "Downloaded: $(du -sh "$GENOME_FA" | cut -f1)"
else
  skip "$GENOME_FA"
fi

# ---------------------------------------------------------------------------
# 3. Index chr22 (fai + BWA)
# ---------------------------------------------------------------------------
if [[ ! -f "$GENOME_FA.fai" ]]; then
  log "Building .fai index..."
  "$BWA_ENV/bin/samtools" faidx "$GENOME_FA"
else
  skip "$GENOME_FA.fai"
fi

if [[ ! -f "$GENOME_FA.bwt" ]]; then
  log "Building BWA index (may take ~30s)..."
  "$BWA_ENV/bin/bwa" index "$GENOME_FA"
else
  skip "BWA index"
fi

# ---------------------------------------------------------------------------
# 4. Reads: download SRR1517830 to sources cache, subset to 100K
# ---------------------------------------------------------------------------
FULL_R1="$SOURCES_DIR/$ACCESSION/${ACCESSION}_1.fastq.gz"
FULL_R2="$SOURCES_DIR/$ACCESSION/${ACCESSION}_2.fastq.gz"

READS_DIR="$CORE_DIR/short_read/paired_end/exome"
SUBSET_R1="$READS_DIR/${ACCESSION}_R1_${SUBSET}.fastq.gz"
SUBSET_R2="$READS_DIR/${ACCESSION}_R2_${SUBSET}.fastq.gz"
LINES=$((NUM_READS * 4))

if [[ ! -f "$FULL_R1" ]]; then
  log "Downloading ${ACCESSION}_1.fastq.gz from EBI SRA..."
  curl -fL --retry 3 "$EBI_BASE/${ACCESSION}_1.fastq.gz" -o "$FULL_R1"
else
  skip "$FULL_R1"
fi

if [[ ! -f "$FULL_R2" ]]; then
  log "Downloading ${ACCESSION}_2.fastq.gz from EBI SRA..."
  curl -fL --retry 3 "$EBI_BASE/${ACCESSION}_2.fastq.gz" -o "$FULL_R2"
else
  skip "$FULL_R2"
fi

if [[ ! -f "$SUBSET_R1" ]]; then
  log "Subsetting R1 to ${SUBSET} reads..."
  { gunzip -c "$FULL_R1" || true; } | head -"$LINES" | gzip > "$SUBSET_R1"
else
  skip "$SUBSET_R1"
fi

if [[ ! -f "$SUBSET_R2" ]]; then
  log "Subsetting R2 to ${SUBSET} reads..."
  { gunzip -c "$FULL_R2" || true; } | head -"$LINES" | gzip > "$SUBSET_R2"
else
  skip "$SUBSET_R2"
fi

# Write sample metadata sidecar (model-driven via Python)
SAMPLE_META="$READS_DIR/${ACCESSION}_sample_meta.yaml"
if [[ ! -f "$SAMPLE_META" ]]; then
  log "Writing sample metadata sidecar..."
  python3 - <<PYEOF
import sys; sys.path.insert(0, "$PROJECT_ROOT")
from agent.models.core_data import SampleMeta, SubsetInfo
meta = SampleMeta(
    sample="$SAMPLE",
    accession="$ACCESSION",
    read_type="short_read",
    end_type="paired_end",
    assay_type="exome",
    sex="male",
    database="EBI_SRA",
    protocol="PCR-free",
    capture="whole_exome",
    read_length=150,
    source_urls={
        "r1": "$EBI_BASE/${ACCESSION}_1.fastq.gz",
        "r2": "$EBI_BASE/${ACCESSION}_2.fastq.gz",
    },
    subsets={
        "$SUBSET": SubsetInfo(
            r1="short_read/paired_end/exome/${ACCESSION}_R1_${SUBSET}.fastq.gz",
            r2="short_read/paired_end/exome/${ACCESSION}_R2_${SUBSET}.fastq.gz",
            num_reads=$NUM_READS,
            available=True,
        )
    },
)
meta.write("$SAMPLE_META")
print("[setup_core_test_data] Sample metadata written.")
PYEOF
else
  skip "$SAMPLE_META"
fi

# ---------------------------------------------------------------------------
# 5. BWA mem + samtools sort/index
# ---------------------------------------------------------------------------
BWA_OUT_DIR="$CORE_DIR/pipeline_outputs/bwa_samtools"
BAM="$BWA_OUT_DIR/${SAMPLE_KEY}_aligned_sorted.bam"
SAM="$BWA_OUT_DIR/${SAMPLE_KEY}_aligned.sam"
BWA_LOG="$BWA_OUT_DIR/${SAMPLE_KEY}_bwa_mem.log"

if [[ ! -f "$BAM" ]]; then
  log "Running bwa mem + samtools sort/index..."
  "$BWA_ENV/bin/bwa" mem -t 4 \
    -R "@RG\tID:${ACCESSION}\tSM:${SAMPLE}\tPL:ILLUMINA\tLB:exome" \
    "$GENOME_FA" "$SUBSET_R1" "$SUBSET_R2" \
    > "$SAM" 2> "$BWA_LOG"
  "$BWA_ENV/bin/samtools" sort -@ 4 -o "$BAM" "$SAM"
  "$BWA_ENV/bin/samtools" index "$BAM"
  log "Alignment complete: $(du -sh "$BAM" | cut -f1) BAM"
else
  skip "$BAM"
fi

# ---------------------------------------------------------------------------
# 6. FreeBayes variant calling
# ---------------------------------------------------------------------------
FB_OUT_DIR="$CORE_DIR/pipeline_outputs/freebayes"
VCF="$FB_OUT_DIR/${SAMPLE_KEY}_variants.vcf"
FB_LOG="$FB_OUT_DIR/${SAMPLE_KEY}_freebayes.log"

if [[ ! -f "$VCF" ]]; then
  log "Running freebayes..."
  "$FREEBAYES_ENV/bin/freebayes" \
    -f "$GENOME_FA" \
    --min-mapping-quality 20 \
    --min-base-quality 20 \
    --min-alternate-fraction 0.2 \
    --min-alternate-count 2 \
    "$BAM" \
    > "$VCF" \
    2> "$FB_LOG"
  VARIANT_COUNT=$(grep -v "^#" "$VCF" | wc -l | tr -d ' ')
  log "FreeBayes complete: $VARIANT_COUNT variant records"
else
  skip "$VCF"
fi

# ---------------------------------------------------------------------------
# 7. Write provenance files (model-validated via gen_provenance.py)
# ---------------------------------------------------------------------------
log "Generating provenance files..."

# Relative paths are from pipeline_outputs/{pipeline}/ to their targets
# That is 2 dirs up to reach core_test_data_hg38/, then navigate from there
# pipeline_spec is 4 dirs up to project root, then config/pipelines/

python3 scripts/gen_provenance.py \
  --pipeline bwa_samtools \
  --conda-env "$BWA_ENV" \
  --pipeline-spec "../../../../config/pipelines/bwa_samtools.yaml" \
  --genome-build "$GENOME_BUILD" \
  --chromosome "$CHROM" \
  --reference "../../genome/${CHROM}.fa" \
  --reference-fai "../../genome/${CHROM}.fa.fai" \
  --sample "$SAMPLE" \
  --accession "$ACCESSION" \
  --subset "$SUBSET" \
  --num-reads "$NUM_READS" \
  --assay-type exome \
  --end-type paired_end \
  --database EBI_SRA \
  --r1 "../../short_read/paired_end/exome/${ACCESSION}_R1_${SUBSET}.fastq.gz" \
  --r2 "../../short_read/paired_end/exome/${ACCESSION}_R2_${SUBSET}.fastq.gz" \
  --outputs "${SAMPLE_KEY}_aligned.sam:sam,${SAMPLE_KEY}_aligned_sorted.bam:bam:indexed,${SAMPLE_KEY}_aligned_sorted.bam.bai:bai" \
  --out "$BWA_OUT_DIR/${SAMPLE_KEY}_provenance.yaml"

python3 scripts/gen_provenance.py \
  --pipeline freebayes \
  --conda-env "$FREEBAYES_ENV" \
  --pipeline-spec "../../../../config/pipelines/freebayes_1.3.10.yaml" \
  --genome-build "$GENOME_BUILD" \
  --chromosome "$CHROM" \
  --reference "../../genome/${CHROM}.fa" \
  --reference-fai "../../genome/${CHROM}.fa.fai" \
  --bam "../bwa_samtools/${SAMPLE_KEY}_aligned_sorted.bam" \
  --bai "../bwa_samtools/${SAMPLE_KEY}_aligned_sorted.bam.bai" \
  --upstream-pipelines bwa_samtools \
  --parameters="--min-mapping-quality:20,--min-base-quality:20,--min-alternate-fraction:0.2,--min-alternate-count:2" \
  --outputs "${SAMPLE_KEY}_variants.vcf:vcf" \
  --out "$FB_OUT_DIR/${SAMPLE_KEY}_provenance.yaml"

log "Provenance files written."

# ---------------------------------------------------------------------------
# 8. Rebuild manifest (derived from provenance + disk state via gen_manifest.py)
# ---------------------------------------------------------------------------
log "Rebuilding manifest..."
python3 scripts/gen_manifest.py --core-dir "$CORE_DIR"
log "Manifest written."

log ""
log "Done. Core test data for $GENOME_BUILD / $SAMPLE_KEY at:"
log "  $CORE_DIR"
