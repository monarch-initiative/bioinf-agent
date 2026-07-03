#!/usr/bin/env python3
"""Capability map — where does the router LAND real tools? (the capability window)

This is the companion to the code-coverage view: coverage answers "is our code
tested?", this answers "what tools can the system actually handle, and where does
it fall?" — the question the whole system exists to answer. Query-only (no installs),
so it's cheap to re-run; it exercises `resolver.resolve` across a deliberately
diverse + hard set, incl. genuinely repo-only tools and known name-collisions.

Each tool lands in one bucket:
  ROUTED:<tier>      — resolved to a registry/repo tier (conda/pip/cran/... )
  DISCOVERED:<repo>  — no registry hit, but the github-search discovery step found
                       a repo (AUTO = dominant exact match; CONFIRM = needs a human)
  AMBIGUOUS          — same name on >1 ecosystem; caller must disambiguate
  DEAD-END           — nothing found (fails legibly with a hint)

Run:  PYTHONPATH=. python scripts/capability_probe.py
Writes docs/capability_map.json (the machine view) and prints a table.
NOT a pytest test — routing depends on live registries (non-deterministic); the
discovery LOGIC itself is unit-tested in tests/test_synthesis.py.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.skills import resolver  # noqa: E402

# (tool, language, github_repo, note). Spread across tiers + the hard cases the
# capability probe exists to catch. Add real tools here as the use-cases grow.
CASES = [
    ("bwa",        "", None, "conda sanity"),
    ("samtools",   "", None, "conda sanity"),
    ("cutadapt",   "", None, "conda/pip"),
    ("macs2",      "", None, "peak calling"),
    ("gatk4",      "", None, "jar-class, on bioconda"),
    ("miniprot",   "", None, "aligner"),
    ("nanoplot",   "", None, "long-read QC"),
    ("DESeq2",     "r", None, "bioconductor"),
    ("edgeR",      "r", None, "bioconductor"),
    ("ape",        "", None, "name collision (PyPI ape != CRAN ape)"),
    ("ape",        "r", None, "disambiguated -> CRAN"),
    ("GAPIT",      "r", None, "REPO-ONLY (plant GWAS) — discovery target"),
    ("GAPIT3",     "r", None, "REPO-ONLY variant name"),
    ("cellranger", "", None, "COLLISION: CRAN cellranger != 10x cellranger"),
    ("zzz_no_such_tool_xyz", "", None, "genuine dead-end"),
]


def classify(d: dict) -> str:
    if d.get("ambiguous"):
        return "AMBIGUOUS"
    if d.get("chosen"):
        return f"ROUTED:{d['chosen']}"
    if d.get("recommended_repo"):
        mode = "AUTO" if d.get("repo_auto_adoptable") else "CONFIRM"
        return f"DISCOVERED:{d['recommended_repo']}({mode})"
    return "DEAD-END"


def main() -> int:
    rows, out = [], []
    for tool, lang, repo, note in CASES:
        try:
            d = resolver.resolve(tool, language=lang, github_repo=repo, timeout=10)
            bucket = classify(d)
        except Exception as e:  # a probe that CRASHES is itself a capability finding
            d, bucket = {}, f"CRASH:{type(e).__name__}"
        rows.append((tool, lang or "-", bucket, note))
        out.append({"tool": tool, "language": lang or None, "bucket": bucket,
                    "chosen": d.get("chosen"), "recommended_repo": d.get("recommended_repo"),
                    "auto_adoptable": d.get("repo_auto_adoptable"),
                    "available": d.get("available") or [], "note": note})

    print(f"\n  CAPABILITY MAP — {len(CASES)} tools (query-only routing)\n")
    print(f"  {'TOOL':<16}{'LANG':<5}{'WHERE IT LANDS':<40}NOTE")
    print("  " + "-" * 96)
    for tool, lang, bucket, note in rows:
        print(f"  {tool:<16}{lang:<5}{bucket:<40}{note}")

    dead = [r for r in rows if r[2] == "DEAD-END"]
    disc = [r for r in rows if r[2].startswith("DISCOVERED")]
    print(f"\n  routed: {sum(r[2].startswith('ROUTED') for r in rows)}  "
          f"discovered: {len(disc)}  ambiguous: {sum(r[2]=='AMBIGUOUS' for r in rows)}  "
          f"dead-end: {len(dead)}  crash: {sum(r[2].startswith('CRASH') for r in rows)}")

    dest = ROOT / "docs" / "capability_map.json"
    dest.write_text(json.dumps({"cases": out}, indent=2))
    print(f"  wrote {dest.relative_to(ROOT)}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
