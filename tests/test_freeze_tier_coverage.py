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
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "docs" / "freeze_tier_coverage.json"
GRID_HTML = ROOT / "docs" / "freeze_tier_grid.html"

# A page is self-contained iff it FETCHES nothing external — the real property.
# NOT "the string 'https://' never appears": a broke tier's last_error routinely
# carries a URL (clone/download timeout — the tiers this grid exists to prove), and
# that harmless escaped text must not false-red the check (the #5 substring-proxy trap).
_EXTERNAL_FETCH = re.compile(r'<script[^>]+\bsrc=|<link\b|@import|url\(\s*["\']?https?:', re.I)


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
    rd = _render_mod()
    data = _data()
    html1 = rd.render(data, current_sha=data["git_sha"])
    html2 = rd.render(data, current_sha=data["git_sha"])
    assert html1 == html2, "render is not deterministic"
    assert not _EXTERNAL_FETCH.search(html1), \
        "grid must be self-contained (no external/CDN resources)"
    assert not re.search(r"\{[a-z_]+\}", html1), "unfilled format placeholder leaked"
    for t in data["tiers"]:
        assert f">{t}<" in html1, f"tier '{t}' missing from the grid"
    # not stale when the render sha matches the measured sha
    assert "measured at" not in html1


@pytest.mark.integration
def test_render_row_status_matches_the_record():
    """The #5 lesson applied to the human-facing artifact: the grid must PAINT each
    tier by its real record, not merely mention its name. The old presence check
    (`>{t}<`) is a name proxy — a mutation that greens every row would sail through
    it. This maps each record's status → its rendered row class + glyph, so an
    'unmeasured' tier can never render as a green ✅ 'proven' undetected."""
    rd = _render_mod()
    data = _data()
    html = rd.render(data, current_sha=data["git_sha"])
    rows = {tier: (cls, glyph) for cls, glyph, tier in re.findall(
        r'<tr class="([^"]+)"><td class="tier"><span class="g">(.*?)</span>([\w\-]+)</td>', html)}
    assert set(rows) == set(data["tiers"]), \
        f"rows parsed ({sorted(rows)}) != records ({sorted(data['tiers'])})"
    for tier, rec in data["tiers"].items():
        cls, glyph = rows[tier]
        st = rec["status"]
        if st == "proven":
            assert "ok" in cls and glyph == "✅", f"{tier} proven but row=({cls!r},{glyph!r})"
        elif st == "unmeasured":
            assert cls == "muted" and glyph != "✅", f"{tier} unmeasured but row=({cls!r},{glyph!r})"
        elif st == "broke":
            assert "bad" in cls and glyph == "💥", f"{tier} broke but row=({cls!r},{glyph!r})"
    # Green-everything mutation guard, robust to a FULLY-GREEN grid: force one real tier to
    # 'unmeasured' in a copy and assert the render paints it muted (not ok). The old form
    # ("some real row is non-ok") silently self-disabled the moment every tier was proven —
    # exactly when the whole grid went green — so it tested the render property by leaning on
    # an incidental red row. This tests the property directly.
    probe = {**data, "tiers": {**data["tiers"],
             "conda": {**data["tiers"]["conda"], "status": "unmeasured",
                       "attempts": 0, "passed": 0, "rate": None}}}
    phtml = rd.render(probe, current_sha=probe["git_sha"])
    prows = {tier: (cls, glyph) for cls, glyph, tier in re.findall(
        r'<tr class="([^"]+)"><td class="tier"><span class="g">(.*?)</span>([\w\-]+)</td>', phtml)}
    ccls, cglyph = prows.get("conda", ("", ""))
    # unmeasured must never render as the proven green (it renders muted, or 'bad'/suspect
    # when — as here — it also carries a floor it no longer backs); either way, NOT ✅ ok.
    assert ccls != "ok" and cglyph != "✅", \
        "an unmeasured tier rendered as proven-green — the render may be painting green unconditionally"


@pytest.mark.integration
def test_a_url_in_an_error_message_does_not_trip_self_containment():
    """A broke tier's last_error routinely contains a URL (the clone/download
    timeout that is the routine failure of exactly the tiers this grid proves). The
    self-containment check must assert NO external fetch, so a harmless escaped URL
    in the error text does not false-red CI."""
    rd = _render_mod()
    data = json.loads(DATA_PATH.read_text())
    data["tiers"]["binary"] = {
        "kind": "install", "probe_tool": "mosdepth", "note": "n",
        "attempts": 1, "passed": 0, "status": "broke", "rate": 0.0,
        "image_digest": None, "content_digest": None, "validation_locus": None,
        "last_error": "curl: (28) Failed to connect to "
                      "https://github.com/brentp/mosdepth: Connection timed out"}
    html = rd.render(data, current_sha=data["git_sha"])
    assert "github.com/brentp/mosdepth" in html, "the error text should be shown"
    assert not _EXTERNAL_FETCH.search(html), \
        "a URL inside escaped error text must not count as an external resource"


@pytest.mark.integration
def test_render_degrades_on_a_malformed_record_instead_of_crashing():
    """A partially-written / hand-edited / merge-conflicted record (missing 'status'
    or 'passed') must render a muted '?' row, not raise KeyError and blank the page."""
    rd = _render_mod()
    r1 = rd.render({"git_sha": "abc1234",
                    "tiers": {"conda": {"kind": "install", "probe_tool": "pigz", "attempts": 1}}},
                   current_sha="abc1234")
    assert ">conda<" in r1 and "?" in r1               # missing 'status' → muted '?'
    r2 = rd.render({"git_sha": "abc1234",
                    "tiers": {"conda": {"kind": "install", "status": "proven", "attempts": 1}}},
                   current_sha="abc1234")
    assert ">conda<" in r2                             # missing 'passed' → no crash


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


@pytest.mark.integration
def test_committed_html_is_a_faithful_render_of_the_committed_json():
    """The committed HTML must equal a render of the committed JSON at the JSON's own
    measured sha — so neither can be hand-edited without the other, and the page a
    human opens can't diverge from the measurements it claims to project."""
    rd = _render_mod()
    data = _data()
    expected = rd.render(data, current_sha=data["git_sha"])
    committed = GRID_HTML.read_text()
    assert committed == expected, (
        "docs/freeze_tier_grid.html is out of sync with freeze_tier_coverage.json — "
        "re-run scripts/measure_freeze_tier_coverage.py (or render_freeze_tier_grid.py).")


# ── incremental measurement: per-tier sha + carry-forward + fingerprint ───────

@pytest.mark.integration
def test_recipe_fingerprint_is_stable_and_recipe_specific():
    """The anchor of incremental measurement: the fingerprint is deterministic, differs
    per recipe, and is a constant for a tier with no build recipe. Editing a recipe must
    change it (that is what auto-invalidates a carried measurement)."""
    ft = _ft()
    fp = ft.recipe_fingerprint(ft.tier("perl"))
    assert fp == ft.recipe_fingerprint(ft.tier("perl"))            # stable
    assert fp != ft.recipe_fingerprint(ft.tier("cargo"))          # recipe-specific
    # a mutated recipe → a different fingerprint (the invalidation trigger)
    mutated = {**ft.tier("perl"), "build": {**ft.tier("perl")["build"], "primary_tools": ["Other"]}}
    assert ft.recipe_fingerprint(mutated) != fp
    # a spec with NO build recipe fingerprints to a constant (two empties agree)…
    assert ft.recipe_fingerprint({"tier": "x"}) == ft.recipe_fingerprint({"tier": "y"})
    # …and every WIRED row — now including the build-method rows (adopt/authors), which carry
    # a real `build` recipe since this slice — differs from that empty constant.
    assert ft.recipe_fingerprint(ft.tier("adopt-image")) != ft.recipe_fingerprint({"tier": "x"})


@pytest.mark.integration
def test_carry_forward_preserves_the_original_measured_sha():
    """THE anti-laundering rule (the bug that got --keep removed): a carried record keeps
    its ORIGINAL measured_sha and is flagged carried_forward — never re-stamped as if it
    were rebuilt this run. That is what lets the render show a carried tier's real age."""
    gen, ft = _gen(), _ft()
    fp = ft.recipe_fingerprint(ft.tier("cargo"))
    prev = {"kind": "install", "status": "proven", "attempts": 1, "passed": 1, "rate": 1.0,
            "recipe_fingerprint": fp, "measured_sha": "old1234", "measured_on": "2026-01-01T00:00:00Z",
            "image_digest": "sha256:c", "validation_locus": "emulated", "last_error": None}
    r = gen.carry_forward(ft.tier("cargo"), prev, recipe_changed=False, current_fp=fp)
    assert r["status"] == "proven" and r["passed"] == 1
    assert r["measured_sha"] == "old1234", "carry-forward must NOT re-stamp the sha"
    assert r["carried_forward"] is True and r["fingerprint_backfilled"] is False


@pytest.mark.integration
def test_carry_forward_only_carries_a_real_prior_measurement():
    """Nothing is conjured: a missing / unmeasured / attempts-0 prior carries forward as
    'unmeasured', never as proven."""
    gen, ft = _gen(), _ft()
    fp = ft.recipe_fingerprint(ft.tier("go"))
    assert gen.carry_forward(ft.tier("go"), None, recipe_changed=False, current_fp=fp)["status"] == "unmeasured"
    placeholder = {"status": "unmeasured", "attempts": 0, "passed": 0}
    assert gen.carry_forward(ft.tier("go"), placeholder, recipe_changed=False,
                             current_fp=fp)["status"] == "unmeasured"


@pytest.mark.integration
def test_carry_forward_invalidates_a_changed_recipe():
    """'Retroactively fix a tier' safety: once its recipe changes, a carried measurement
    can no longer count as proven — it reverts to unmeasured with a self-explaining error,
    so the floor-earned ratchet reddens it until a fresh bake."""
    gen, ft = _gen(), _ft()
    prev = {"kind": "install", "status": "proven", "attempts": 1, "passed": 1, "rate": 1.0,
            "recipe_fingerprint": "oldfp", "measured_sha": "old1234", "last_error": None}
    r = gen.carry_forward(ft.tier("binary"), prev, recipe_changed=True, current_fp="newfp")
    assert r["status"] == "unmeasured" and r["passed"] == 0
    assert "recipe changed" in (r["last_error"] or "")


@pytest.mark.integration
def test_carry_forward_grandfathers_a_pre_fingerprint_record_visibly():
    """The one-time migration: a proven prior with NO fingerprint is carried forward with
    the missing sha backfilled and a VISIBLE fingerprint_backfilled flag — the grandfathering
    is disclosed, and from then on the record carries a fingerprint."""
    gen, ft = _gen(), _ft()
    fp = ft.recipe_fingerprint(ft.tier("source"))
    prev = {"kind": "install", "status": "proven", "attempts": 1, "passed": 1, "rate": 1.0,
            "measured_sha": None, "image_digest": "sha256:s", "validation_locus": "emulated",
            "last_error": None}   # NO recipe_fingerprint (pre-migration)
    r = gen.carry_forward(ft.tier("source"), prev, recipe_changed=False, current_fp=fp,
                          grandfather_sha="887e655")
    assert r["status"] == "proven" and r["measured_sha"] == "887e655"
    assert r["fingerprint_backfilled"] is True and r["recipe_fingerprint"] == fp


@pytest.mark.integration
def test_recipe_unchanged_since_reads_the_baseline_recipe_via_git(monkeypatch):
    """The migration verifier: it must fingerprint the tier's recipe AS OF the baseline
    commit (read via `git show`) and compare — NOT trust. A git failure returns False
    (conservative: re-measure), so a hiccup can only cost a rebuild, never launder."""
    gen, ft = _gen(), _ft()
    conda_build = ft.tier("conda")["build"]
    baseline_src = ("FREEZE_TIERS = [{'tier':'conda','kind':'install',"
                    f"'builder':'container_native','build':{conda_build!r}}}]")

    class _R:
        def __init__(self, rc, out): self.returncode, self.stdout = rc, out
    monkeypatch.setattr(gen.subprocess, "run", lambda *a, **k: _R(0, baseline_src))
    fp = ft.recipe_fingerprint(ft.tier("conda"))
    assert gen._recipe_unchanged_since("basesha", "conda", fp) is True       # identical recipe
    assert gen._recipe_unchanged_since("basesha", "conda", "somethingelse") is False
    # a git failure is conservative → False (re-measure), never a laundered True
    monkeypatch.setattr(gen.subprocess, "run", lambda *a, **k: _R(128, ""))
    assert gen._recipe_unchanged_since("basesha", "conda", fp) is False


@pytest.mark.integration
def test_main_measures_one_tier_and_carries_the_rest_forward(tmp_path, monkeypatch):
    """THE #5 lesson applied: drive the REAL entry point (main()), not just the seam. An
    incremental `--only` run must (a) freshly measure the named tier at HEAD, and (b) carry
    every other proven tier forward UNCHANGED, keeping its original measured_sha — the whole
    point of incremental measurement (no full N-tier re-sweep to add or fix one tier)."""
    gen, ft = _gen(), _ft()
    out = (tmp_path / "grid.json").resolve()
    # a prior grid with fingerprinted proven records for every WIRED tier, measured @old9999
    prior_tiers = {}
    for spec in ft.FREEZE_TIERS:
        t = spec["tier"]
        if spec.get("builder"):
            prior_tiers[t] = {"kind": spec["kind"], "status": "proven", "attempts": 1,
                              "passed": 1, "rate": 1.0, "measured_sha": "old9999",
                              "recipe_fingerprint": ft.recipe_fingerprint(spec),
                              "image_digest": "sha256:z", "validation_locus": "emulated",
                              "last_error": None, "carried_forward": False}
        else:
            prior_tiers[t] = {"kind": spec["kind"], "status": "unmeasured", "attempts": 0,
                              "passed": 0, "rate": None, "last_error": None}
    out.write_text(json.dumps({"git_sha": "old9999", "measured_on": "x", "tiers": prior_tiers}))

    monkeypatch.setattr(gen, "_docker_up", lambda: True)
    monkeypatch.setattr(gen, "_git", lambda *a: "new5678")
    monkeypatch.setattr(gen, "build_tier",
                        lambda spec: {"ok": True, "error": None, "image_digest": "sha256:fresh",
                                      "content_digest": "sha256:c", "validation_locus": "emulated"})
    monkeypatch.setattr(gen, "_render", lambda data: None)

    rc = gen.main(["--only", "cargo", "--out", str(out)])
    assert rc == 0
    result = json.loads(out.read_text())["tiers"]
    # cargo: freshly measured at the new HEAD, not carried
    assert result["cargo"]["status"] == "proven"
    assert result["cargo"]["measured_sha"] == "new5678"
    assert result["cargo"]["carried_forward"] is False
    assert result["cargo"]["image_digest"] == "sha256:fresh"
    # every other wired tier: carried forward, ORIGINAL sha preserved (never re-stamped)
    for t, rec in result.items():
        if t != "cargo" and ft.tier(t).get("builder"):
            assert rec["carried_forward"] is True, f"{t} should be carried"
            assert rec["measured_sha"] == "old9999", f"{t} sha must be preserved, not re-stamped"
            assert rec["status"] == "proven"


@pytest.mark.integration
def test_render_discloses_carry_forward_provenance():
    """The render must SHOW a carried tier's provenance (carried @sha), so a reader can
    see it wasn't rebuilt this run — the staleness signal that carry-forward must never hide."""
    rd, ft = _render_mod(), _ft()
    data = {"git_sha": "head1", "measured_on": "x", "platform": "linux/amd64",
            "host_arch": "arm64", "docker_available": True,
            "install_tiers_total": len(ft.INSTALL_TIERS), "install_tiers_proven": 1,
            "tiers": {"conda": {"kind": "install", "probe_tool": "pigz", "note": "n",
                                "status": "proven", "attempts": 1, "passed": 1, "rate": 1.0,
                                "validation_locus": "emulated", "measured_sha": "old7777",
                                "carried_forward": True, "fingerprint_backfilled": False,
                                "last_error": None}}}
    html = rd.render(data, current_sha="head1")
    assert "carried @old7777" in html


@pytest.mark.integration
def test_a_docker_less_run_refuses_to_overwrite_the_committed_grid(tmp_path, monkeypatch):
    """The write-boundary honesty guard: a Docker-less run measures nothing, so it
    must refuse to touch the committed grid (no silent clobber, no laundering a stale
    green). --force writes a deliberate all-unmeasured file (which reddens the ratchet)."""
    gen = _gen()
    canonical = (tmp_path / "freeze_tier_coverage.json").resolve()
    canonical.write_text(DATA_PATH.read_text())        # a real prior with proven tiers
    before = canonical.read_text()
    monkeypatch.setattr(gen, "OUT", canonical)
    monkeypatch.setattr(gen, "_docker_up", lambda: False)
    monkeypatch.setattr(gen, "_render", lambda data: None)   # don't touch real docs/

    rc = gen.main(["--out", str(canonical)])
    assert rc == 1, "a docker-less canonical write must refuse"
    assert canonical.read_text() == before, "the committed grid must be left untouched"

    rc2 = gen.main(["--out", str(canonical), "--force"])
    assert rc2 == 0, "--force must write a deliberate all-unmeasured file"
    forced = json.loads(canonical.read_text())
    assert all(r["status"] == "unmeasured" for r in forced["tiers"].values()), \
        "a forced docker-less run must record every tier unmeasured — never proven"
    assert forced["install_tiers_proven"] == 0
