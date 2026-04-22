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
#     pipeline_outputs/bwa_samtools/{sample}_{accession}/
#     pipeline_outputs/freebayes/{sample}_{accession}/
#     manifest.yaml
#
# Raw source reads are cached in data/sources/{accession}/ to avoid re-downloading.
# Idempotent: skips any step whose output files already exist.
# Requires: conda envs bioinf_bwa_samtools and bioinf_freebayes already installed.

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

if [[ ! -d "$BWA_ENV" ]]; then
  echo "ERROR: conda env bioinf_bwa_samtools not found at $BWA_ENV" >&2
  echo "       Run: python -m agent.main --once 'install latest bwa and samtools'" >&2
  exit 1
fi

if [[ ! -d "$FREEBAYES_ENV" ]]; then
  echo "ERROR: conda env bioinf_freebayes not found at $FREEBAYES_ENV" >&2
  echo "       Run: python -m agent.main --once 'install latest freebayes'" >&2
  exit 1
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
  "$CORE_DIR/pipeline_outputs/bwa_samtools/$SAMPLE_KEY" \
  "$CORE_DIR/pipeline_outputs/freebayes/$SAMPLE_KEY" \
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
SUBSET_R1="$CORE_DIR/short_read/paired_end/exome/${ACCESSION}_R1_${SUBSET}.fastq.gz"
SUBSET_R2="$CORE_DIR/short_read/paired_end/exome/${ACCESSION}_R2_${SUBSET}.fastq.gz"
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
  gunzip -c "$FULL_R1" | head -"$LINES" | gzip > "$SUBSET_R1"
else
  skip "$SUBSET_R1"
fi

if [[ ! -f "$SUBSET_R2" ]]; then
  log "Subsetting R2 to ${SUBSET} reads..."
  gunzip -c "$FULL_R2" | head -"$LINES" | gzip > "$SUBSET_R2"
else
  skip "$SUBSET_R2"
fi

# ---------------------------------------------------------------------------
# 5. BWA mem + samtools sort/index
# ---------------------------------------------------------------------------
BWA_OUT_DIR="$CORE_DIR/pipeline_outputs/bwa_samtools/$SAMPLE_KEY"
BAM="$BWA_OUT_DIR/aligned_sorted.bam"
SAM="$BWA_OUT_DIR/aligned.sam"

if [[ ! -f "$BAM" ]]; then
  log "Running bwa mem + samtools sort/index..."
  "$BWA_ENV/bin/bwa" mem -t 4 \
    -R "@RG\tID:${ACCESSION}\tSM:${SAMPLE}\tPL:ILLUMINA\tLB:exome" \
    "$GENOME_FA" "$SUBSET_R1" "$SUBSET_R2" \
    > "$SAM" 2> "$BWA_OUT_DIR/bwa_mem.log"
  "$BWA_ENV/bin/samtools" sort -@ 4 -o "$BAM" "$SAM"
  "$BWA_ENV/bin/samtools" index "$BAM"
  log "Alignment complete: $(du -sh "$BAM" | cut -f1) BAM"
else
  skip "$BAM"
fi

# ---------------------------------------------------------------------------
# 6. FreeBayes variant calling
# ---------------------------------------------------------------------------
FB_OUT_DIR="$CORE_DIR/pipeline_outputs/freebayes/$SAMPLE_KEY"
VCF="$FB_OUT_DIR/variants.vcf"

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
    2> "$FB_OUT_DIR/freebayes.log"
  VARIANT_COUNT=$(grep -v "^#" "$VCF" | wc -l | tr -d ' ')
  log "FreeBayes complete: $VARIANT_COUNT variant records"
else
  skip "$VCF"
  VARIANT_COUNT=$(grep -v "^#" "$VCF" | wc -l | tr -d ' ')
fi

# ---------------------------------------------------------------------------
# 7. Write provenance files
# ---------------------------------------------------------------------------
BWA_VER=$("$BWA_ENV/bin/bwa" 2>&1 | grep "Version:" | awk '{print $2}' || echo "unknown")
SAMTOOLS_VER=$("$BWA_ENV/bin/samtools" --version | head -1 | awk '{print $2}')
FB_VER=$("$FREEBAYES_ENV/bin/freebayes" --version 2>&1 | grep "version:" | awk '{print $2}' || echo "1.3.10")
TODAY=$(date +%Y-%m-%d)

cat > "$BWA_OUT_DIR/provenance.yaml" <<PROVEOF
pipeline: bwa_samtools
pipeline_spec: ../../../../../config/pipelines/bwa_samtools.yaml
conda_env: bioinf_bwa_samtools
created_at: '$TODAY'

tool_versions:
  bwa: $BWA_VER
  samtools: $SAMTOOLS_VER

inputs:
  genome_build: $GENOME_BUILD
  chromosome_subset: $CHROM
  reference: ../../../genome/${CHROM}.fa
  reference_fai: ../../../genome/${CHROM}.fa.fai
  reads:
    - read_type: short_read
      end_type: paired_end
      assay_type: exome
      subset: $SUBSET
      num_reads: $NUM_READS
      r1: ../../../short_read/paired_end/exome/${ACCESSION}_R1_${SUBSET}.fastq.gz
      r2: ../../../short_read/paired_end/exome/${ACCESSION}_R2_${SUBSET}.fastq.gz
      sample: $SAMPLE
      accession: $ACCESSION
      database: EBI_SRA

upstream_pipelines: []

outputs:
  - file: aligned.sam
    type: sam
  - file: aligned_sorted.bam
    type: bam
    indexed: true
  - file: aligned_sorted.bam.bai
    type: bai
PROVEOF

cat > "$FB_OUT_DIR/provenance.yaml" <<PROVEOF
pipeline: freebayes
pipeline_spec: ../../../../../config/pipelines/freebayes_1.3.10.yaml
conda_env: bioinf_freebayes
created_at: '$TODAY'

tool_versions:
  freebayes: $FB_VER

inputs:
  genome_build: $GENOME_BUILD
  chromosome_subset: $CHROM
  reference: ../../../genome/${CHROM}.fa
  reference_fai: ../../../genome/${CHROM}.fa.fai
  bam: ../../bwa_samtools/${SAMPLE_KEY}/aligned_sorted.bam
  bai: ../../bwa_samtools/${SAMPLE_KEY}/aligned_sorted.bam.bai

upstream_pipelines:
  - bwa_samtools

parameters:
  --min-mapping-quality: 20
  --min-base-quality: 20
  --min-alternate-fraction: 0.2
  --min-alternate-count: 2

outputs:
  - file: variants.vcf
    type: vcf
PROVEOF

log "Provenance files written."

# ---------------------------------------------------------------------------
# 8. Write/update core manifest
# ---------------------------------------------------------------------------
SAM_SIZE=$(stat -f%z "$SAM" 2>/dev/null || stat -c%s "$SAM")
BAM_SIZE=$(stat -f%z "$BAM" 2>/dev/null || stat -c%s "$BAM")
VCF_SIZE=$(stat -f%z "$VCF" 2>/dev/null || stat -c%s "$VCF")

cat > "$CORE_DIR/manifest.yaml" <<MANEOF
# Core test dataset — $GENOME_BUILD, $CHROM subset
# Generated by scripts/setup_core_test_data.sh on $TODAY
#
# Controlled vocabulary:
#   read_type:  short_read | long_read
#   end_type:   paired_end | single_end | mate_pair
#   assay_type: exome | wgs | rnaseq | chipseq | atacseq | hic | amplicon
#   file_type:  fastq | bam | sam | bai | vcf | bed | bigwig | pod5 | fast5

genome_build: $GENOME_BUILD
species: homo_sapiens
chromosome_subset: $CHROM
generated_at: '$TODAY'

genome:
  fasta: genome/${CHROM}.fa
  fai: genome/${CHROM}.fa.fai
  chromosome: $CHROM
  source_url: https://hgdownload.soe.ucsc.edu/goldenPath/${GENOME_BUILD}/chromosomes/${CHROM}.fa.gz
  indexes:
    bwa:
      - genome/${CHROM}.fa.amb
      - genome/${CHROM}.fa.ann
      - genome/${CHROM}.fa.bwt
      - genome/${CHROM}.fa.pac
      - genome/${CHROM}.fa.sa

sequencing_data:
  short_read:
    paired_end:
      exome:
        - sample: $SAMPLE
          accession: $ACCESSION
          sex: male
          database: EBI_SRA
          protocol: PCR-free
          capture: whole_exome
          read_length: 150
          source_urls:
            r1: ${EBI_BASE}/${ACCESSION}_1.fastq.gz
            r2: ${EBI_BASE}/${ACCESSION}_2.fastq.gz
          subsets:
            ${SUBSET}:
              r1: short_read/paired_end/exome/${ACCESSION}_R1_${SUBSET}.fastq.gz
              r2: short_read/paired_end/exome/${ACCESSION}_R2_${SUBSET}.fastq.gz
              num_reads: $NUM_READS
              available: true
    single_end: {}
    mate_pair: {}
  long_read:
    single_end: {}

pipeline_outputs:
  bwa_samtools:
    available: true
    samples:
      ${SAMPLE_KEY}:
        provenance: pipeline_outputs/bwa_samtools/${SAMPLE_KEY}/provenance.yaml
        files:
          - path: pipeline_outputs/bwa_samtools/${SAMPLE_KEY}/aligned.sam
            type: sam
            size_bytes: $SAM_SIZE
          - path: pipeline_outputs/bwa_samtools/${SAMPLE_KEY}/aligned_sorted.bam
            type: bam
            size_bytes: $BAM_SIZE
          - path: pipeline_outputs/bwa_samtools/${SAMPLE_KEY}/aligned_sorted.bam.bai
            type: bai
  freebayes:
    available: true
    upstream_pipelines:
      - bwa_samtools
    samples:
      ${SAMPLE_KEY}:
        provenance: pipeline_outputs/freebayes/${SAMPLE_KEY}/provenance.yaml
        files:
          - path: pipeline_outputs/freebayes/${SAMPLE_KEY}/variants.vcf
            type: vcf
            size_bytes: $VCF_SIZE
            variant_records: $VARIANT_COUNT
MANEOF

log "Core manifest written."
log ""
log "Done. Core test data for $GENOME_BUILD / $SAMPLE_KEY at:"
log "  $CORE_DIR"
