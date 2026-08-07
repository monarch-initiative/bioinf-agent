#!/usr/bin/env python
"""Re-render every frozen env's `{name}.ENV.html` and `{name}.recipe.md` from its record.

WHY THIS EXISTS — the Layer-1 half of the argument `rerender_run_dashboards.py` makes for
Layer 2, and it went unbuilt for longer.

The Layer-1 deliverables are WRITE-ONCE: `freeze()` and `freeze_from_image()` render the
ENV report, the attestation and both recipe forms, and nothing ever touches them again.
That is right for the RECORDS — the EnvCache entry, `{name}.recipe.yaml` and
`{name}.attestation.json` are digest-pinned provenance and must never be rewritten. But
`{name}.ENV.html` and `{name}.recipe.md` are not records. They are VIEWS, rendered purely
from a record, and a view has no provenance to preserve. So when the renderer is corrected,
every page already on disk keeps showing the old, wrong thing, and no code path in the
system will ever fix it. Re-freezing is not the answer either: a rebuild yields a NEW
record with a NEW digest, so "just re-freeze it" means discarding the artifact you were
trying to correct.

WHAT PROMPTED IT. On 2026-08-07 the ENV report was taught to draw
`BuildContract.violations` — it had only ever drawn `.coverage`, so the word "violation"
appeared nowhere in the renderer and the status pill was `passed == total` over the
verifications list, which is a different and much weaker question. Rendered over the real
corpus, two of eighteen envs FAIL the contract:

    talos_v11   WELL_FORMED.shipped_binaries — the record uses the old key dialect, so
                its contents cannot be read without guessing
                -> the page said "✓ Validated in shipped image"
    multiqc     VALIDATED_IN_IMAGE.evidence_shape — the evidence pipes into `head -5`, so
                the recorded `passed` reports HEAD's exit status; it would pass in an image
                that does not contain the tool at all
                -> the page said "Adopted by digest"

Without this script that fix reaches ZERO pages a human opens, which is the same as not
having made it. The three artifacts a human reads are what this project is judged on; a
renderer that is correct only in principle is not one of them.

SAFE BY CONSTRUCTION. This writes `.ENV.html` and `.recipe.md` and NOTHING else. It never
touches `_env_cache.json`, `.recipe.yaml` or `.attestation.json`. Every page is rendered
from the record as it already exists — no field is recomputed, nothing is re-probed, no
Docker daemon or network is consulted — so a re-render cannot upgrade a claim, only
restate the one already recorded. A record that will not render is reported and SKIPPED,
leaving the stale page alone rather than replacing it with a page built from something we
could not read.

    python scripts/rerender_env_reports.py             # re-render all, report diffs
    python scripts/rerender_env_reports.py --check     # report only; write nothing
    python scripts/rerender_env_reports.py NAME ...    # just these envs
    python scripts/rerender_env_reports.py --dir PATH  # a corpus outside the repo
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agent.skills.env_report_html import render_env_report_html   # noqa: E402
from agent.skills.env_recipe_render import render_recipe_markdown  # noqa: E402


def _records(cache_path: Path) -> list[dict]:
    """Every freeze record in the EnvCache, in file order.

    The cache is `{request_key: record}` or `{request_key: {"record": …}}` depending on
    when it was written; both shapes are read here rather than at three call sites.
    """
    if not cache_path.exists():
        return []
    raw = json.loads(cache_path.read_text())
    entries = raw if isinstance(raw, list) else list(raw.values())
    out = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        rec = e.get("record") if isinstance(e.get("record"), dict) else e
        if rec.get("name"):
            out.append(rec)
    return out


def _rendered(record: dict, out_dir: Path) -> tuple[list[tuple[Path, str]], list[str]]:
    """`(pairs, errors)` — every view this record produces, each rendered INDEPENDENTLY.

    The recipe MARKDOWN is here because it is the second view rendered from a record and
    it drifted the same way: its "Verify what you rebuilt" section told every reader to
    run `verify_env_recipe`, which for an `authors-dockerfile` env runs nothing and returns
    `refused`. `recipe.yaml` is NOT here — that one is a record.

    PER-VIEW ISOLATION, and it is load-bearing rather than defensive. The first cut
    rendered both views in one try-block, and the FIRST record it met undid it:
    `talos_v11`'s `shipped_binaries` uses the old key dialect, `render_recipe_markdown`
    raises `ValidationError` on it, and the whole record was skipped — including its
    `ENV.html`, which is the page that now carries the very violation
    (`WELL_FORMED.shipped_binaries`) describing that malformation.

    The page that REPORTS a defect must not be blocked by the defect it reports. So each
    view stands alone: a broken recipe render leaves the old recipe.md untouched and still
    corrects the ENV report, and the failure is named rather than swallowed.
    """
    name = record.get("name") or "env"
    pairs: list[tuple[Path, str]] = []
    errors: list[str] = []

    try:
        pairs.append((out_dir / f"{name}.ENV.html", render_env_report_html(record)))
    except Exception as e:
        errors.append(f"ENV.html: {type(e).__name__}: {e}")

    recipe_yaml = out_dir / f"{name}.recipe.yaml"
    if recipe_yaml.exists():
        try:
            import yaml
            recipe = yaml.safe_load(recipe_yaml.read_text())
            if isinstance(recipe, dict):
                pairs.append((out_dir / f"{name}.recipe.md",
                              render_recipe_markdown(recipe, record)))
        except Exception as e:
            errors.append(f"recipe.md: {type(e).__name__}: {e}")
    return pairs, errors


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="env names (default: every record in the cache)")
    ap.add_argument("--check", action="store_true",
                    help="report which pages are stale; write nothing (CI-friendly)")
    ap.add_argument("--dir", default="env_reports", help="directory holding the artifacts")
    args = ap.parse_args()

    out_dir = Path(args.dir) if Path(args.dir).is_absolute() else (REPO / args.dir)
    records = _records(out_dir / "_env_cache.json")
    if args.names:
        wanted = set(args.names)
        found = {r["name"] for r in records}
        records = [r for r in records if r["name"] in wanted]
        for m in sorted(wanted - found):
            print(f"  ?  {m}: no record by that name in {out_dir}/_env_cache.json")

    if not records:
        print(f"no freeze records found in {out_dir}/_env_cache.json")
        return 0

    stale: list[str] = []
    rewritten: list[str] = []
    failed: list[str] = []
    for record in records:
        name = record["name"]
        pairs, errors = _rendered(record, out_dir)
        for err in errors:
            failed.append(f"{name}/{err.split(':', 1)[0]}")
            print(f"  !  {name}: {err.split(':', 1)[0]} would not render, left untouched — "
                  f"{err.split(':', 1)[1].strip().splitlines()[0]}")

        changed = [(p, html) for p, html in pairs
                   if (p.read_text() if p.exists() else "") != html]
        if not changed:
            if not errors:
                print(f"  =  {name}: current")
            continue

        stale.append(name)
        which = ", ".join(p.name.split(".", 1)[1] for p, _ in changed)
        if args.check:
            print(f"  ~  {name}: STALE ({which}) — rendered before a renderer fix")
            continue
        for p, html in changed:
            p.write_text(html)
        rewritten.append(name)
        print(f"  -> {name}: re-rendered {which}")

    print(f"\n{len(records)} record(s) · {len(stale)} stale · {len(rewritten)} re-rendered"
          + (f" · {len(failed)} unrenderable" if failed else ""))
    # --check is an ASSERTION about the tree, so a stale page exits non-zero. A record that
    # will not render is a real problem either way and also exits non-zero — same posture
    # as the Layer-2 script, so neither can be green while an artifact is wrong.
    if failed:
        return 2
    return 1 if (args.check and stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())
