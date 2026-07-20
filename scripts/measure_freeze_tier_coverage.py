#!/usr/bin/env python3
"""
measure_freeze_tier_coverage — the REAL breadth signal for the freeze tier grid.

For each freeze tier with a wired probe, this drives an ACTUAL container-native
docker build (env_freeze.build_env_image) and asserts the honesty contract passes
(env_honesty.check_build(res) == [] — BUILT · VALIDATED_IN_IMAGE · POLICY_CLEAN),
the SAME assertion L15/test_real_container_build.py makes for pigz. It records
passed/attempts + the image digest + the validation locus, writes
docs/freeze_tier_coverage.json, and re-renders docs/freeze_tier_grid.html.

This is the EXPENSIVE half, mirroring measure_terminal_coverage.py: it needs a
live Docker daemon + a warm base image, is SLOW (real builds, emulated when the
host arch != linux/amd64), and is EXPLICIT OPT-IN. The free CI never runs it — CI
gates only on the committed JSON via the hermetic ratchet test
(tests/test_freeze_tier_coverage.py). Refresh + commit the JSON after changing any
build code (container_build.py / env_build.py / env_freeze.py / install_commands.py):

    python scripts/measure_freeze_tier_coverage.py              # all wired tiers
    python scripts/measure_freeze_tier_coverage.py --only source
    python scripts/measure_freeze_tier_coverage.py --keep       # keep prior tiers' measurements

HONESTY. A tier is only ever recorded `proven` when a real build returned success
AND check_build was clean. No Docker daemon → the tier is `unmeasured` (attempts
0), NEVER passed — a docker-less green would be a lie (the trap L15 already guards
via _docker_up). `tier_record()` is a PURE function so the hermetic test can prove
that guard without Docker.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "freeze_tier_coverage.json"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.path.insert(0, str(Path(__file__).resolve().parent))
import freeze_tiers as ft  # noqa: E402

PLATFORM = "linux/amd64"   # the HPC ship platform; emulated on an arm64/other host


def _docker_up() -> bool:
    """A live daemon — the same probe L15 uses. Absent ⇒ tiers stay `unmeasured`,
    never falsely green."""
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=15).returncode == 0
    except Exception:
        return False


def _host_arch() -> str:
    import platform as _p
    return _p.machine().lower()


def build_tier(spec: dict) -> dict:
    """Drive ONE real container-native build for a wired tier. Returns a
    NORMALIZED outcome — {ok, error, image_digest, content_digest,
    validation_locus} — so the pure `tier_record` never touches Docker or the raw
    BuildResult shape. `ok` is True ONLY when the build succeeded AND the honesty
    contract is clean, the whole point of the grid."""
    from agent.skills import env_freeze
    from agent.skills.env_honesty import check_build

    b = spec["build"]
    name = f"bioinf_tiergrid_{spec['tier']}"
    try:
        if "conda_deps" in b:
            res = env_freeze.build_env_image(
                {}, name=name, conda_deps=b["conda_deps"],
                primary_tools=b["primary_tools"], platform=PLATFORM)
        elif "install_method" in b:
            im = b["install_method"]
            tool = im.get("name") or spec["probe_tool"]
            draft = {"install_steps": [{"installed_packages": [
                {"name": tool, "install_method": im}]}]}
            res = env_freeze.build_env_image(
                draft, name=name, primary_tools=b["primary_tools"], platform=PLATFORM)
        else:
            return {"ok": False, "error": f"tier {spec['tier']} has no recognized "
                    f"build recipe (need conda_deps or install_method)"}
    except Exception as e:   # a crash IS a build failure — record it, don't abort the sweep
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    violations = check_build(res)
    ok = bool(res.get("success")) and violations == []
    err = None
    if not ok:
        err = (res.get("error") or "").strip() or (
            f"check_build violations: {violations}" if violations else
            f"build did not succeed (outcome={res.get('outcome')})")
    return {"ok": ok, "error": err,
            "image_digest": res.get("image_digest"),
            "content_digest": res.get("content_digest"),
            "validation_locus": res.get("validation_locus")}


def tier_record(spec: dict, outcome: dict | None, docker_available: bool) -> dict:
    """PURE: fold a tier's build outcome into its committed JSON record. Hermetic-
    testable (no Docker). The honesty invariants live HERE:

      - docker_available False  → status 'unmeasured', attempts 0, NEVER passed.
      - builder is None         → status 'unmeasured' (declared, not yet probed).
      - a real outcome          → 'proven' iff outcome.ok, else 'broke'.

    `outcome` is None for an unmeasured tier (builder None, or Docker down)."""
    base = {"kind": spec["kind"], "probe_tool": spec.get("probe_tool") or None,
            "note": spec.get("note", "")}
    if spec.get("builder") is None or not docker_available or outcome is None:
        reason = None
        if spec.get("builder") is None:
            reason = "no real-build probe wired yet (declared row)"
        elif not docker_available:
            reason = "no Docker daemon at measurement time"
        return {**base, "attempts": 0, "passed": 0, "status": "unmeasured",
                "rate": None, "image_digest": None, "content_digest": None,
                "validation_locus": None, "last_error": reason}
    passed = 1 if outcome.get("ok") else 0
    return {**base, "attempts": 1, "passed": passed,
            "status": "proven" if passed else "broke",
            "rate": float(passed),   # passed/attempts with attempts==1
            "image_digest": outcome.get("image_digest"),
            "content_digest": outcome.get("content_digest"),
            "validation_locus": outcome.get("validation_locus"),
            "last_error": None if passed else (outcome.get("error") or "build failed")}


def _git(*a) -> str:
    try:
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def _render(data: dict) -> None:
    spec = importlib.util.spec_from_file_location(
        "render_freeze_tier_grid", ROOT / "scripts" / "render_freeze_tier_grid.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.write(data)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", default="", help="comma-separated tiers to (re)build")
    ap.add_argument("--keep", action="store_true",
                    help="keep prior measurements for tiers not built this run "
                         "(default: unbuilt tiers reset to unmeasured)")
    ap.add_argument("--out", default=str(OUT), help="output JSON path")
    args = ap.parse_args(argv)
    out_path = Path(args.out)

    docker = _docker_up()
    only = {t.strip() for t in args.only.split(",") if t.strip()}
    if only:
        unknown = only - set(ft.ALL_TIERS)
        if unknown:
            print(f"  ! unknown tier(s): {sorted(unknown)}", file=sys.stderr)
            return 2

    prior = {}
    if args.keep and out_path.exists():
        prior = (json.loads(out_path.read_text()).get("tiers") or {})

    if not docker:
        print("  ! no Docker daemon — every tier will record 'unmeasured' "
              "(a docker-less green would be a lie).", file=sys.stderr)

    tiers: dict[str, dict] = {}
    for spec in ft.FREEZE_TIERS:
        name = spec["tier"]
        build_this = spec.get("builder") is not None and (not only or name in only)
        if build_this and docker:
            print(f"  building tier '{name}' (probe: {spec.get('probe_tool')}) …", flush=True)
            outcome = build_tier(spec)
            tiers[name] = tier_record(spec, outcome, docker)
            print(f"    → {tiers[name]['status']}"
                  + (f" ({tiers[name]['last_error']})" if tiers[name]["last_error"] else ""))
        elif args.keep and name in prior:
            tiers[name] = prior[name]          # carry forward a prior measurement
        else:
            tiers[name] = tier_record(spec, None, docker)

    proven = sum(1 for r in tiers.values() if r["status"] == "proven"
                 and r["kind"] == "install")
    data = {
        "measured_on": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "git_sha": _git("rev-parse", "--short", "HEAD") or "unknown",
        "platform": PLATFORM,
        "host_arch": _host_arch(),
        "docker_available": docker,
        "install_tiers_total": len(ft.INSTALL_TIERS),
        "install_tiers_proven": proven,
        "tiers": tiers,
    }
    out_path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"\n  wrote {out_path.relative_to(ROOT) if out_path.is_relative_to(ROOT) else out_path}"
          f"  ({proven}/{len(ft.INSTALL_TIERS)} install tiers proven by a real build)")
    if out_path == OUT:
        _render(data)
        print(f"  rendered {(ROOT / 'docs' / 'freeze_tier_grid.html').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
