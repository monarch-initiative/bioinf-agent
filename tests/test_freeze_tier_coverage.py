"""
Hermetic ratchet + integrity tests for the freeze-tier coverage grid
(scripts/freeze_tiers.py + measure_freeze_tier_coverage.py + render_freeze_tier_grid.py,
projected into docs/freeze_tier_coverage.json + docs/freeze_tier_grid.html).

These are the CI half of the grid's two-half split (mirroring the outcomes
dashboard): the REAL builds are expensive, opt-in, and Docker-bound, so the free
CI can't run them — it gates on the committed JSON instead. Every test here is a
pure read of that JSON (or exercises the generator's PURE classifier with fakes),
so all run under CI's `-m "not live and not integration_docker"` selection.

What they enforce, each bought by a real failure mode the recon named:
  - the grid's tier set can't drift from InstallMethod.type (a new tier with no
    row would silently vanish from the breadth meter);
  - a floor can only be EARNED — an unmeasured tier can't claim one, and
    floor ≤ measured for every measured tier (a regressed build reddens);
  - the generator can NEVER record a Docker-less green (the trap L15 guards);
  - the render is deterministic, self-contained, and loudly flags staleness
    (real builds can't refresh in CI, so the page can age between opt-in runs).
"""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "docs" / "freeze_tier_coverage.json"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ft():
    return _load("freeze_tiers")


def _gen():
    return _load("measure_freeze_tier_coverage")


def _render_mod():
    return _load("render_freeze_tier_grid")


def _data() -> dict:
    assert DATA_PATH.exists(), (
        "docs/freeze_tier_coverage.json is missing — run "
        "python scripts/measure_freeze_tier_coverage.py")
    return json.loads(DATA_PATH.read_text())


# ── drift guard ───────────────────────────────────────────────────────────────

@pytest.mark.integration
def test_grid_tier_set_matches_the_install_method_model():
    """The grid's install-tier set MUST equal the InstallMethod.type union, and
    FLOORS must cover exactly every tier. A tier added to the model with no grid
    row is a breadth hole the meter would silently omit."""
    ft = _ft()
    ft.assert_tiers_match_model()   # raises on any drift
    data = _data()
    tiers = set(data["tiers"])
    assert tiers == set(ft.ALL_TIERS), (
        f"committed JSON tier keys drifted from FREEZE_TIERS: "
        f"missing={sorted(set(ft.ALL_TIERS) - tiers)} "
        f"extra={sorted(tiers - set(ft.ALL_TIERS))}. Re-run the generator.")


# ── the ratchet: a floor is EARNED, never claimed ─────────────────────────────

@pytest.mark.integration
def test_a_floor_above_zero_is_backed_by_a_proven_measurement():
    """A reviewed floor > 0 is a PROMISE that the tier really builds. It must be
    backed by a committed `proven` measurement at or above that rate — otherwise
    it's an unbacked claim (the exact 'green with nothing behind it' the grid
    exists to prevent). An unmeasured tier must be floored 0."""
    ft = _ft()
    data = _data()
    for t in ft.ALL_TIERS:
        floor = ft.FLOORS[t]
        rec = data["tiers"][t]
        rate = (rec["passed"] / rec["attempts"]) if rec["attempts"] else None
        if floor > 0:
            assert rec["status"] == "proven", (
                f"tier '{t}' has floor {floor} but its committed status is "
                f"'{rec['status']}' — a floor must be backed by a proven build.")
            assert rate is not None and rate >= floor, (
                f"tier '{t}': measured rate {rate} < floor {floor} — a regressed "
                f"build. Re-run the generator; if the tier really broke, fix it "
                f"before lowering the floor.")
        if rec["status"] == "unmeasured":
            assert floor == 0.0, (
                f"tier '{t}' is unmeasured but floored {floor}: you can't ratchet "
                f"a tier you never built.")


@pytest.mark.integration
def test_measured_tiers_never_fall_below_their_floor():
    """The monotonic ratchet, stated over every MEASURED tier: floor ≤ rate. This
    is what makes a regression a red build rather than a silent slide."""
    ft = _ft()
    data = _data()
    for t, rec in data["tiers"].items():
        if not rec["attempts"]:
            continue
        rate = rec["passed"] / rec["attempts"]
        assert rate >= ft.FLOORS[t], (
            f"tier '{t}': rate {rate} < floor {ft.FLOORS[t]}")


# ── the committed JSON is well-formed + honestly stamped ──────────────────────

@pytest.mark.integration
def test_committed_measurements_are_well_formed_and_stamped():
    ft = _ft()
    data = _data()
    for key in ("measured_on", "git_sha", "platform", "host_arch"):
        assert str(data.get(key) or "").strip(), f"missing/empty stamp field {key!r}"
    proven = 0
    for t, rec in data["tiers"].items():
        for f in ("kind", "attempts", "passed", "status", "last_error"):
            assert f in rec, f"tier '{t}' record missing field {f!r}"
        assert 0 <= rec["passed"] <= rec["attempts"], f"tier '{t}': passed>attempts"
        if rec["attempts"] == 0:
            assert rec["status"] == "unmeasured" and rec["passed"] == 0, \
                f"tier '{t}': attempts 0 must be unmeasured with 0 passed"
        else:
            assert rec["status"] in ("proven", "broke"), f"tier '{t}': bad status"
        if rec["status"] == "proven" and rec["kind"] == "install":
            proven += 1
    # the headline the JSON advertises must match the records (the generator
    # can't over-claim its own breadth count).
    assert data.get("install_tiers_proven") == proven, (
        f"install_tiers_proven={data.get('install_tiers_proven')} but {proven} "
        f"install tiers are actually 'proven'")
    assert data.get("install_tiers_total") == len(ft.INSTALL_TIERS)


# ── the generator can NEVER record a Docker-less green (pure) ──────────────────

@pytest.mark.integration
def test_generator_never_records_a_docker_less_green():
    """tier_record is the honesty seam. No Docker at measurement time ⇒ the tier
    is 'unmeasured' and never 'passed', even if some stale `outcome` is handed in.
    A declared (builder None) tier is unmeasured regardless. Only a real, clean
    build outcome under a live daemon becomes 'proven'."""
    gen = _gen()
    real = {"tier": "source", "kind": "install", "builder": "container_native",
            "probe_tool": "seqtk", "note": "n"}
    declared = {"tier": "pip", "kind": "install", "builder": None,
                "probe_tool": "", "note": "n"}
    green = {"ok": True, "error": None, "image_digest": "sha256:x",
             "content_digest": "sha256:y", "validation_locus": "emulated"}
    red = {"ok": False, "error": "boom", "image_digest": None,
           "content_digest": None, "validation_locus": None}

    # Docker DOWN → unmeasured, never passed, even with a (stale) green outcome.
    r = gen.tier_record(real, green, docker_available=False)
    assert r["status"] == "unmeasured" and r["passed"] == 0 and r["attempts"] == 0

    # declared tier (no probe) → unmeasured regardless of Docker.
    r = gen.tier_record(declared, None, docker_available=True)
    assert r["status"] == "unmeasured" and r["passed"] == 0

    # a real, clean build under a live daemon → proven.
    r = gen.tier_record(real, green, docker_available=True)
    assert r["status"] == "proven" and r["passed"] == 1 and r["attempts"] == 1
    assert r["validation_locus"] == "emulated" and r["image_digest"] == "sha256:x"

    # a real build that FAILED the contract → broke, error carried, not passed.
    r = gen.tier_record(real, red, docker_available=True)
    assert r["status"] == "broke" and r["passed"] == 0 and r["last_error"] == "boom"


# ── the render is deterministic, self-contained, and flags staleness ──────────

@pytest.mark.integration
def test_render_is_deterministic_self_contained_and_present():
    import re
    ft = _ft()
    rd = _render_mod()
    data = _data()
    html1 = rd.render(data, current_sha=data["git_sha"])
    html2 = rd.render(data, current_sha=data["git_sha"])
    assert html1 == html2, "render is not deterministic"
    assert "http://" not in html1 and "https://" not in html1, \
        "grid must be self-contained (no external/CDN resources)"
    assert "src=" not in html1.lower()
    assert not re.search(r"\{[a-z_]+\}", html1), "unfilled format placeholder leaked"
    for t in data["tiers"]:
        assert f">{t}<" in html1, f"tier '{t}' missing from the grid"
    # not stale when the render sha matches the measured sha
    assert "measured at" not in html1


@pytest.mark.integration
def test_render_loudly_flags_staleness_and_emptiness():
    rd = _render_mod()
    data = _data()
    # a different HEAD than the measured sha ⇒ a stale banner naming both.
    stale = rd.render(data, current_sha="deadbee")
    assert "measured at" in stale and "deadbee" in stale and data["git_sha"] in stale
    # empty data ⇒ a NOT-measured banner, no crash.
    empty = rd.render({"tiers": {}, "git_sha": "unknown"}, current_sha="abc1234")
    assert "NOT measured" in empty
