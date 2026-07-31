#!/usr/bin/env python
"""Re-render every sealed workflow's `{name}.RUN.html` from its `{name}.workflow.yaml`.

WHY THIS EXISTS. The Layer-2 run dashboard is WRITE-ONCE: `seal_workflow` renders it and
nothing ever touches it again. That is right for the RECORD — a sealed spec is a
digest-pinned provenance artifact — but the dashboard is not the record, it is a VIEW
rendered purely from the record, and a view has no provenance to preserve. So when the
renderer is corrected, every dashboard already on disk keeps showing the old, wrong thing
and no code path will ever fix it.

Measured 2026-07-31, which is what prompted this: `run_dashboard_html._usage_status` was
taught the three states (verified / failed / not_attempted) precisely so a page would stop
printing a bare `False` for "never tested". The fix shipped, the tests passed — and 4 of the
5 dashboards on disk still read `Usage self-tested: False`, because each had been rendered
before it landed. A renderer fix that reaches zero artifacts has not fixed anything a reader
can see, and "the report says False, the record says not_attempted" is the two-answers-to-one-
question defect in the artifact the user actually opens.

SAFE BY CONSTRUCTION. This only ever rewrites `.RUN.html`. It reads the sealed spec through
the typed `spec_writer.load_workflow_spec` seam, so a malformed artifact fails loudly here
rather than rendering a confident page from a scraped dict, and it NEVER writes a
`.workflow.yaml`. If a spec no longer validates, that spec is reported and skipped — the
stale page is left alone rather than replaced by a page built from a record we could not
parse.

    python scripts/rerender_run_dashboards.py            # re-render all, report diffs
    python scripts/rerender_run_dashboards.py --check    # report only; write nothing
    python scripts/rerender_run_dashboards.py NAME ...   # just these workflows
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from agent.skills.run_dashboard_html import render_run_dashboard_html   # noqa: E402
from agent.skills.spec_writer import load_workflow_spec                  # noqa: E402


def _env_record(request_key: str) -> dict:
    """The frozen env record a spec pins, for the ENV.html link + stale-detection.

    Best-effort ON PURPOSE: `render_run_dashboard_html` takes `env_record=None` and simply
    renders without the cross-link, so a dashboard is still re-renderable on a machine whose
    EnvCache no longer holds that env. Losing a hyperlink is a fair price; refusing to
    correct a page because an unrelated cache is cold is not.
    """
    if not request_key:
        return {}
    try:
        from agent.skills.freeze import EnvCache
        # Same location the server constructs it at (mcp_server.py:118) and the same one
        # resources.py:240 reads — env_reports/_env_cache.json, NOT a config key.
        return EnvCache(REPO / "env_reports" / "_env_cache.json").lookup(request_key) or {}
    except Exception:
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("names", nargs="*", help="workflow names (default: all sealed specs)")
    ap.add_argument("--check", action="store_true",
                    help="report which dashboards are stale; write nothing (CI-friendly)")
    ap.add_argument("--dir", default="env_reports", help="directory holding the artifacts")
    args = ap.parse_args()

    out_dir = (REPO / args.dir) if not Path(args.dir).is_absolute() else Path(args.dir)
    specs = sorted(out_dir.glob("*.workflow.yaml"))
    if args.names:
        wanted = set(args.names)
        specs = [p for p in specs if p.name[: -len(".workflow.yaml")] in wanted]
        missing = wanted - {p.name[: -len(".workflow.yaml")] for p in specs}
        for m in sorted(missing):
            print(f"  ?  {m}: no {m}.workflow.yaml in {out_dir}")

    if not specs:
        print(f"no sealed workflow specs found in {out_dir}")
        return 0

    stale, rewritten, failed = [], [], []
    for spec_path in specs:
        name = spec_path.name[: -len(".workflow.yaml")]
        try:
            spec = load_workflow_spec(spec_path)
        except Exception as e:
            failed.append(name)
            print(f"  !  {name}: spec did not validate, left untouched — "
                  f"{type(e).__name__}: {e}")
            continue

        d = spec.model_dump(exclude_none=True)
        html = render_run_dashboard_html(d, env_record=_env_record(d.get("env_request_key", "")))
        page = out_dir / f"{name}.RUN.html"
        current = page.read_text() if page.exists() else ""
        if current == html:
            print(f"  =  {name}: current")
            continue

        stale.append(name)
        if args.check:
            print(f"  ~  {name}: STALE (rendered before a renderer fix; run without --check)")
            continue
        page.write_text(html)
        rewritten.append(name)
        # Display-relative when the target is inside the repo, absolute otherwise: --dir
        # accepts any path, and a cosmetic prettifier must never be able to raise past a
        # write that already succeeded.
        try:
            shown = page.relative_to(REPO)
        except ValueError:
            shown = page
        print(f"  ->  {name}: re-rendered {shown}")

    print(f"\n{len(specs)} spec(s) · {len(stale)} stale · {len(rewritten)} re-rendered"
          + (f" · {len(failed)} unparseable" if failed else ""))
    # --check is an assertion about the tree, so a stale page is a non-zero exit. A spec that
    # would not validate is always a failure, checked or not: the artifact is broken, and
    # exiting 0 on it would be this script making the same absence-reads-as-fine mistake it
    # was written to correct.
    if failed:
        return 2
    return 1 if (args.check and stale) else 0


if __name__ == "__main__":
    raise SystemExit(main())
