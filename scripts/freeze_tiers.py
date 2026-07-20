#!/usr/bin/env python3
"""
freeze_tiers — the canonical definition of the freeze TIERS, imported by the
generator, the render, and the ratchet test so all three share ONE source of
truth (the sibling of seaworthy_scope.py for the outcomes dashboard).

WHY THIS EXISTS. The headline promise is "call once for ANY tool, get a
trustworthy artifact." That promise is only as true as the number of install
tiers a REAL container-native docker build has actually baked AND passed
`env_honesty.check_build` on. The P2 tier-breadth recon measured that number at
**1 of 11** (only conda, via L15/pigz) — every other tier was implemented and
wired but never baked. This module enumerates every tier so the grid can measure
that number honestly, and can never let it be confused with resolver-decision
coverage (the intent grid) or terminal coverage (the outcomes dashboard) — three
orthogonal maps, each blind to the others' half.

TWO KINDS OF ROW:
  - install tiers  — the InstallMethod.type members the container-native build
                     bakes (conda + the 9 non-conda). Proven by build_env_image.
  - build methods  — the ADOPT/AUTHORS routes that ship an image WITHOUT a
                     container-native reconstruction: adopt a biocontainer / the
                     author's own image / build the author's Dockerfile.

DRIFT GUARD. `assert_tiers_match_model()` checks the install-tier set equals the
InstallMethod.type Literal. A tier added to the union with no grid row (or a grid
row for a tier that left the union) fails the hermetic test — the same ledger-vs-
source posture extract_outcomes takes on the outcome enum. Adding a tier to the
model is therefore not optional bookkeeping here; it is what keeps the breadth
meter honest.

FLOORS are a REVIEWED ratchet, kept in CODE (not auto-written by the generator),
exactly like the intent corpus's `expect`: the generator re-measures
`passed/attempts`; a human raises a floor only after a tier's real build lands.
The ratchet test asserts `floor <= measured` for every measured tier and
`floor == 0` for every unmeasured one — so a floor can only ever be earned, and a
regressed measurement reddens.
"""
from __future__ import annotations

import sys
import typing
from pathlib import Path

# Make `agent` importable whether this module is run as a script or imported by
# the generator / hermetic test (pytest already has the repo root on the path).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

# ── the tiers ────────────────────────────────────────────────────────────────
# Each install tier with `builder == "container_native"` carries a `build`
# recipe the generator feeds to env_freeze.build_env_image. A tier with
# `builder is None` is DECLARED but not yet wired to a real probe — its row shows
# "unmeasured" (floor 0), to be promoted in a later slice. Probes PIN a
# tag/commit so a rebuild is reproducible.

FREEZE_TIERS: list[dict] = [
    {
        "tier": "conda", "kind": "install", "builder": "container_native",
        "probe_tool": "pigz",
        "build": {"conda_deps": ["pigz"], "primary_tools": ["pigz"]},
        "note": "pixi engine lock (URL + sha256). The proven seed "
                "(also L15/test_real_container_build.py).",
    },
    {
        "tier": "source", "kind": "install", "builder": "container_native",
        "probe_tool": "seqtk",
        "build": {
            "install_method": {
                "type": "source",
                "source": "https://github.com/lh3/seqtk",
                "commit_sha": "ae7defa8bead3ef77d241f12194dc66acdd40fca",  # v1.4
                "build_command": "make",
                "bin_path": "seqtk",
            },
            "primary_tools": ["seqtk"],
        },
        "note": "the SHARED long-tail ContainerBuild.run executor that binary / "
                "jar / cargo / go / perl / synthesized also ride — highest "
                "leverage single build. Proving it is necessary-but-not-"
                "sufficient for those siblings (each has its own spec/toolchain).",
    },
    # ── declared, not yet baked on real bytes (floor 0 until their slice) ──────
    {"tier": "pip", "kind": "install", "builder": None, "probe_tool": "",
     "note": "flagless → pixi engine (in-lock); flag-bearing → engine-coupled "
             "longtail RUN. Mocked-only today."},
    {"tier": "r_install", "kind": "install", "builder": None, "probe_tool": "",
     "note": "cran / bioconductor / github:owner/repo → Rscript install + "
             "requireNamespace||stop() evidence. Mapping unit only today."},
    {"tier": "binary", "kind": "install", "builder": None, "probe_tool": "",
     "note": "re-fetch + sha256 firewall + wrapper. Mocked-only. A darwin→linux "
             "asset is a CORRECT unanchored_cross_platform refusal, not a fail."},
    {"tier": "jar", "kind": "install", "builder": None, "probe_tool": "",
     "note": "JRE-ensure + jar download + java -jar wrapper. No build test today."},
    {"tier": "cargo", "kind": "install", "builder": None, "probe_tool": "",
     "note": "cargo install --root --locked, engine rust toolchain. Unit only."},
    {"tier": "go", "kind": "install", "builder": None, "probe_tool": "",
     "note": "GOBIN go install, engine go toolchain. Unit only."},
    {"tier": "perl", "kind": "install", "builder": None, "probe_tool": "",
     "note": "cpanm --notest + xlocale shim for XS against conda perl. Unit only."},
    {"tier": "synthesized", "kind": "install", "builder": None, "probe_tool": "",
     "note": "provenance-gated command sequence via the shared longtail executor. "
             "Fully wired, zero real-docker coverage — ranked highest, least proven."},
    # ── build methods (adopt / authors — no container-native reconstruction) ──
    {"tier": "adopt-biocontainer", "kind": "build_method", "builder": None,
     "probe_tool": "",
     "note": "pull a BioContainer by manifest digest + in-image evidence. The "
             "DEFAULT production path, yet 0 real-bytes coverage (all tests mock "
             "the digest-pull)."},
    {"tier": "adopt-image", "kind": "build_method", "builder": None,
     "probe_tool": "",
     "note": "freeze_from_image on the author's own image. L15 proves the method "
             "wrapper on a toy debian shell tool — NOT a real package install."},
    {"tier": "authors-dockerfile", "kind": "build_method", "builder": None,
     "probe_tool": "",
     "note": "build_env_from_authors_recipe (docker build -f + build-args). L15 "
             "proves it on a toy tool — NOT a real tool's Dockerfile."},
]

INSTALL_TIERS: list[str] = [t["tier"] for t in FREEZE_TIERS if t["kind"] == "install"]
BUILD_METHODS: list[str] = [t["tier"] for t in FREEZE_TIERS if t["kind"] == "build_method"]
ALL_TIERS: list[str] = [t["tier"] for t in FREEZE_TIERS]


# ── reviewed floors (the ratchet — raised by a human, never by the generator) ─
FLOORS: dict[str, float] = {
    "conda":  1.0,   # L15/pigz + the generator
    "source": 1.0,   # seqtk v1.4 — proven 2026-07-20 (this slice)
    "pip":                0.0,
    "r_install":          0.0,
    "binary":             0.0,
    "jar":                0.0,
    "cargo":              0.0,
    "go":                 0.0,
    "perl":               0.0,
    "synthesized":        0.0,
    "adopt-biocontainer": 0.0,
    "adopt-image":        0.0,
    "authors-dockerfile": 0.0,
}


def tier(name: str) -> dict:
    """The FREEZE_TIERS row for `name` (raises if unknown — a typo shouldn't
    silently measure nothing)."""
    for t in FREEZE_TIERS:
        if t["tier"] == name:
            return t
    raise KeyError(f"unknown freeze tier {name!r}; known: {ALL_TIERS}")


def buildable_install_types() -> list[str]:
    """The InstallMethod.type Literal members — the tiers the container-native
    build can bake. Read from the model so the grid can't drift from it."""
    from agent.models.core_data import InstallMethod
    ann = InstallMethod.model_fields["type"].annotation
    args = list(typing.get_args(ann))
    if not args:
        raise AssertionError(
            "could not read the InstallMethod.type Literal — the model shape "
            "changed; the tier-grid drift guard needs updating")
    return args


def assert_tiers_match_model() -> None:
    """The drift guard. The grid's install-tier set MUST equal the
    InstallMethod.type union — no more, no less. A new tier in the union with no
    grid row is a breadth hole the meter would silently omit; a grid row for a
    tier no longer in the union is a ghost."""
    model = set(buildable_install_types())
    grid = set(INSTALL_TIERS)
    missing = model - grid   # in the model, no grid row
    extra = grid - model     # grid row, not in the model
    assert not missing, (
        f"{sorted(missing)} are InstallMethod.type members with NO freeze-tier "
        f"grid row — add them to FREEZE_TIERS so the breadth meter counts them.")
    assert not extra, (
        f"{sorted(extra)} are freeze-tier grid rows that are NOT InstallMethod.type "
        f"members — they left the model; remove the ghost rows.")
    # floors must cover exactly every tier (install + build method)
    fl = set(FLOORS)
    all_t = set(ALL_TIERS)
    assert fl == all_t, (
        f"FLOORS must cover exactly every tier. missing={sorted(all_t - fl)} "
        f"extra={sorted(fl - all_t)}")


if __name__ == "__main__":
    assert_tiers_match_model()
    print(f"freeze tiers OK — {len(INSTALL_TIERS)} install tiers "
          f"+ {len(BUILD_METHODS)} build methods; model set matches.")
    for t in FREEZE_TIERS:
        wired = "real-probe" if t["builder"] else "unmeasured"
        print(f"  {t['tier']:20s} {t['kind']:13s} {wired:11s} floor={FLOORS[t['tier']]}")
