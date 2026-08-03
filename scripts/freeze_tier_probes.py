"""
freeze_tier_probes — the executable counterpart of the scripts/freeze_tiers.py recipe rows.

A LIBRARY, not a meter. Each function takes ONE tier recipe row and drives the REAL
production path for it — `env_freeze.build_env_image` for the container-native install
tiers, `freeze_from_image` / `build_from_authors_recipe` for the build-method rows — and
returns a normalized {ok, error, image_digest, content_digest, validation_locus} outcome.
There is no main(), no JSON output, and NO COVERAGE CLAIM: nothing here counts tiers,
carries a measurement forward, or writes a committed artifact.

That is deliberate. These functions used to live in scripts/measure_freeze_tier_coverage.py,
which wrapped them in a counter that published `install_tiers_proven: 10` to
docs/freeze_tier_coverage.json. The counter was retired because it hand-built its own
`{"install_steps": [{"installed_packages": [...]}]}` input and called NO install primitive —
so the one hop it implied it covered (does the PRIMITIVE write a record the freeze dispatch
can consume?) was precisely the hop it skipped. The drivers themselves were never the
problem and are kept, because they are what the hermetic wiring tests exercise:
tests/test_freeze_build_method_tiers.py and tests/test_freeze_synthesized_tier.py
monkeypatch the executors these functions call and assert the right one is reached with the
right arguments.

Every heavy import stays FUNCTION-LOCAL on purpose — the hermetic tests monkeypatch module
attributes (`env_freeze`, `ffi`) by string target, which only resolves because the import
happens at call time, not at module import.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

PLATFORM = "linux/amd64"   # the HPC ship platform; emulated on an arm64/other host


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


def _build_method_outcome(res: dict) -> dict:
    """Normalize a freeze_from_image / build_from_authors_recipe result into build_tier's
    shape. Those executors run the honesty contract (check_build) INTERNALLY and return a
    proven/refused/broke dict — so a non-`success` result is a real failure (a refusal =
    honesty violation, a broke = pull/build failure), recorded `ok=False` with the reason.
    Same honesty bar as the container-native tiers: a green means the adopted/built image
    passed BUILT · VALIDATED_IN_IMAGE · POLICY_CLEAN on real bytes."""
    ok = bool(res.get("success"))
    err = None
    if not ok:
        viols = res.get("honesty_violations")
        err = (res.get("error") or "").strip() or (
            f"honesty violations: {viols}" if viols else "not proven (no success)")
    # The in-image evidence runs under `docker run --platform linux/amd64`; on a non-amd64
    # host that is QEMU emulation — the same locus the container-native tiers record. This is
    # a faithful derivation from the known (host arch, fixed --platform), not a fabricated field.
    host = _host_arch()
    locus = ("emulated" if host not in ("x86_64", "amd64") else "native") if ok else None
    return {"ok": ok, "error": err,
            "image_digest": res.get("image_digest"),
            "content_digest": res.get("content_digest"),
            "validation_locus": locus}


def build_method_tier(spec: dict) -> dict:
    """Drive ONE real ADOPT / AUTHORS-DOCKERFILE build-method probe — the routes that ship
    an image WITHOUT a container-native reconstruction, so they don't ride build_env_image.
    Uses the SAME injectable executors the freeze() MCP surface uses (freeze_from_image /
    build_from_authors_recipe), pointed at a THROWAWAY EnvCache + reports dir so nothing
    touches the real cache or env_reports/. Returns the normalized build_tier outcome."""
    import tempfile
    from agent.skills import freeze_from_image as ffi
    from agent.skills.biocontainers import resolve_biocontainer
    from agent.skills.freeze import EnvCache

    b = spec["build"]
    name = f"bioinf_tiergrid_{spec['tier']}"
    try:
        with tempfile.TemporaryDirectory(prefix="tiergrid_bm_") as td:
            cache = EnvCache(Path(td) / "env_cache.json")
            reports = Path(td) / "reports"
            if "adopt" in b:
                a = b["adopt"]
                image = a.get("image")
                if a.get("kind") == "biocontainer":
                    # The distinguishing act of the biocontainer row: resolve the curated
                    # quay.io/biocontainers image + adopt it BY MANIFEST DIGEST.
                    rb = resolve_biocontainer([(a["tool"], a.get("version"))])
                    if not (rb.get("found") and rb.get("image_by_digest")):
                        return {"ok": False, "error": f"no biocontainer for "
                                f"{a['tool']}={a.get('version')}: {rb.get('reason')}"}
                    image = rb["image_by_digest"]
                res = ffi.freeze_from_image(
                    image=image, tools=[{"name": a["tool"], "evidence": a["evidence"]}],
                    name=name, env_cache=cache, reports_dir=reports,
                    build_method="adopt-image", platform=PLATFORM)
            elif "authors_dockerfile" in b:
                ad = b["authors_dockerfile"]
                res = ffi.build_from_authors_recipe(
                    repo=ad["repo"], tools=[{"name": ad["tool"], "evidence": ad["evidence"]}],
                    name=name, env_cache=cache, reports_dir=reports,
                    recipe=ad.get("recipe", "Dockerfile"), ref=ad.get("ref", ""),
                    version=ad.get("ref", ""), build_args=ad.get("build_args"),
                    platform=PLATFORM)
            else:
                return {"ok": False, "error": f"build_method tier {spec['tier']} has no "
                        f"recognized recipe (need 'adopt' or 'authors_dockerfile')"}
    except Exception as e:   # a crash IS a failure — record it, don't abort the sweep
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return _build_method_outcome(res)
