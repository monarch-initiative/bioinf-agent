"""Tests for scripts/freeze_tiers.py — the DECLARATION of the install/build-method tiers.

freeze_tiers.py enumerates every tier as data (tier(), ALL_TIERS, INSTALL_TIERS,
FREEZE_TIERS, BUILD_METHODS, FLOORS) plus two pure helpers: assert_tiers_match_model()
and recipe_fingerprint(). This file tests exactly that — the declaration and the lint.

It is the residue of tests/test_freeze_tier_coverage.py, which was deleted along with the
freeze-tier meter (scripts/measure_freeze_tier_coverage.py, scripts/render_freeze_tier_grid.py
and the committed docs/freeze_tier_* artifacts). 18 of that file's 20 tests were tests OF the
meter — they read the committed JSON, drove the renderer, or exercised carry-forward
bookkeeping — and died with it. These two survived because they test freeze_tiers.py itself
and touch no measurement.

The load-bearing one is the DRIFT LINT. assert_tiers_match_model() reads the
InstallMethod.type Literal off the live model and refuses a tier row that drifted from it in
either direction. It had exactly ONE caller in the whole suite; rehoming it here is what
stops the meter's deletion from silently retiring the guard too.
"""
import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ft():
    return _load("freeze_tiers")


# ── drift guard ───────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_the_tier_set_matches_the_install_method_model():
    """The declared install-tier set MUST equal the InstallMethod.type union, and FLOORS
    must cover exactly every tier. A tier added to the model with no declared row (or a
    row naming a type the model dropped) is drift between the code and its own map."""
    _ft().assert_tiers_match_model()   # raises on any drift


# ── the recipe fingerprint ────────────────────────────────────────────────────

@pytest.mark.integration
def test_recipe_fingerprint_is_stable_and_recipe_specific():
    """The fingerprint is deterministic, differs per recipe, and is a constant for a tier
    with no build recipe. Editing a recipe must change it."""
    ft = _ft()
    fp = ft.recipe_fingerprint(ft.tier("perl"))
    assert fp == ft.recipe_fingerprint(ft.tier("perl"))            # stable
    assert fp != ft.recipe_fingerprint(ft.tier("cargo"))          # recipe-specific
    # a mutated recipe → a different fingerprint
    mutated = {**ft.tier("perl"), "build": {**ft.tier("perl")["build"], "primary_tools": ["Other"]}}
    assert ft.recipe_fingerprint(mutated) != fp
    # a spec with NO build recipe fingerprints to a constant (two empties agree)…
    assert ft.recipe_fingerprint({"tier": "x"}) == ft.recipe_fingerprint({"tier": "y"})
    # …and every WIRED row — including the build-method rows (adopt/authors), which carry
    # a real `build` recipe — differs from that empty constant.
    assert ft.recipe_fingerprint(ft.tier("adopt-image")) != ft.recipe_fingerprint({"tier": "x"})
