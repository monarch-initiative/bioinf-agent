#!/usr/bin/env bash
# Bootstrap reference genomes and test datasets.
#
# Usage:
#   ./scripts/bootstrap_references.sh [options]
#
# Options:
#   --genome  hg38_full|hg38_chr22|ecoli_k12|all   (default: all)
#   --data    exome|smoke|all|none                  (default: all)
#   --help
#
# Examples:
#   ./scripts/bootstrap_references.sh
#   ./scripts/bootstrap_references.sh --genome hg38_full
#   ./scripts/bootstrap_references.sh --genome hg38_full --data exome
#   ./scripts/bootstrap_references.sh --genome hg38_chr22 --data smoke
#
# Notes:
#   - Full hg38 FASTA is ~3.2 GB compressed / ~26 GB decompressed.
#     Index building (BWA, STAR) takes additional time and space.
#   - The script is safe to re-run; already-present files are skipped.
#   - Requires: conda (samtools accessible), curl, python3, pyyaml

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_ROOT"

# Prefer conda's Python (has pyyaml) over the macOS system python3
_conda_python() {
    # Try CONDA_PREFIX, then common miniforge/miniconda/anaconda locations
    for candidate in \
        "${CONDA_PREFIX:-__none__}/bin/python3" \
        "$HOME/miniforge3/bin/python3" \
        "$HOME/miniconda3/bin/python3" \
        "$HOME/anaconda3/bin/python3" \
        "$(command -v python3 2>/dev/null)"
    do
        [[ -x "$candidate" ]] && "$candidate" -c "import yaml" 2>/dev/null && { echo "$candidate"; return; }
    done
    echo "python3"  # fallback — check_deps will catch the missing yaml
}
PYTHON="$(_conda_python)"

# ── defaults ──────────────────────────────────────────────────────────────────
GENOME_TARGET="all"
DATA_TARGET="all"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --genome) GENOME_TARGET="$2"; shift 2 ;;
        --data)   DATA_TARGET="$2";   shift 2 ;;
        --help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *) echo "Unknown argument: $1"; exit 1 ;;
    esac
done

# ── helpers ───────────────────────────────────────────────────────────────────
log()  { echo "[bootstrap] $*"; }
warn() { echo "[bootstrap] WARNING: $*" >&2; }
die()  { echo "[bootstrap] ERROR: $*" >&2; exit 1; }

check_deps() {
    local missing=()
    for dep in curl; do
        command -v "$dep" &>/dev/null || missing+=("$dep")
    done
    if ! "$PYTHON" -c "import yaml" 2>/dev/null; then
        missing+=("pyyaml (pip install pyyaml)")
    fi
    if [[ ${#missing[@]} -gt 0 ]]; then die "Missing required tools: ${missing[*]}"; fi
}

# Download with progress bar; skip if file already exists and is non-empty
download() {
    local url="$1" dest="$2"
    if [[ -f "$dest" && -s "$dest" ]]; then
        log "  $(basename "$dest") already present — skipping."
        return 0
    fi
    log "  Downloading $(basename "$dest") from $url"
    curl -fL --retry 3 --retry-delay 5 --progress-bar \
        -o "$dest" "$url" || die "Download failed: $url"
}

mark_available() {
    local manifest="$1" key="$2" id="$3"
    "$PYTHON" - <<EOF
import yaml, pathlib
p = pathlib.Path("$manifest")
data = yaml.safe_load(p.read_text()) or {}
for item in data.get("$key", []):
    if item.get("id") == "$id":
        item["available"] = True
p.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
print("[bootstrap]   Marked $id as available in manifest.")
EOF
}

samtools_available() {
    command -v samtools &>/dev/null
}

# ── Genome: hg38_full ─────────────────────────────────────────────────────────
bootstrap_hg38_full() {
    log "=== hg38_full: Full human genome (UCSC hg38) ==="
    local out_dir="$PROJECT_ROOT/data/genomes/hg38_full"
    mkdir -p "$out_dir/indexes"

    # FASTA
    local fasta="$out_dir/genome.fa"
    local fasta_gz="$out_dir/genome.fa.gz"

    if [[ -f "$fasta" && -s "$fasta" ]]; then
        log "  genome.fa already present — skipping download."
    else
        download \
            "https://hgdownload.soe.ucsc.edu/goldenPath/hg38/bigZips/hg38.fa.gz" \
            "$fasta_gz"
        log "  Decompressing hg38.fa.gz (~26 GB uncompressed — this takes a while)..."
        gzip -dc "$fasta_gz" > "$fasta"
        rm -f "$fasta_gz"
        log "  Decompression done."
    fi

    # samtools index
    if samtools_available; then
        if [[ ! -f "$fasta.fai" ]]; then
            log "  Building samtools faidx index..."
            samtools faidx "$fasta"
        fi
        if [[ ! -f "$out_dir/genome.dict" ]]; then
            log "  Building sequence dictionary..."
            samtools dict "$fasta" > "$out_dir/genome.dict"
        fi
    else
        warn "samtools not in PATH — skipping fai/dict. Install samtools and re-run."
    fi

    # GTF — GENCODE v45 comprehensive (filtered to primary chromosomes)
    local gtf="$out_dir/genes.gtf"
    if [[ ! -f "$gtf" || ! -s "$gtf" ]]; then
        log "  Downloading GENCODE v45 comprehensive annotation..."
        download \
            "https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_45/gencode.v45.annotation.gtf.gz" \
            "$out_dir/genes.gtf.gz"
        log "  Decompressing annotation GTF..."
        gzip -dc "$out_dir/genes.gtf.gz" > "$gtf"
        rm -f "$out_dir/genes.gtf.gz"
    fi

    mark_available \
        "$PROJECT_ROOT/data/genomes/manifest.yaml" \
        "genomes" "hg38_full"
    log "=== hg38_full ready. ==="
}

# ── Genome: hg38_chr22 (fast smoke-test genome) ───────────────────────────────
bootstrap_hg38_chr22() {
    log "=== hg38_chr22: chr22-only subset (smoke tests) ==="
    local out_dir="$PROJECT_ROOT/data/genomes/hg38_chr22"
    mkdir -p "$out_dir/indexes"

    local fasta="$out_dir/genome.fa"
    if [[ ! -f "$fasta" || ! -s "$fasta" ]]; then
        download \
            "https://hgdownload.soe.ucsc.edu/goldenPath/hg38/chromosomes/chr22.fa.gz" \
            "$out_dir/chr22.fa.gz"
        gzip -dc "$out_dir/chr22.fa.gz" > "$fasta"
        rm -f "$out_dir/chr22.fa.gz"
    fi

    if samtools_available; then
        [[ ! -f "$fasta.fai" ]] && samtools faidx "$fasta"
        [[ ! -f "$out_dir/genome.dict" ]] && samtools dict "$fasta" > "$out_dir/genome.dict"
    fi

    local gtf="$out_dir/genes.gtf"
    if [[ ! -f "$gtf" || ! -s "$gtf" ]]; then
        log "  Downloading GENCODE v45 and filtering to chr22..."
        download \
            "https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/release_45/gencode.v45.annotation.gtf.gz" \
            "$out_dir/full_annotation.gtf.gz"
        gzip -dc "$out_dir/full_annotation.gtf.gz" | awk '$1=="chr22"' > "$gtf"
        rm -f "$out_dir/full_annotation.gtf.gz"
    fi

    mark_available \
        "$PROJECT_ROOT/data/genomes/manifest.yaml" \
        "genomes" "hg38_chr22"
    log "=== hg38_chr22 ready. ==="
}

# ── Genome: E. coli K-12 ─────────────────────────────────────────────────────
bootstrap_ecoli_k12() {
    log "=== ecoli_k12: E. coli K-12 MG1655 ==="
    local out_dir="$PROJECT_ROOT/data/genomes/ecoli_k12"
    mkdir -p "$out_dir/indexes"

    local fasta="$out_dir/genome.fa"
    if [[ ! -f "$fasta" || ! -s "$fasta" ]]; then
        download \
            "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/005/845/GCF_000005845.2_ASM584v2/GCF_000005845.2_ASM584v2_genomic.fna.gz" \
            "$out_dir/ecoli.fa.gz"
        gzip -dc "$out_dir/ecoli.fa.gz" > "$fasta"
        rm -f "$out_dir/ecoli.fa.gz"
    fi

    if samtools_available; then
        [[ ! -f "$fasta.fai" ]] && samtools faidx "$fasta"
        [[ ! -f "$out_dir/genome.dict" ]] && samtools dict "$fasta" > "$out_dir/genome.dict"
    fi

    local gtf="$out_dir/genes.gtf"
    if [[ ! -f "$gtf" || ! -s "$gtf" ]]; then
        download \
            "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/005/845/GCF_000005845.2_ASM584v2/GCF_000005845.2_ASM584v2_genomic.gff.gz" \
            "$out_dir/genes.gff.gz"
        gzip -dc "$out_dir/genes.gff.gz" > "$gtf"
        rm -f "$out_dir/genes.gff.gz"
    fi

    mark_available \
        "$PROJECT_ROOT/data/genomes/manifest.yaml" \
        "genomes" "ecoli_k12"
    log "=== ecoli_k12 ready. ==="
}

# ── Test data: SRR1517830 (exome, HG00096, 1000 Genomes) ─────────────────────
download_exome_SRR1517830() {
    log "=== exome_SRR1517830: Exome HG00096 (SRR1517830) ==="
    local out_dir="$PROJECT_ROOT/data/test_data/exome_SRR1517830"
    mkdir -p "$out_dir"

    download \
        "ftp://ftp.sra.ebi.ac.uk/vol1/fastq/SRR151/000/SRR1517830/SRR1517830_1.fastq.gz" \
        "$out_dir/SRR1517830_1.fastq.gz"

    download \
        "ftp://ftp.sra.ebi.ac.uk/vol1/fastq/SRR151/000/SRR1517830/SRR1517830_2.fastq.gz" \
        "$out_dir/SRR1517830_2.fastq.gz"

    # Quick sanity check — first line of R1 must start with @
    log "  Verifying R1 integrity..."
    local first_line
    first_line=$(gzip -dc "$out_dir/SRR1517830_1.fastq.gz" 2>/dev/null | head -1 || true)
    if [[ "$first_line" == @* ]]; then
        log "  R1 FASTQ header OK."
    else
        warn "R1 header check failed — file may be incomplete."
    fi

    mark_available \
        "$PROJECT_ROOT/data/test_data/manifest.yaml" \
        "datasets" "exome_SRR1517830"
    log "=== SRR1517830 ready. ==="
}

# ── Smoke-test datasets (synthetic, generated from chr22) ─────────────────────
generate_smoke_datasets() {
    log "=== Generating synthetic smoke-test datasets from chr22 ==="
    if ! samtools_available; then
        warn "samtools not in PATH — cannot run wgsim to generate synthetic reads. Skipping."
        return 0
    fi

    "$PYTHON" - <<'EOF'
import yaml, sys
sys.path.insert(0, ".")
from agent.skills.test_runner import TestRunner

config = yaml.safe_load(open("config/agent_config.yaml"))
runner = TestRunner(config)

smoke_datasets = [
    "rnaseq_small_paired_human",
    "wgs_small_paired_human",
    "chipseq_small_human",
    "wgs_ecoli_small",
]

for ds_id in smoke_datasets:
    print(f"[bootstrap]   Generating {ds_id}...")
    result = runner.download_resource("test_data", ds_id)
    if result.get("success"):
        print(f"[bootstrap]   {ds_id} OK: {result}")
    else:
        print(f"[bootstrap]   WARNING: {ds_id} failed: {result.get('error', result)}", flush=True)
EOF
}

# ── Main ──────────────────────────────────────────────────────────────────────
check_deps

case "$GENOME_TARGET" in
    all)
        bootstrap_hg38_full
        bootstrap_hg38_chr22
        bootstrap_ecoli_k12
        ;;
    hg38_full)   bootstrap_hg38_full ;;
    hg38_chr22)  bootstrap_hg38_chr22 ;;
    ecoli_k12)   bootstrap_ecoli_k12 ;;
    none) log "Skipping genome downloads." ;;
    *) die "Unknown --genome value: $GENOME_TARGET" ;;
esac

case "$DATA_TARGET" in
    all)
        download_exome_SRR1517830
        generate_smoke_datasets
        ;;
    exome)  download_exome_SRR1517830 ;;
    smoke)  generate_smoke_datasets ;;
    none)   log "Skipping test data." ;;
    *) die "Unknown --data value: $DATA_TARGET" ;;
esac

log "Bootstrap complete."
