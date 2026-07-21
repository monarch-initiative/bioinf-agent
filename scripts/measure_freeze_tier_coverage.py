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

HONESTY. A tier is only ever recorded `proven` when a real build returned success
AND check_build was clean. No Docker daemon → the tier is `unmeasured` (attempts
0), NEVER passed — a docker-less green would be a lie (the trap L15 already guards
via _docker_up). `tier_record()` is a PURE function so the hermetic test can prove
that guard without Docker; and a docker-less run REFUSES to overwrite the committed
grid at all (pass --force to write a deliberate all-unmeasured file), so no code
path can launder a stale or absent measurement into the committed artifact.
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


def _synth_install_method(ss: dict) -> dict:
    """Drive the REAL synthesis machinery for the synthesized tier's `synth_spec`, and
    return a provenance-tagged `install_method` ready for build_env_image — or raise.

    This is what makes the synthesized tier HONEST rather than a hand-tag: the `submission`
    in the recipe is only the agent's CLAIM (what it would type after reading synth_fetch).
    Here we re-run the runtime's own ground-truth path:

      1. fetch_build_source RE-FETCHES the repo at the PINNED commit and reads its build
         files + their sha256s (the runtime's bytes, not the recipe's word).
      2. an ANCHOR check refuses if the re-fetch resolved a different commit (the source
         moved) — the same guard synth_build makes.
      3. validate_submission RE-VERIFIES every command against those bytes: an 'extracted'
         command must occur verbatim in its named origin_file (sha256 stamped by the
         runtime), an 'agent_authored' command must be grounded in the corpus (its
         URLs/remotes present). A paraphrase or an invented URL → violation → we raise.

    So the shipped `commands` carry provenance the RUNTIME produced; check_build's
    PROVENANCE_CLEAN passes because nothing was hand-tagged. Raising (not returning an
    error dict) lets build_tier's single except-handler record it as a build failure."""
    from agent.skills.env_manager import EnvManager
    from agent.skills import synthesis as _synth

    repo, commit = ss["repo_url"], ss["commit"]
    fetch = EnvManager.fetch_build_source(repo, commit, is_relevant=_synth.is_build_relevant)
    if not fetch.get("success"):
        raise RuntimeError(f"synth fetch of {repo}@{commit[:12]} failed: {fetch.get('error')}")
    if fetch.get("commit") != commit:
        raise RuntimeError(
            f"synth anchor mismatch: re-fetch resolved {fetch.get('commit')!r}, "
            f"recipe pinned {commit!r} — the source moved; re-pin the commit")
    # Ground the source URL exactly as synth_build does (a `git clone {url}` grounds).
    fetch["corpus"] = _synth.build_corpus(fetch["files"]) + "\n" + repo
    val = _synth.validate_submission(fetch, list(ss["submission"]))
    if not val["ok"]:
        raise RuntimeError(f"synth provenance violation (not shippable): {val['violations']}")
    return {"type": "synthesized", "source": repo, "tool": ss["tool"],
            "evidence": ss.get("evidence", ""),
            "engine_coupled": ss.get("engine_coupled", False),
            "commands": val["records"], "commit_sha": fetch["commit"],
            "file_hashes": {f["path"]: f["sha256"] for f in fetch["files"]}}


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
        elif "synth_spec" in b:
            # The synthesized tier is NOT a static install_method: its honesty rests on
            # driving the REAL synth_fetch + validate_submission machinery so the shipped
            # provenance is the runtime's own re-verification (a hand-tagged install_method
            # is exactly what check_build.PROVENANCE_CLEAN refuses — the 2026-07-20 gap).
            # `_synth_install_method` re-fetches at the pinned commit, re-verifies every
            # submitted command against those bytes, and returns a provenance-tagged
            # install_method — or raises with the reason (a mismatch/violation is a build
            # failure recorded like any other, not a silent skip).
            im = _synth_install_method(b["synth_spec"])
            draft = {"install_steps": [{"installed_packages": [
                {"name": b["synth_spec"]["tool"], "install_method": im}]}]}
            res = env_freeze.build_env_image(
                draft, name=name, primary_tools=b["primary_tools"], platform=PLATFORM)
        elif "install_method" in b:
            im = b["install_method"]
            tool = im.get("name") or spec["probe_tool"]
            draft = {"install_steps": [{"installed_packages": [
                {"name": tool, "install_method": im}]}]}
            res = env_freeze.build_env_image(
                draft, name=name, primary_tools=b["primary_tools"], platform=PLATFORM)
        else:
            return {"ok": False, "error": f"tier {spec['tier']} has no recognized "
                    f"build recipe (need conda_deps, install_method, or synth_spec)"}
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


def tier_record(spec: dict, outcome: dict | None, docker_available: bool,
                *, sha: str = "", now: str = "") -> dict:
    """PURE: fold a tier's FRESH build outcome into its committed JSON record.
    Hermetic-testable (no Docker). The honesty invariants live HERE:

      - docker_available False  → status 'unmeasured', attempts 0, NEVER passed.
      - builder is None         → status 'unmeasured' (declared, not yet probed).
      - a real outcome          → 'proven' iff outcome.ok, else 'broke'.

    `outcome` is None for an unmeasured tier (builder None, or Docker down). `sha`/`now`
    stamp WHEN and against WHAT commit this tier was measured — the per-tier provenance
    that makes INCREMENTAL measurement honest (a later run can measure one tier at a new
    sha and carry the rest forward WITHOUT re-stamping their older shas; see
    `carry_forward`). `recipe_fingerprint` records the recipe this measurement built, so
    a future edit to that recipe auto-invalidates the carried record."""
    base = {"kind": spec["kind"], "probe_tool": spec.get("probe_tool") or None,
            "note": spec.get("note", ""),
            "recipe_fingerprint": ft.recipe_fingerprint(spec)}
    if spec.get("builder") is None or not docker_available or outcome is None:
        reason = None
        if spec.get("builder") is None:
            reason = "no real-build probe wired yet (declared row)"
        elif not docker_available:
            reason = "no Docker daemon at measurement time"
        return {**base, "attempts": 0, "passed": 0, "status": "unmeasured",
                "rate": None, "image_digest": None, "content_digest": None,
                "validation_locus": None, "measured_sha": None, "measured_on": None,
                "carried_forward": False, "last_error": reason}
    passed = 1 if outcome.get("ok") else 0
    return {**base, "attempts": 1, "passed": passed,
            "status": "proven" if passed else "broke",
            "rate": float(passed),   # passed/attempts with attempts==1
            "image_digest": outcome.get("image_digest"),
            "content_digest": outcome.get("content_digest"),
            "validation_locus": outcome.get("validation_locus"),
            "measured_sha": sha or None, "measured_on": now or None,
            "carried_forward": False,
            "last_error": None if passed else (outcome.get("error") or "build failed")}


def carry_forward(spec: dict, prev: dict | None, *, recipe_changed: bool,
                  current_fp: str, grandfather_sha: str = "") -> dict:
    """PURE: fold a tier's PRIOR committed record into a new grid when we are measuring a
    DIFFERENT tier this run — the honest INCREMENTAL path that replaces re-baking every
    tier on every run. Hermetic-testable (no Docker, no git — the caller decides
    `recipe_changed`). Each rule guards a specific way carry-forward could lie:

      - prev None / not a REAL measurement (unmeasured, attempts 0) → 'unmeasured'. We only
        ever carry a real prior build result forward, never a placeholder — nothing is
        conjured into existence.
      - `recipe_changed` (this tier's recipe differs from when it was measured) → 'unmeasured'
        with a self-explaining error. The old measurement no longer describes today's recipe,
        so it CANNOT keep counting as proven — this is what makes 'retroactively fix a tier'
        safe (edit its recipe → it reddens until re-baked, per the floor-earned ratchet).
      - else → carry the measurement VERBATIM, PRESERVING the original `measured_sha`
        (NOT re-stamping it to now). Re-stamping stale records as fresh was exactly the
        `--keep` laundering bug a prior self-review removed; here the original sha is kept
        and `carried_forward: True` discloses this tier was not rebuilt this run, so the
        render can flag its age. `current_fp` is stored so the NEXT run compares against it.

    MIGRATION (a pre-fingerprint prior record, `recipe_fingerprint` absent): the caller
    passes `recipe_changed=False` only after verifying out-of-band (git) that the recipe is
    unchanged since it was measured, and `grandfather_sha` backfills the otherwise-missing
    `measured_sha`. The result carries `fingerprint_backfilled: True` so the one-time
    grandfathering is visible, and every record has a fingerprint from then on."""
    base = {"kind": spec["kind"], "probe_tool": spec.get("probe_tool") or None,
            "note": spec.get("note", ""), "recipe_fingerprint": current_fp}

    def _unmeasured(reason: str) -> dict:
        return {**base, "attempts": 0, "passed": 0, "status": "unmeasured", "rate": None,
                "image_digest": None, "content_digest": None, "validation_locus": None,
                "measured_sha": (prev or {}).get("measured_sha"), "measured_on": None,
                "carried_forward": False, "last_error": reason}

    if not prev or prev.get("status") not in ("proven", "broke") \
            or (prev.get("attempts") or 0) < 1:
        return _unmeasured("no prior real measurement to carry forward")
    if recipe_changed:
        return _unmeasured("recipe changed since this tier was last measured — "
                           "re-run the generator for it (--only " + spec["tier"] + ")")
    backfilled = not prev.get("recipe_fingerprint")
    return {**base,
            "attempts": prev["attempts"], "passed": prev["passed"],
            "status": prev["status"], "rate": prev.get("rate"),
            "image_digest": prev.get("image_digest"),
            "content_digest": prev.get("content_digest"),
            "validation_locus": prev.get("validation_locus"),
            "measured_sha": prev.get("measured_sha") or (grandfather_sha or None),
            "measured_on": prev.get("measured_on"),
            "carried_forward": True, "fingerprint_backfilled": backfilled,
            "last_error": prev.get("last_error")}


def _git(*a) -> str:
    try:
        return subprocess.run(["git", *a], cwd=ROOT, capture_output=True,
                              text=True, timeout=10).stdout.strip()
    except Exception:
        return ""


def _rel(p) -> str:
    """Repo-relative display path, tolerant of a path outside ROOT (e.g. a --out
    override or a monkeypatched OUT) — never raises."""
    p = Path(p)
    return str(p.relative_to(ROOT)) if p.is_relative_to(ROOT) else str(p)


def _render(data: dict) -> None:
    spec = importlib.util.spec_from_file_location(
        "render_freeze_tier_grid", ROOT / "scripts" / "render_freeze_tier_grid.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.write(data)


def _load_prior(out_path: Path) -> dict:
    """The prior committed grid's tiers (for carry-forward), or {} if none/unreadable.
    A missing or malformed prior simply means every non-measured tier records
    'unmeasured' — carry-forward can only ever build on a real prior file."""
    try:
        return (json.loads(out_path.read_text()).get("tiers") or {}) if out_path.exists() else {}
    except Exception:
        return {}


def _recipe_unchanged_since(baseline_sha: str, tier_name: str, current_fp: str) -> bool:
    """Did `tier_name`'s BUILD RECIPE change between `baseline_sha` and now? Reads the
    tier's recipe from freeze_tiers.py AT baseline_sha via `git show`, fingerprints it
    with the CURRENT `recipe_fingerprint`, and compares. Used ONLY to grandfather a
    PRE-fingerprint prior record (one measured before per-tier fingerprints existed):
    the code VERIFIES the recipe is byte-identical since it was measured rather than
    trusting it. Any failure → False (conservative: treat as changed ⇒ re-measure), so a
    git hiccup can only ever cost a rebuild, never launder a stale green."""
    try:
        src = subprocess.run(["git", "show", f"{baseline_sha}:scripts/freeze_tiers.py"],
                             cwd=ROOT, capture_output=True, text=True, timeout=15)
        if src.returncode != 0 or not src.stdout.strip():
            return False
        ns: dict = {"__file__": str(ROOT / "scripts" / "freeze_tiers.py"), "__name__": "_ft_baseline"}
        exec(compile(src.stdout, "<freeze_tiers@baseline>", "exec"), ns)
        old = next((t for t in (ns.get("FREEZE_TIERS") or []) if t.get("tier") == tier_name), None)
        return old is not None and ft.recipe_fingerprint(old) == current_fp
    except Exception:
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", default="", help="comma-separated tiers to (re)build; the "
                    "rest are CARRIED FORWARD from the committed grid (incremental)")
    ap.add_argument("--out", default=str(OUT), help="output JSON path")
    ap.add_argument("--force", action="store_true",
                    help="write even with no Docker daemon (produces an all-"
                         "'unmeasured' file — this deliberately reddens the ratchet)")
    ap.add_argument("--grandfather-from", default="", metavar="SHA",
                    help="one-time migration: carry forward PRE-fingerprint prior records "
                         "whose recipe is git-verified unchanged since SHA (the commit they "
                         "were measured at). Without it, a fingerprint-less prior is treated "
                         "as changed ⇒ re-measure. No effect on records that already carry a "
                         "recipe_fingerprint.")
    args = ap.parse_args(argv)
    out_path = Path(args.out)
    is_canonical = out_path.resolve() == OUT   # any spelling of the committed target

    docker = _docker_up()
    only = {t.strip() for t in args.only.split(",") if t.strip()}
    if only:
        unknown = only - set(ft.ALL_TIERS)
        if unknown:
            print(f"  ! unknown tier(s): {sorted(unknown)}", file=sys.stderr)
            return 2

    # HONESTY GUARD: a Docker-less run can MEASURE NOTHING, so it must never be
    # allowed to overwrite the committed grid — doing so would silently erase the
    # proven tiers (a footgun that reddens the ratchet with no obvious cause). Refuse the
    # canonical write unless the operator explicitly --forces an all-unmeasured file
    # (which then reddens the ratchet in CI, as it should). Carry-forward is DISABLED when
    # Docker is down (below) so this run can never launder a prior green as "current"
    # either — same posture as tier_record's per-tier guard, at the write boundary so no
    # code path can route around it (the exact seam a self-review flagged).
    if not docker and is_canonical and not args.force:
        print(f"  ! no Docker daemon — refusing to overwrite the committed "
              f"{_rel(OUT)} (a docker-less run measures nothing). Re-run "
              f"with Docker up; or pass --force to write an all-unmeasured file "
              f"deliberately; or --out <path> to write elsewhere.", file=sys.stderr)
        return 1
    if not docker:
        print("  ! no Docker daemon — every tier will record 'unmeasured' "
              "(a docker-less green would be a lie).", file=sys.stderr)

    head_sha = _git("rev-parse", "--short", "HEAD") or "unknown"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    prior = _load_prior(out_path)   # for carry-forward (only consulted when Docker is up)

    tiers: dict[str, dict] = {}
    for spec in ft.FREEZE_TIERS:
        name = spec["tier"]
        current_fp = ft.recipe_fingerprint(spec)
        build_this = spec.get("builder") is not None and (not only or name in only)
        if not docker:
            # Docker down: measure NOTHING and carry NOTHING — every tier unmeasured.
            # (No carry-forward here, so a docker-less run can't restamp a prior green.)
            tiers[name] = tier_record(spec, None, docker)
        elif build_this:
            print(f"  building tier '{name}' (probe: {spec.get('probe_tool')}) …", flush=True)
            outcome = build_tier(spec)
            tiers[name] = tier_record(spec, outcome, docker, sha=head_sha, now=now)
            print(f"    → {tiers[name]['status']}"
                  + (f" ({tiers[name]['last_error']})" if tiers[name]["last_error"] else ""))
        else:
            # CARRY FORWARD the prior committed measurement (the incremental path). Decide
            # whether this tier's recipe changed since it was measured:
            #   - prior HAS a fingerprint → compare it to today's (pure, robust);
            #   - prior LACKS one (pre-fingerprint record) → only carry it if
            #     --grandfather-from git-verifies the recipe is unchanged since that sha.
            prev = prior.get(name)
            prev_fp = (prev or {}).get("recipe_fingerprint")
            if prev_fp:
                recipe_changed = prev_fp != current_fp
            elif args.grandfather_from and prev and prev.get("status") in ("proven", "broke"):
                recipe_changed = not _recipe_unchanged_since(args.grandfather_from, name, current_fp)
            else:
                recipe_changed = True   # no fingerprint + no verified baseline ⇒ re-measure
            tiers[name] = carry_forward(spec, prev, recipe_changed=recipe_changed,
                                        current_fp=current_fp,
                                        grandfather_sha=args.grandfather_from)
            if tiers[name].get("carried_forward"):
                gf = " (grandfathered)" if tiers[name].get("fingerprint_backfilled") else ""
                print(f"  carried '{name}' forward: {tiers[name]['status']} "
                      f"@{tiers[name].get('measured_sha')}{gf}", flush=True)

    proven = sum(1 for r in tiers.values() if r["status"] == "proven"
                 and r["kind"] == "install")
    data = {
        "measured_on": now,          # this assembly run; per-tier measured_on is authoritative
        "git_sha": head_sha,         # this assembly's HEAD; per-tier measured_sha is authoritative
        "platform": PLATFORM,
        "host_arch": _host_arch(),
        "docker_available": docker,
        "install_tiers_total": len(ft.INSTALL_TIERS),
        "install_tiers_proven": proven,
        "tiers": tiers,
    }
    out_path.write_text(json.dumps(data, indent=2) + "\n")
    print(f"\n  wrote {_rel(out_path)}"
          f"  ({proven}/{len(ft.INSTALL_TIERS)} install tiers proven by a real build)")
    if is_canonical:
        _render(data)
        print(f"  rendered {(ROOT / 'docs' / 'freeze_tier_grid.html').relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
