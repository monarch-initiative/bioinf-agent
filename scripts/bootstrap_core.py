#!/usr/bin/env python3
"""
bootstrap_core.py — One-time setup for the bioinf-agent.

Dogfoods the agent's own tooling: the core_tools env is built via the same
PipelineState + env_manager path that user pipelines use.

Steps:
  1. Install the core_tools conda env (samtools + bcftools + seqkit + bwa)
     via start_pipeline → search_package → install_packages →
     verify_installation (host env; no spec is written — the combined env-spec
     writer was retired with the host build path)
  2. Download the reference chromosome FASTA and build fai + bwa indexes
     using the env we just installed
  3. Download the canonical test datasets listed in config/core_datasets.yaml
  4. Smoke test: align one read pair with bwa, sort + index with samtools,
     validate the BAM with OutputValidator

Usage:
  python scripts/bootstrap_core.py [--genome-build hg38] [--skip-smoke]
"""

from __future__ import annotations

import argparse
import gzip
import shutil
import sys
import urllib.request
from pathlib import Path
from typing import Any

import yaml

PROJECT_ROOT = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_ROOT))

from agent.skills.core_test_data import (
    add_core_pod5_data,
    add_core_test_data,
    add_phenopacket,
)
from agent.skills.env_manager import EnvManager
from agent.skills.package_search import PackageSearch
from agent.skills.pipeline_state import PipelineState
from agent.validators.output_validator import OutputValidator


# Packages installed in the core_tools env. Each entry: (spec, channel, check_cmd).
CORE_TOOLS_PACKAGES = [
    ("samtools",   "bioconda",    "samtools --version | head -1"),
    ("bcftools",   "bioconda",    "bcftools --version | head -1"),
    ("seqkit",     "bioconda",    "seqkit version"),
    ("bwa",        "bioconda",    "bwa 2>&1 | head -3"),
    ("conda-pack", "conda-forge", "conda-pack --version"),
]


GENOME_BUILDS = {
    "hg38":  {
        "url":   "https://hgdownload.soe.ucsc.edu/goldenPath/hg38/chromosomes/chr22.fa.gz",
        "name":  "chr22.fa",
        "chrom": "chr22",
    },
    "mm10":  {
        "url":   "https://hgdownload.soe.ucsc.edu/goldenPath/mm10/chromosomes/chr19.fa.gz",
        "name":  "chr19.fa",
        "chrom": "chr19",
    },
    "ecoli": {
        "url":   "https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/005/845/"
                 "GCF_000005845.2_ASM584v2/GCF_000005845.2_ASM584v2_genomic.fna.gz",
        "name":  "genome.fa",
        "chrom": "all",
    },
}


# Recognised kwargs for add_core_test_data — any other YAML field is ignored
# (e.g. our `description` field is for humans, not the function).
_ADD_DATA_KWARGS = {
    "accession", "assay_type", "end_type", "genome_build", "sample",
    "subset", "platform", "source_url", "source_url_r2",
}


def log(msg: str) -> None:
    print(f"[bootstrap] {msg}", flush=True)


def load_config() -> dict:
    with open(PROJECT_ROOT / "config" / "agent_config.yaml") as f:
        return yaml.safe_load(f)


def load_datasets() -> dict:
    path = PROJECT_ROOT / "config" / "core_datasets.yaml"
    if not path.exists():
        log(f"WARN: {path} not found — no datasets will be downloaded")
        return {"short_read": [], "long_read": []}
    with open(path) as f:
        return yaml.safe_load(f) or {}


# ---------------------------------------------------------------------------
# Step 1 — install core_tools env via the accumulator path
# ---------------------------------------------------------------------------

def install_core_tools(config: dict) -> dict[str, Any]:
    env_name      = config["core_tools"]["env_name"]
    env_path      = PROJECT_ROOT / config["paths"]["conda_envs_prefix"] / env_name
    pipelines_dir = PROJECT_ROOT / config["paths"]["pipelines_dir"]

    # Idempotency: if env exists AND a finalized spec exists, skip
    existing = [p for p in pipelines_dir.glob(f"{env_name}_*.yaml")
                if not p.name.endswith(".draft.yaml")]
    if env_path.exists() and existing:
        log(f"SKIP: {env_name} already installed (spec: {existing[0].name})")
        return {"skipped": True, "env_path": str(env_path), "spec_path": str(existing[0])}

    state      = PipelineState(config)
    pkg_search = PackageSearch(config)
    env_mgr    = EnvManager(config)

    r = state.start(env_name,
        "Core bioinf tooling: output validation (samtools / bcftools / seqkit) "
        "and reference genome indexing (bwa). Used by OutputValidator and the "
        "bootstrap. Not a user-facing pipeline.")
    pid = r["pipeline_id"]
    log(f"start_pipeline → {pid} (resumed={r['resumed']})")

    # Search for each package — captures resolved version + channel
    log("Resolving package versions via search_package...")
    for pkg_name, channel, check_cmd in CORE_TOOLS_PACKAGES:
        res = pkg_search.search(pkg_name, "latest")
        if res.get("found"):
            state.add_package(pid, {
                "name":              res.get("package_name", pkg_name),
                "requested_version": "latest",
                "resolved_version":  res.get("version"),
                "channel":           res.get("channel", channel),
                "conda_spec":        res.get("conda_spec"),
                "description":       res.get("description"),
                "homepage":          res.get("home"),
                "input_types":       res.get("input_types", []),
                "output_types":      res.get("output_types", []),
                "check_command":     check_cmd,
            })
            log(f"  {pkg_name} → {res.get('version')} ({res.get('channel')})")
        else:
            # Fall back to bare spec — install_packages still works
            state.add_package(pid, {
                "name":              pkg_name,
                "requested_version": "latest",
                "channel":           channel,
                "check_command":     check_cmd,
            })
            log(f"  {pkg_name} → search failed, using bare spec")

    # Create the env
    log(f"Creating env {env_name}...")
    create_res = env_mgr.create(env_name)
    if not create_res.get("success"):
        log("FATAL: env creation failed")
        log((create_res.get("error") or "")[-2000:])
        return {"error": "env creation failed", "details": create_res}
    state.set_conda_env(pid, env_name, python_version=config["conda"]["python_version"])

    # Install all packages in one solve
    log("Running solver + installing packages (this may take a minute)...")
    draft = state.get_draft(pid)
    pkg_specs = [
        {"spec": p.get("conda_spec") or p["name"], "channel": p["channel"]}
        for p in draft["packages"]
    ]
    inst_res = env_mgr.install(env_name, pkg_specs)
    if not inst_res.get("success"):
        log("FATAL: install_packages failed")
        log((inst_res.get("stderr") or "")[-2000:])
        return {"error": "package install failed", "details": inst_res}

    # Verify each tool (except conda-pack which is infrastructure)
    log("Verifying each tool...")
    for pkg_name, _channel, check_cmd in CORE_TOOLS_PACKAGES:
        if pkg_name == "conda-pack":
            continue
        v = env_mgr.verify(env_name, pkg_name, check_cmd)
        out = (v.get("output") or "")[:120].replace("\n", " ")
        ok = "✓" if v.get("success") else "✗"
        log(f"  {ok} {pkg_name}: {out}")
        state.patch_package(pid, pkg_name, {
            "verify_command": check_cmd,
            "verify_output":  (v.get("output") or "")[:500],
        })

    # The env is built and every tool verified. The combined env-spec writer
    # (save_pipeline_spec) was retired with the host build path — an env is now
    # solved once by freeze() and verified IN its shipped image (install==ship).
    # bootstrap's job is just to stand up the core_tools conda env + test data on
    # the host, so we stop here and clear the draft.
    draft = state.get_draft(pid)
    n_pkgs = len(draft.get("packages", []) or [])
    state.pop_for_finalize(pid)
    state.delete_draft_file(pid)
    log(f"core_tools env ready at {env_path} ({n_pkgs} packages verified)")

    return {"installed": True, "env_path": str(env_path), "packages_verified": n_pkgs}


# ---------------------------------------------------------------------------
# Step 2 — reference genome + indexes
# ---------------------------------------------------------------------------

def download_and_index_genome(config: dict, genome_build: str) -> dict[str, Any]:
    info = GENOME_BUILDS.get(genome_build)
    if not info:
        return {"error": f"unsupported genome_build: {genome_build}"}

    data_dir   = PROJECT_ROOT / config["paths"]["data_dir"]
    core_dir   = data_dir / f"core_test_data_{genome_build}"
    genome_dir = core_dir / "genome"
    genome_dir.mkdir(parents=True, exist_ok=True)
    fasta_path = genome_dir / info["name"]

    if not fasta_path.exists():
        log(f"Downloading {info['url']}...")
        # Stream + gunzip; write to .tmp first then rename (atomic on success)
        tmp_path = fasta_path.with_suffix(fasta_path.suffix + ".tmp")
        with urllib.request.urlopen(info["url"], timeout=300) as resp:
            with gzip.GzipFile(fileobj=resp) as gz, open(tmp_path, "wb") as out:
                shutil.copyfileobj(gz, out, length=65536)
        tmp_path.rename(fasta_path)
        log(f"  → {fasta_path.name} ({fasta_path.stat().st_size // 1024} KB)")
    else:
        log(f"SKIP genome download: {fasta_path.name} exists")

    env_name = config["core_tools"]["env_name"]
    env_mgr  = EnvManager(config)

    fai = fasta_path.with_suffix(fasta_path.suffix + ".fai")
    if not fai.exists():
        log("Building samtools fai index...")
        r = env_mgr.run_in_env(env_name, f"samtools faidx {fasta_path}", timeout=300)
        if r["returncode"] != 0:
            return {"error": "samtools faidx failed",
                    "stderr": (r.get("stderr") or "")[-500:]}

    bwt = fasta_path.with_suffix(fasta_path.suffix + ".bwt")
    if not bwt.exists():
        log("Building BWA index (~30s for chr22)...")
        r = env_mgr.run_in_env(env_name, f"bwa index {fasta_path}", timeout=600)
        if r["returncode"] != 0:
            return {"error": "bwa index failed",
                    "stderr": (r.get("stderr") or "")[-500:]}

    return {
        "success":   True,
        "fasta":     str(fasta_path),
        "core_dir":  str(core_dir),
        "chrom":     info["chrom"],
    }


# ---------------------------------------------------------------------------
# Step 3 — test datasets
# ---------------------------------------------------------------------------

def download_datasets(config: dict, datasets_cfg: dict, genome_build: str) -> dict[str, Any]:
    """Download every entry in core_datasets.yaml. Each result is recorded
    explicitly — no silent skips for long-read failures."""
    results: dict[str, list] = {
        "short_read": [], "long_read": [], "pod5": [], "phenopackets": []
    }

    for group_name, group in (("short_read", "Short-read"), ("long_read", "Long-read (best-effort)")):
        entries = datasets_cfg.get(group_name, [])
        if not entries:
            continue
        log(f"=== {group}: {len(entries)} dataset(s) ===")
        for d in entries:
            kwargs = {k: v for k, v in d.items() if k in _ADD_DATA_KWARGS}
            if group_name == "long_read":
                kwargs.setdefault("subset", "500")
            label = f"{d['accession']} ({d['assay_type']})"
            log(f"Adding {label}...")
            try:
                res = add_core_test_data(config, **kwargs)
            except Exception as e:
                res = {"success": False, "error": f"exception: {e}"}
            ok = res.get("success", False)
            log(f"  {'OK' if ok else ('SKIP' if group_name == 'long_read' else 'FAIL')}: "
                f"{res.get('error') or 'downloaded'}")
            results[group_name].append({
                "accession": d["accession"],
                "success":   ok,
                "error":     res.get("error"),
            })

    pod5_entries = datasets_cfg.get("pod5", [])
    if pod5_entries:
        log(f"=== POD5 raw signal (best-effort): {len(pod5_entries)} dataset(s) ===")
        for d in pod5_entries:
            label = f"{d['accession']} ({d.get('chemistry') or d.get('assay_type','?')})"
            log(f"Adding {label}...")
            try:
                res = add_core_pod5_data(
                    config,
                    accession=d["accession"],
                    sample=d.get("sample", d["accession"]),
                    source_url=d["source_url"],
                    assay_type=d.get("assay_type", "ont_wgs"),
                    platform=d.get("platform", "ont"),
                    chemistry=d.get("chemistry", ""),
                    flowcell=d.get("flowcell", ""),
                    kit=d.get("kit", ""),
                    suggested_model=d.get("suggested_model", ""),
                    expected_sha256=d.get("expected_sha256", ""),
                    expected_size=d.get("expected_size", 0),
                    genome_build=genome_build,
                )
            except Exception as e:
                res = {"success": False, "error": f"exception: {e}"}
            ok = res.get("success", False)
            log(f"  {'OK' if ok else 'SKIP'}: "
                f"{res.get('error') or f'{res.get(\"size_bytes\", 0)} B  sha256={(res.get(\"sha256\") or \"\")[:12]}…'}")
            results["pod5"].append({
                "accession":  d["accession"],
                "success":    ok,
                "error":      res.get("error"),
                "sha256":     res.get("sha256"),
                "size_bytes": res.get("size_bytes"),
            })

    pk_entries = datasets_cfg.get("phenopackets", [])
    if pk_entries:
        log(f"=== Phenopackets: {len(pk_entries)} dataset(s) ===")
        for d in pk_entries:
            url = d["source_url"]
            log(f"Adding {url.split('/')[-1]}...")
            try:
                res = add_phenopacket(config, source_url=url, genome_build=genome_build)
            except Exception as e:
                res = {"success": False, "error": f"exception: {e}"}
            ok = res.get("success", False)
            log(f"  {'OK' if ok else 'FAIL'}: "
                f"{res.get('error') or res.get('phenopacket_id')}")
            results["phenopackets"].append({
                "source_url":     url,
                "phenopacket_id": res.get("phenopacket_id"),
                "success":        ok,
                "error":          res.get("error"),
            })

    return results


# ---------------------------------------------------------------------------
# Step 4 — smoke test
# ---------------------------------------------------------------------------

def smoke_test(config: dict, genome_info: dict, datasets_result: dict) -> dict[str, Any]:
    """Align one read pair, sort + index, validate the BAM. Same code paths the
    agent uses (env_manager.run_in_env + OutputValidator.validate)."""
    env_name  = config["core_tools"]["env_name"]
    core_dir  = Path(genome_info["core_dir"])
    fasta     = genome_info["fasta"]
    env_mgr   = EnvManager(config)
    validator = OutputValidator(config)

    manifest_path = core_dir / "manifest.yaml"
    if not manifest_path.exists():
        return {"error": "no manifest.yaml — did dataset download fail?"}

    with open(manifest_path) as f:
        manifest = yaml.safe_load(f) or {}

    # Walk the manifest for a paired-end short-read dataset
    pick = None
    for read_type, ends in (manifest.get("sequencing_data") or {}).items():
        for end_type, assays in (ends or {}).items():
            if end_type != "paired_end":
                continue
            for assay, samples in (assays or {}).items():
                for smp in (samples or []):
                    for subset, info in (smp.get("subsets") or {}).items():
                        if info.get("r1") and info.get("r2"):
                            pick = (smp, subset, info, assay)
                            break
                    if pick: break
                if pick: break
            if pick: break
        if pick: break
    if pick is None:
        return {"error": "no paired-end short-read dataset on disk"}

    smp, subset, info, assay = pick
    r1 = core_dir / info["r1"]
    r2 = core_dir / info["r2"]
    out_dir = core_dir / "smoke_test"
    out_dir.mkdir(parents=True, exist_ok=True)
    bam = out_dir / "smoke.bam"

    log(f"  aligning {smp.get('sample')} {subset} ({assay}) reads with bwa mem...")
    r = env_mgr.run_in_env(env_name,
        f"bwa mem -t 2 {fasta} {r1} {r2} 2>/dev/null | "
        f"samtools sort -o {bam} - && samtools index {bam}",
        timeout=600)
    if r["returncode"] != 0:
        return {"error": "alignment pipeline failed",
                "stderr": (r.get("stderr") or "")[-500:]}

    log("  validating BAM with OutputValidator...")
    v = validator.validate(str(bam), "bam", env_name=env_name)
    if not v.get("passed"):
        return {"error": "BAM validation failed", "validation": v}

    return {
        "passed":     True,
        "sample":     smp.get("sample"),
        "subset":     subset,
        "bam":        str(bam),
        "size_bytes": v.get("size_bytes"),
        "flagstat":   (v.get("flagstat") or "")[:300],
    }


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="One-time bootstrap for bioinf-agent.")
    parser.add_argument("--genome-build", default="hg38",
                        choices=sorted(GENOME_BUILDS.keys()))
    parser.add_argument("--skip-smoke", action="store_true",
                        help="Skip the post-bootstrap alignment smoke test")
    args = parser.parse_args()

    config = load_config()
    env_name = config["core_tools"]["env_name"]

    log(f"=== Step 1: install {env_name} ===")
    install = install_core_tools(config)
    if install.get("error"):
        log(f"FATAL: {install['error']}")
        sys.exit(1)

    log(f"=== Step 2: reference genome ({args.genome_build}) ===")
    genome = download_and_index_genome(config, args.genome_build)
    if genome.get("error"):
        log(f"FATAL: {genome['error']}")
        sys.exit(1)

    log("=== Step 3: test datasets ===")
    datasets = download_datasets(config, load_datasets(), args.genome_build)

    if args.skip_smoke:
        log("=== Step 4: smoke test SKIPPED ===")
        smoke = {"skipped": True}
    else:
        log("=== Step 4: smoke test ===")
        smoke = smoke_test(config, genome, datasets)
        if smoke.get("error"):
            log(f"SMOKE FAILED: {smoke['error']}")
            log("(Bootstrap completed otherwise — investigate the env or test data)")
            sys.exit(2)
        log(f"  ✓ {smoke['sample']} {smoke['subset']} aligned + validated "
            f"({smoke.get('size_bytes', 0) // 1024} KB BAM)")

    # Summary
    sr_ok = sum(1 for r in datasets["short_read"] if r["success"])
    sr_tot = len(datasets["short_read"])
    lr_ok = sum(1 for r in datasets["long_read"] if r["success"])
    lr_tot = len(datasets["long_read"])
    p5_ok = sum(1 for r in datasets.get("pod5", []) if r["success"])
    p5_tot = len(datasets.get("pod5", []))
    pk_ok = sum(1 for r in datasets["phenopackets"] if r["success"])
    pk_tot = len(datasets["phenopackets"])

    log("")
    log("=== Bootstrap complete ===")
    log(f"  Core tools env: {install.get('env_path', '(skipped)')}")
    log(f"  Spec:           {Path(install.get('saved_yaml', '')).name if install.get('saved_yaml') else '(already installed)'}")
    log(f"  Reference:      {genome['fasta']}")
    log(f"  Short-read:     {sr_ok}/{sr_tot} OK")
    log(f"  Long-read:      {lr_ok}/{lr_tot} OK (best-effort)")
    log(f"  POD5:           {p5_ok}/{p5_tot} OK (best-effort)")
    log(f"  Phenopackets:   {pk_ok}/{pk_tot} OK")
    log(f"  Smoke test:     {'PASSED' if smoke.get('passed') else 'SKIPPED' if smoke.get('skipped') else 'FAILED'}")
    log("")
    log("Next — install pipelines via Claude Code:")
    log("  install latest GAPIT as my gwas_pipeline")


if __name__ == "__main__":
    main()
