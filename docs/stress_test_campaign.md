# Stress-test campaign — plan (NOT YET RUN)

Purpose: validate that the system's features actually work end-to-end on **real,
diverse bioinformatics tools**, shake out latent bugs on the dark paths, and
raise real terminal coverage as a byproduct. This is the "prove it works before
we harden" pass — hardening tests written afterward are anchored to real runs,
not to unvalidated code.

**Why this is safe to run un-hardened:** the honesty contract is the net.
`freeze` refuses on any `check_build` violation and `seal_workflow` refuses on
any I0/I3/I6/I7/I8 violation — a broken install physically cannot produce a
false-green artifact. Worst case a run breaks loudly and refuses, which is
exactly the signal we want.

**The instrument:** after each block, re-run
`python scripts/measure_terminal_coverage.py` and watch the dashboard's dark
count fall for the targeted subsystems. Every `broke`/`degraded` that fires on a
real run is a precise bug/ergonomics report.

---

## Two arenas — do NOT conflate

Roughly half the dark **failure** paths live in the HPC bridge and can only be
honestly stressed on a real cluster:

| Arena | Who runs it | Subsystems it can light up |
|-------|-------------|----------------------------|
| **LOCAL** (Docker on this machine) | the agent, solo | env_manager (install tiers) · container_build · env_build · **validate** · freeze · core_test_data · service |
| **CLUSTER** (real HPC) | **USER-DRIVEN ONLY** (no cheeky head-node testing) | transfer (36 dark-fail) · stage · cluster · run_cluster · submit · modules |

Blocks A–C are LOCAL. Block D is CLUSTER and only runs when the user drives it
from their own session. A local campaign cannot move the transfer/cluster dark
count — that is expected, not a gap in the plan.

---

## Block A — install-tier matrix (Layer 1: install + freeze every tier)

One representative tool per tier, so every install primitive AND every `freeze`
routing branch fires at least once. Each row ends in a `freeze()`.

| # | Tool | Tier / primitive | Freeze routing exercised | Dark subsystem hit |
|---|------|------------------|--------------------------|--------------------|
| A1 | **samtools** | conda (pure) | **ADOPT** biocontainer by digest (no build) | freeze adopt path |
| A2 | **samtools + bcftools + bwa** | conda (multi) | ADOPT-or-BUILD decision on a multi-pkg env | container_build/env_build |
| A3 | **pysam** (or cutadapt) | pip / PyPI | CONTAINER-NATIVE BUILD, pip in-lock (`--pypi`) | env_manager.pip, build |
| A4 | **DESeq2** | R / Bioconductor | build via Rscript (heavy dep tree — real stress) | env_manager (R install), build |
| A5 | **Picard** | jar (Java) | build + JRE + `java -jar` wrapper | env_manager.jar (5 dark) |
| A6 | **seqtk** | source (git + make) | build: clone@commit + rebuild (source replay) | env_manager.git (8 dark) |
| A7 | **mosdepth** | binary (release) | build: linux asset re-fetch + sha256 anchor | env_manager.binary (5 dark) |
| A8 | *(stretch)* a VEP plugin | perl (cpanm XS) | build: cpanm replay, XS compile vs conda perl | env_manager.perl (3 dark) |
| A9 | *(stretch)* a Rust or Go bio tool | cargo / go | build: toolchain replay | env_manager.cargo/go (6 dark) |

A8/A9 are the exotic tiers — do them only after A1–A7 are clean, since they're
the most likely to surface real friction.

## Block B — real workflows (Layer 2: seal → validate.* coverage)

`validate.*` is the biggest single dark cluster (56 dark across nearly every
filetype). The only way to light it is to run real pipelines that PRODUCE those
files, in the frozen image (`run_step_in_container`), then `seal_workflow`. Uses
the existing core test data (chr22 + exome/rnaseq/wgs/wgbs reads + ACTB
phenopacket + pod5 fixture).

| # | Workflow | Steps | validate.* filetypes covered |
|---|----------|-------|------------------------------|
| B1 | **Align → sort → index** | bwa/minimap2 (chr22 + exome reads) → samtools sort → index | fastq, sam, bam, bai |
| B2 | **Variant calling** | bcftools mpileup → call on B1's BAM | vcf |
| B3 | **Coverage** | mosdepth on B1's BAM | bed, txt/summary, bigwig |
| B4 | **QC** | fastqc on the read sets | html, (zip) |
| B5 | **Phenopacket → VCF → annotate** | `phenopacket_to_vcf` (ACTB) → snpEff/VEP annotate | vcf, json |
| B6 | **RNA-seq quant → DE** | salmon/kallisto (rnaseq reads) → DESeq2 (A4) | counts_matrix, tabular/tsv |
| B7 | **Basecall + mods** | dorado on the pod5 fixture (R10.4.1) | bam (methylation/base-mod) |

B1→B2→B3 chain off one alignment (also exercises I8 lineage: same BAM consumed
downstream). B6 reuses the A4 DESeq2 env (multi-env workflow chaining).

## Block C — freeze-policy edge scenarios (the procedural firewalls)

| # | Scenario | Invariant exercised |
|---|----------|---------------------|
| C1 | a **GPU** tool (dorado, or a CUDA tool) declares `toolkit_version` | I12 accelerator honesty |
| C2 | a **gated/licensed** tool → must set `redistributable:false` + `licenses[]`, tarball-only, never pushed | I13 license firewall |
| C3 | a **service-dependent** tool (needs Redis/Postgres/web) via `start_service` | I10 + env_manager.service (8 dark) |

C2 needs an actual gated artifact; treat as optional/last. C3 is the cheapest
way to clear the 8 dark `service.*` terminals.

## Block D — HPC bridge (USER-DRIVEN, real cluster only)

Runs ONLY when the user drives it from their own ssh session. Lights up the
transfer/cluster half of the dark-fail list.

| # | Scenario | Chain |
|---|----------|-------|
| D1 | validate-on-cluster | `freeze` → `run_step_on_cluster` (scratch sandbox) → `seal_workflow` (cluster locus) |
| D2 | production submit | `stage_apptainer_image` → `upload` → `submit_workflow_job` → `cluster_job_status` → `download` |
| D3 | transport coverage | run D1/D2 once over `scp_head_node` and once over `globus` |

---

## Execution order & acceptance

1. **A1** first (smallest, pure-conda adopt) — proves the happy path end to end.
2. **A2–A7** — one per tier; fix whatever breaks before moving on.
3. **B1–B4** — the core alignment/variant/coverage/QC workflows (biggest
   validate.* lift).
4. **A8/A9, B5–B7, C1–C3** — the long tail and firewalls.
5. **D1–D3** — only when the user is driving the cluster.

**After each block:** `python scripts/measure_terminal_coverage.py`, note the
dark-count drop for the targeted subsystems, and log every `broke`/`degraded`
that fired (the real bug/ergonomics list). THEN harden — write tests for what
actually broke + the now-exercised hot paths, anchored to these real runs.

**Acceptance for "features work":** A1–A7 each produce a frozen, honest artifact
(or refuse loudly for a real reason); B1–B4 each seal a validated workflow. The
dark count for env_manager / container_build / env_build / validate / freeze
falls materially. Bugs found are logged, not silently worked around
(no-fallback rule).

> Status: **PLANNED, not run.** Get everything in place + pushed first, then run
> Block A1.
