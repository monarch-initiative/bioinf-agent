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

# (tool, language, github_repo, expect, note).
#
# `expect` is the whole point, and it used to be missing. This probe drove the real
# resolver, correctly classified `cellranger` as landing on the WRONG PACKAGE, wrote
# `note: "COLLISION: CRAN cellranger != 10x cellranger"` into docs/capability_map.json
# on 2026-07-03 — and returned 0. Thirteen days later a full-system audit "discovered"
# that same defect and called it the most user-facing hole in the system (§3). The
# detection was never the problem; the note was prose, so nothing could fail on it.
#
# A diagnostic that reports without asserting is a diagnostic that gets skipped. So the
# expectation is machine-checked and a violated one exits non-zero:
#   bucket=<str>       the routing bucket this tool MUST land in
# It used to also carry identity=True/False (resolve must CONFIRM / FLAG the pick). The
# reverse-theme-park Phase 2 (2026-07-17) DELETED the resolver's identity verdict: it now
# surfaces identity FACTS and the LLM ride judges. So this probe pins ROUTING only — the
# question a capability map exists to answer ("where does the router land real tools?").
# Whether the landed tool is the one the user MEANT (cellranger) is the ride's judgment and
# is unmeasurable here; it is the intent corpus's known_gap, tracked one level up.
# Add real tools here as the use-cases grow: this is the regression net for "can the
# system still handle an arbitrary tool?", which no unit test can answer.
CASES = [
    ("bwa",        "", None, {"bucket": "ROUTED:conda"},  "conda sanity"),
    ("samtools",   "", None, {"bucket": "ROUTED:conda"},  "conda sanity"),
    ("cutadapt",   "", None, {"bucket": "ROUTED:conda"},  "conda/pip"),
    ("macs2",      "", None, {"bucket": "ROUTED:conda"},  "peak calling"),
    ("gatk4",      "", None, {"bucket": "ROUTED:conda"},  "jar-class, on bioconda"),
    ("miniprot",   "", None, {"bucket": "ROUTED:conda"},  "aligner"),
    ("nanoplot",   "", None, {"bucket": "ROUTED:conda"},  "long-read QC"),
    ("DESeq2",     "r", None, {"bucket": "ROUTED:bioconductor"}, "bioconductor"),
    ("edgeR",      "r", None, {"bucket": "ROUTED:bioconductor"}, "bioconductor"),
    ("ape",        "",  None, {"bucket": "AMBIGUOUS"},         "name collision (PyPI ape != CRAN ape)"),
    # `language='r'` disambiguates away from the PyPI `ape` (a build tool). It lands on
    # conda's r-ape, not CRAN, because conda outranks cran — correct (the package's own
    # words, "manipulating phylogenetic trees", are surfaced for the ride to confirm).
    ("ape",        "r", None, {"bucket": "ROUTED:conda"},
     "disambiguated -> the R phylogenetics pkg (conda r-ape)"),
    ("GAPIT",      "r", None, {},                              "REPO-ONLY (plant GWAS) — discovery target"),
    ("GAPIT3",     "r", None, {},                              "REPO-ONLY variant name"),
    # THE case this file exists for. CRAN's cellranger parses spreadsheet cell ranges;
    # the tool anyone asking for "cellranger" means is 10x Genomics' single-cell suite,
    # which is a release binary and is on no registry. resolve ROUTES to cran (a real fact)
    # and surfaces its self_description ("Translate Spreadsheet Cell Ranges …"); catching
    # that this is not the meant tool is the LLM ride's job on those facts, not the router's,
    # so no `bucket` is asserted — the note carries the finding. This is the corpus's
    # 'interpretation' gap in miniature: green routing, wrong meaning.
    ("cellranger", "", None, {},
     "COLLISION: CRAN cellranger != 10x cellranger — identity is the ride's call now"),
    ("zzz_no_such_tool_xyz", "", None, {"bucket": "DEAD-END"}, "genuine dead-end"),
]


def classify(d: dict) -> str:
    if d.get("ambiguous"):
        return "AMBIGUOUS"
    if d.get("auto_discovered"):   # found a repo AND auto-adopted it → complete plan
        return f"AUTO-DISCOVERED:{d.get('recommended_repo')}→{d.get('chosen')}"
    if d.get("chosen"):
        return f"ROUTED:{d['chosen']}"
    if d.get("recommended_repo"):  # found candidate(s) but needs a human to confirm
        return f"DISCOVERED:{d['recommended_repo']}(CONFIRM)"
    return "DEAD-END"


def check(d: dict, bucket: str, expect: dict) -> list[str]:
    """Expectations violated by this resolution. Empty == the router still behaves.

    Only asserts what `expect` declares — an unlisted key is not a silent pass, it is
    a deliberate "we don't pin this yet".

    ROUTING only. The identity verdict (`identity.confirmed`) this used to assert was
    DELETED in the reverse-theme-park Phase 2 (2026-07-17): the resolver no longer
    confirms/flags a pick, it surfaces identity FACTS (self_description, channel,
    repo_anchored) and the LLM ride judges. So this probe pins WHERE a tool lands, which
    is what a capability map is for; whether the landed tool is the one the user MEANT is
    the ride's call and is unmeasurable by a resolve()-probe (see cellranger below)."""
    bad = []
    want = expect.get("bucket")
    if want and bucket != want:
        bad.append(f"expected bucket {want!r}, got {bucket!r}")
    return bad


def main() -> int:
    rows, out, failures = [], [], []
    for tool, lang, repo, expect, note in CASES:
        try:
            d = resolver.resolve(tool, language=lang, github_repo=repo, timeout=10)
            bucket = classify(d)
        except Exception as e:  # a probe that CRASHES is itself a capability finding
            d, bucket = {}, f"CRASH:{type(e).__name__}"
        bad = check(d, bucket, expect)
        failures += [f"{tool}({lang or '-'}): {b}" for b in bad]
        rows.append((tool, lang or "-", bucket, "FAIL" if bad else "ok", note))
        out.append({"tool": tool, "language": lang or None, "bucket": bucket,
                    "chosen": d.get("chosen"), "recommended_repo": d.get("recommended_repo"),
                    "auto_adoptable": d.get("repo_auto_adoptable"),
                    "identity": d.get("identity"), "expect": expect,
                    "violations": bad,
                    "available": d.get("available") or [], "note": note})

    print(f"\n  CAPABILITY MAP — {len(CASES)} tools (query-only routing)\n")
    print(f"  {'TOOL':<16}{'LANG':<5}{'WHERE IT LANDS':<34}{'':<6}NOTE")
    print("  " + "-" * 96)
    for tool, lang, bucket, verdict, note in rows:
        print(f"  {tool:<16}{lang:<5}{bucket:<34}{verdict:<6}{note}")

    dead = [r for r in rows if r[2] == "DEAD-END"]
    disc = [r for r in rows if r[2].startswith("DISCOVERED")]
    print(f"\n  routed: {sum(r[2].startswith('ROUTED') for r in rows)}  "
          f"discovered: {len(disc)}  ambiguous: {sum(r[2]=='AMBIGUOUS' for r in rows)}  "
          f"dead-end: {len(dead)}  crash: {sum(r[2].startswith('CRASH') for r in rows)}")

    dest = ROOT / "docs" / "capability_map.json"
    dest.write_text(json.dumps({"cases": out}, indent=2))
    print(f"  wrote {dest.relative_to(ROOT)}")

    if failures:
        print(f"\n  *** {len(failures)} EXPECTATION(S) VIOLATED — the router changed behaviour:")
        for f in failures:
            print(f"      {f}")
        print()
        return 1
    print("  all expectations hold\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
