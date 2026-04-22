#!/usr/bin/env bash
# setup_core_test_data.sh — Bootstrap core test data for a genome build.
#
# Usage:
#   ./scripts/setup_core_test_data.sh [options]
#
# Options:
#   --genome-build BUILD    Genome build (default: hg38)
#   --accession ACC         SRA accession (default: SRR1517830)
#   --sample SAMPLE         Sample ID (default: HG00096; omit to use accession)
#   --assay-type TYPE       exome|wgs|rnaseq|chipseq|atacseq|amplicon (default: exome)
#   --end-type TYPE         paired_end|single_end (default: paired_end)
#   --subset SIZE           10K|50K|100K|500K|1M (default: 100K)
#   --no-pipeline           Skip alignment + variant-calling pipeline steps
#
# Output layout:
#   data/core_test_data_{build}/
#     genome/                              {chrom}.fa + indexes
#     short_read/{end_type}/{assay_type}/  {file_key}_R1.fastq.gz
#                                          {sample_key}_sample_meta.yaml
#     pipeline_outputs/bwa_samtools/       {file_key}_aligned_sorted.bam  (+ .bai)
#     pipeline_outputs/freebayes/          {file_key}_variants.vcf
#     manifest.yaml
#
# Defaults produce the canonical hg38/chr22/exome/SRR1517830 dataset.
# Raw source reads are cached in data/sources/{accession}/ to avoid re-downloading.
# Idempotent: skips any step whose output files already exist.
# Conda envs bioinf_bwa_samtools and bioinf_freebayes are auto-installed if missing.

set -euo pipefail

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
GENOME_BUILD="hg38"
ACCESSION="SRR1517830"
SAMPLE="HG00096"
ASSAY_TYPE="exome"
END_TYPE="paired_end"
SUBSET="100K"
RUN_PIPELINE=true

# ---------------------------------------------------------------------------
# Parse arguments
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --genome-build) GENOME_BUILD="$2"; shift 2 ;;
    --accession)    ACCESSION="$2";    shift 2 ;;
    --sample)       SAMPLE="$2";       shift 2 ;;
    --assay-type)   ASSAY_TYPE="$2";   shift 2 ;;
    --end-type)     END_TYPE="$2";     shift 2 ;;
    --subset)       SUBSET="$2";       shift 2 ;;
    --no-pipeline)  RUN_PIPELINE=false; shift ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

# If sample not set via arg and equals the default "HG00096" but accession changed, use accession
if [[ "$SAMPLE" == "HG00096" && "$ACCESSION" != "SRR1517830" ]]; then
  SAMPLE="$ACCESSION"
fi

# ---------------------------------------------------------------------------
# Derived variables
# ---------------------------------------------------------------------------
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATA_DIR="$PROJECT_ROOT/data"
CORE_DIR="$DATA_DIR/core_test_data_${GENOME_BUILD}"
SOURCES_DIR="$DATA_DIR/sources"

BWA_ENV="$PROJECT_ROOT/envs/bioinf_bwa_samtools"
FREEBAYES_ENV="$PROJECT_ROOT/envs/bioinf_freebayes"

SAMPLE_KEY="${SAMPLE}_${ACCESSION}"
FILE_KEY="${SAMPLE_KEY}_${SUBSET}"

# Parse subset size
case "$SUBSET" in
  10K)  NUM_READS=10000   ;;
  50K)  NUM_READS=50000   ;;
  100K) NUM_READS=100000  ;;
  500K) NUM_READS=500000  ;;
  1M)   NUM_READS=1000000 ;;
  *)    echo "ERROR: unknown subset '$SUBSET'. Use 10K, 50K, 100K, 500K, or 1M." >&2; exit 1 ;;
esac

EBI_BASE="https://ftp.sra.ebi.ac.uk/vol1/fastq/${ACCESSION:0:6}/00${ACCESSION: -1}/${ACCESSION}"

log()  { echo "[setup_core_test_data] $*"; }
skip() { echo "[setup_core_test_data] SKIP (exists): $*"; }

if [[ "$GENOME_BUILD" != "hg38" ]]; then
  echo "ERROR: only hg38 is currently supported for genome download." >&2
  exit 1
fi

CONDA_EXE=$(which conda 2>/dev/null || echo "")
if [[ -z "$CONDA_EXE" ]]; then
  echo "ERROR: conda not found in PATH. Activate your conda base environment first." >&2
  exit 1
fi

# ---------------------------------------------------------------------------
# 0. Auto-install conda envs if missing (only when pipeline steps will run)
# ---------------------------------------------------------------------------
if [[ "$RUN_PIPELINE" == "true" ]]; then
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
fi

# ---------------------------------------------------------------------------
# 1. Create directory tree
# ---------------------------------------------------------------------------
log "Creating directory structure for $GENOME_BUILD / $SAMPLE_KEY..."
mkdir -p \
  "$CORE_DIR/genome" \
  "$CORE_DIR/short_read/${END_TYPE}/${ASSAY_TYPE}" \
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

if [[ "$RUN_PIPELINE" == "true" && ! -f "$GENOME_FA.bwt" ]]; then
  log "Building BWA index (may take ~30s)..."
  "$BWA_ENV/bin/bwa" index "$GENOME_FA"
elif [[ -f "$GENOME_FA.bwt" ]]; then
  skip "BWA index"
fi

# ---------------------------------------------------------------------------
# 4. Reads: download to sources cache, subset to target count
# ---------------------------------------------------------------------------
FULL_R1="$SOURCES_DIR/$ACCESSION/${ACCESSION}_1.fastq.gz"
FULL_R2="$SOURCES_DIR/$ACCESSION/${ACCESSION}_2.fastq.gz"

READS_DIR="$CORE_DIR/short_read/${END_TYPE}/${ASSAY_TYPE}"
SUBSET_R1="$READS_DIR/${FILE_KEY}_R1.fastq.gz"
SUBSET_R2="$READS_DIR/${FILE_KEY}_R2.fastq.gz"
LINES=$((NUM_READS * 4))

if [[ "$END_TYPE" == "paired_end" ]]; then
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
else  # single_end
  FULL_R1="$SOURCES_DIR/$ACCESSION/${ACCESSION}.fastq.gz"
  SUBSET_R1="$READS_DIR/${FILE_KEY}_R1.fastq.gz"
  if [[ ! -f "$FULL_R1" ]]; then
    log "Downloading ${ACCESSION}.fastq.gz from EBI SRA..."
    curl -fL --retry 3 "$EBI_BASE/${ACCESSION}.fastq.gz" -o "$FULL_R1"
  fi
  if [[ ! -f "$SUBSET_R1" ]]; then
    log "Subsetting to ${SUBSET} reads..."
    { gunzip -c "$FULL_R1" || true; } | head -"$LINES" | gzip > "$SUBSET_R1"
  fi
fi

# Measure read length from the subset FASTQ (line 2 of record 1)
READ_LENGTH=$(gunzip -c "$SUBSET_R1" 2>/dev/null | awk 'NR==2{print length; exit}')
log "Measured read length: ${READ_LENGTH}bp"

# Write sample metadata sidecar (model-validated via Python)
SAMPLE_META="$READS_DIR/${SAMPLE_KEY}_sample_meta.yaml"
if [[ ! -f "$SAMPLE_META" ]]; then
  log "Writing sample metadata sidecar..."
  python3 - <<PYEOF
import sys; sys.path.insert(0, "$PROJECT_ROOT")
from agent.models.core_data import SampleMeta, SubsetInfo
read_length = int("$READ_LENGTH") if "$READ_LENGTH".strip() else None
meta = SampleMeta(
    sample="$SAMPLE",
    accession="$ACCESSION",
    read_type="short_read",
    end_type="$END_TYPE",
    assay_type="$ASSAY_TYPE",
    database="EBI_SRA",
    read_length=read_length,
    source_urls={
        "r1": "$EBI_BASE/${ACCESSION}_1.fastq.gz",
        "r2": "$EBI_BASE/${ACCESSION}_2.fastq.gz",
    },
    subsets={
        "$SUBSET": SubsetInfo(
            r1="short_read/${END_TYPE}/${ASSAY_TYPE}/${FILE_KEY}_R1.fastq.gz",
            r2=$( [[ "$END_TYPE" == "paired_end" ]] && echo "\"short_read/${END_TYPE}/${ASSAY_TYPE}/${FILE_KEY}_R2.fastq.gz\"" || echo "None" ),
            num_reads=$NUM_READS,
            available=True,
        )
    },
)
meta.write("$SAMPLE_META")
print("[setup_core_test_data] Sample metadata written: $SAMPLE_META")
PYEOF
else
  skip "$SAMPLE_META"
fi

# ---------------------------------------------------------------------------
# 5–7. Pipeline steps (only for paired_end exome/wgs; skip with --no-pipeline)
# ---------------------------------------------------------------------------
if [[ "$RUN_PIPELINE" == "false" ]]; then
  log "Skipping pipeline steps (--no-pipeline)."
else

# ---------------------------------------------------------------------------
# 5. BWA mem + samtools sort/index
# ---------------------------------------------------------------------------
BWA_OUT_DIR="$CORE_DIR/pipeline_outputs/bwa_samtools"
BAM="$BWA_OUT_DIR/${FILE_KEY}_aligned_sorted.bam"
SAM="$BWA_OUT_DIR/${FILE_KEY}_aligned.sam"
BWA_LOG="$BWA_OUT_DIR/${FILE_KEY}_bwa_mem.log"

if [[ ! -f "$BAM" ]]; then
  log "Running bwa mem + samtools sort/index..."
  "$BWA_ENV/bin/bwa" mem -t 4 \
    -R "@RG\tID:${ACCESSION}\tSM:${SAMPLE}\tPL:ILLUMINA\tLB:${ASSAY_TYPE}" \
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
VCF="$FB_OUT_DIR/${FILE_KEY}_variants.vcf"
FB_LOG="$FB_OUT_DIR/${FILE_KEY}_freebayes.log"

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
  --assay-type "$ASSAY_TYPE" \
  --end-type "$END_TYPE" \
  --database EBI_SRA \
  --r1 "../../short_read/${END_TYPE}/${ASSAY_TYPE}/${FILE_KEY}_R1.fastq.gz" \
  --r2 "../../short_read/${END_TYPE}/${ASSAY_TYPE}/${FILE_KEY}_R2.fastq.gz" \
  --outputs "${FILE_KEY}_aligned.sam:sam,${FILE_KEY}_aligned_sorted.bam:bam:indexed,${FILE_KEY}_aligned_sorted.bam.bai:bai" \
  --out "$BWA_OUT_DIR/${FILE_KEY}_provenance.yaml"

python3 scripts/gen_provenance.py \
  --pipeline freebayes \
  --conda-env "$FREEBAYES_ENV" \
  --pipeline-spec "../../../../config/pipelines/freebayes_1.3.10.yaml" \
  --genome-build "$GENOME_BUILD" \
  --chromosome "$CHROM" \
  --reference "../../genome/${CHROM}.fa" \
  --reference-fai "../../genome/${CHROM}.fa.fai" \
  --bam "../bwa_samtools/${FILE_KEY}_aligned_sorted.bam" \
  --bai "../bwa_samtools/${FILE_KEY}_aligned_sorted.bam.bai" \
  --upstream-pipelines bwa_samtools \
  --parameters="--min-mapping-quality:20,--min-base-quality:20,--min-alternate-fraction:0.2,--min-alternate-count:2" \
  --outputs "${FILE_KEY}_variants.vcf:vcf" \
  --out "$FB_OUT_DIR/${FILE_KEY}_provenance.yaml"

log "Provenance files written."

fi  # end RUN_PIPELINE

# ---------------------------------------------------------------------------
# 8. Rebuild manifest (derived from provenance + disk state via gen_manifest.py)
# ---------------------------------------------------------------------------
log "Rebuilding manifest..."
python3 scripts/gen_manifest.py --core-dir "$CORE_DIR"
log "Manifest written."

log ""
log "Done. Core test data for $GENOME_BUILD / $SAMPLE_KEY at:"
log "  $CORE_DIR"
