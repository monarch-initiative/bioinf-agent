"""
Identity-to-disk — the `tool_identities[]` disclosure (audit finding #8).

WHY THIS FILE EXISTS. The Layer-1 honesty contract proves a tool WORKS
(VALIDATED_IN_IMAGE) but never proved it is the tool you MEANT. `resolve()` computed
each tool's self-description ("cellranger" -> CRAN's "Translate Spreadsheet Cell Ranges")
and it died at resolve time — no frozen record, ENV.html, attestation, or recipe carried
it, so a human reading a frozen env could not catch a wrong-domain adoption. This is the
persistence half: freeze captures the tool's OWN words and every deliverable discloses
them, labelled UNVERIFIED.

The disclosure is a liability if it lies, so two properties are load-bearing and tested:

  1. TIED TO THE INSTALL, never a bare-name guess. capture() reads the registry summary
     for the package the SBOM says actually shipped. A tool installed from a non-registry
     tier (binary/source/jar) gets self_description=None — honest silence, NOT a borrowed
     identity from a same-named squatter (the astronomy `dorado` must not label the ONT one).
  2. THE SEAM. `ToolIdentity` is validated at BOTH ends — `EnvCache.register` on write and
     `check_build` WELL_FORMED on serve — the same forbid-extras / no-fabricated-defaults
     discipline that `ShippedBinary` earned. A record we cannot read is never rendered.

Each test names what to break to watch it go red.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent.models.core_data import ToolIdentity, tool_identities
from agent.skills import tool_identity as TI


# ── 1. the model seam ───────────────────────────────────────────────────────────

def test_valid_identity_parses():
    ti = ToolIdentity.model_validate({"tool": "samtools", "self_description": "SAM utils",
                                      "source": "conda", "package": "samtools", "version": "1.21"})
    assert ti.tool == "samtools" and ti.source == "conda"


def test_extra_keys_forbidden():
    """A producer's private dialect must be a write-time error, not a silent extra field."""
    with pytest.raises(ValidationError):
        ToolIdentity.model_validate({"tool": "x", "self_description": None, "source": None,
                                     "package": None, "version": None, "confidence": 0.9})


def test_no_fabricating_defaults():
    """Every field REQUIRED — `None` is a value the producer must STATE, never defaulted in."""
    with pytest.raises(ValidationError):
        ToolIdentity.model_validate({"tool": "x"})   # self_description/source/... absent


def test_empty_tool_rejected():
    with pytest.raises(ValidationError):
        ToolIdentity.model_validate({"tool": "", "self_description": None, "source": None,
                                     "package": None, "version": None})


def test_reader_grandfathers_absent_key():
    """A record frozen before identity-to-disk existed has no key -> [] (re-freeze, not backfill)."""
    assert tool_identities({"name": "old"}) == []


# ── 2. capture(): tied to the install, exhaustive, best-effort ──────────────────

_SBOM = [
    {"name": "samtools", "version": "1.21", "kind": "conda"},
    {"name": "r-seurat", "version": "5.0.1", "kind": "conda"},
    {"name": "pysam", "version": "0.22", "kind": "pypi"},
]


@pytest.fixture
def fake_registry(monkeypatch):
    from agent.skills import resolver as R
    monkeypatch.setattr(R, "probe_conda",
                        lambda name, timeout=12: {"available": True, "summary": f"CONDA::{name}"})
    monkeypatch.setattr(R, "probe_pypi",
                        lambda name, timeout=12: {"available": True, "summary": f"PYPI::{name}"})
    return R


def test_capture_is_exhaustive(fake_registry):
    """Every requested tool gets a record — the disclosure is never silently short."""
    ids = TI.capture(["samtools", "seurat", "pysam", "dorado"], _SBOM)
    assert [i["tool"] for i in ids] == ["samtools", "seurat", "pysam", "dorado"]


def test_capture_ties_conda_to_conda(fake_registry):
    ids = {i["tool"]: i for i in TI.capture(["samtools"], _SBOM)}
    assert ids["samtools"]["source"] == "conda"
    assert ids["samtools"]["self_description"] == "CONDA::samtools"
    assert ids["samtools"]["version"] == "1.21"


def test_capture_matches_r_prefix(fake_registry):
    """`seurat` must resolve to the installed `r-seurat` package, not miss."""
    idn = TI.capture(["seurat"], _SBOM)[0]
    assert idn["package"] == "r-seurat"
    assert idn["self_description"] == "CONDA::r-seurat"


def test_capture_ties_pypi_to_pypi(fake_registry):
    idn = TI.capture(["pysam"], _SBOM)[0]
    assert idn["source"] == "pypi" and idn["self_description"] == "PYPI::pysam"


def test_capture_no_misattribution_for_absent_tool(fake_registry):
    """A tool absent from the SBOM entirely gets honest silence."""
    idn = TI.capture(["dorado"], _SBOM)[0]
    assert idn["self_description"] is None
    assert idn["source"] is None and idn["package"] is None


# A closure where same-named squatters ride in as transitive deps of OTHER tools:
# astronomy-PyPI `dorado` and base-R `r-cluster` are both present, but the REQUESTED
# `dorado`/`cluster` shipped from a binary/source tier and are NOT their own providers.
_SBOM_WITH_SQUATTERS = _SBOM + [
    {"name": "dorado", "version": "0.5.0", "kind": "pypi"},     # astronomy squatter
    {"name": "r-cluster", "version": "2.1.4", "kind": "conda"},  # base-R recommended dep
]


def test_capture_skips_nonregistry_tool_present_in_closure(fake_registry):
    """THE trap the review caught: a binary/source-tier tool whose name ALSO appears in the
    transitive closure must still get None — never the squatter's blurb. Break this by
    dropping the nonregistry_tools guard and watch an ONT `dorado` binary get astronomy-PyPI's
    summary, or a source-built `cluster` get base-R `r-cluster`'s."""
    dorado = TI.capture(["dorado"], _SBOM_WITH_SQUATTERS, nonregistry_tools=["dorado"])[0]
    assert dorado["self_description"] is None and dorado["package"] is None
    cluster = TI.capture(["cluster"], _SBOM_WITH_SQUATTERS, nonregistry_tools=["cluster"])[0]
    assert cluster["self_description"] is None and cluster["package"] is None


def test_capture_registry_tool_still_matches_alongside_squatters(fake_registry):
    """The guard is targeted: a genuine registry-tier tool still resolves even when the
    closure also holds squatters — only the NON-registry tools are silenced."""
    idn = TI.capture(["samtools"], _SBOM_WITH_SQUATTERS, nonregistry_tools=["dorado"])[0]
    assert idn["source"] == "conda" and idn["self_description"] == "CONDA::samtools"


def test_capture_is_best_effort(monkeypatch):
    """A probe that raises must leave self_description None, never fail the freeze."""
    from agent.skills import resolver as R
    def boom(name, timeout=12):
        raise RuntimeError("network down")
    monkeypatch.setattr(R, "probe_conda", boom)
    idn = TI.capture(["samtools"], _SBOM)[0]
    assert idn["self_description"] is None


def test_captured_dicts_satisfy_the_model(fake_registry):
    """capture() states facts; the seam enforces the shape — the two must agree."""
    for d in TI.capture(["samtools", "seurat", "pysam", "dorado"], _SBOM):
        ToolIdentity.model_validate(d)   # raises if capture ever emits an off-shape dict


# ── 3. the dual seam (write + serve) ────────────────────────────────────────────

def _record(identities):
    # requested_tools mirrors the identities' tools — ENV.html only renders a disclosure
    # sub-row for a REQUESTED tool, so a fixture must request what it discloses.
    tools = [i["tool"] for i in identities if i.get("tool")]
    return {
        "name": "demo", "image": "demo@sha256:abc", "image_digest": "sha256:abc",
        "requested_tools": tools or ["samtools"], "resolved_packages": _SBOM,
        "shipped_binaries": [], "tool_identities": identities,
    }


_GOOD_ID = {"tool": "cellranger", "self_description": "Translate Spreadsheet Cell Ranges",
            "source": "conda", "package": "cellranger", "version": "1.1.0"}
_BAD_ID = {"tool": "x", "OOPS": 1}


def test_register_accepts_good_and_rejects_malformed():
    from agent.skills.freeze import EnvCache
    cache = EnvCache(Path(tempfile.mkdtemp()) / "cache.json")
    cache.register("k", _record([_GOOD_ID]))          # must not raise
    with pytest.raises(ValidationError):
        cache.register("k2", _record([_BAD_ID]))      # producer bug is loud at the seam


def test_check_build_wellformed_flags_malformed_identity():
    from agent.skills import env_honesty
    viols = [v for v in env_honesty.check_build(_record([_BAD_ID]))
             if v.get("where") == "tool_identities"]
    assert len(viols) == 1 and viols[0]["invariant"] == "WELL_FORMED.tool_identities"


def test_check_build_wellformed_passes_good_identity():
    from agent.skills import env_honesty
    viols = [v for v in env_honesty.check_build(_record([_GOOD_ID]))
             if v.get("where") == "tool_identities"]
    assert viols == []


# ── 4. the renders — every deliverable discloses it, labelled unverified ─────────

def test_env_html_discloses_labelled_unverified():
    from agent.skills.env_report_html import render_env_report_html
    html = render_env_report_html(_record([_GOOD_ID]))
    assert "Translate Spreadsheet Cell Ranges" in html
    assert "describes it as" in html and "Not verified by this report" in html


def test_env_html_omits_row_when_no_description():
    """A tool with no self_description gets no sub-row (absence is not fabricated as data)."""
    from agent.skills.env_report_html import render_env_report_html
    none_id = {"tool": "samtools", "self_description": None, "source": None,
               "package": None, "version": None}
    html = render_env_report_html(_record([none_id]))
    assert "describes it as" not in html


def test_recipe_md_section_and_sanitization():
    from agent.skills.env_recipe_render import render_recipe_markdown, _md_inline
    recipe = {"name": "demo", "primary_tools": ["cellranger"],
              "build_method": "container-native-build", "tool_identities": [_GOOD_ID]}
    md = render_recipe_markdown(recipe, _record([_GOOD_ID]))
    assert "What each tool says it is" in md and "Translate Spreadsheet Cell Ranges" in md
    # untrusted registry text is rendered raw here — backticks/newlines defused...
    assert "`" not in _md_inline("rm -rf `pwd`") and _md_inline("a\n b") == "a b"
    # ...AND markdown link/image syntax neutralized (no auto-loading pixel / live link in a
    # doc we ship as reproducible-by-anyone). The metacharacters survive only backslash-escaped.
    out = _md_inline("![x](http://attacker/t.png) and [click](http://attacker/x.sh)")
    assert "](" not in out and "![" not in out
    assert r"\[" in out and r"\(" in out


def test_attestation_places_identity_in_internal_not_resolved_deps():
    """Identity is agent-asserted disclosure, so it lives beside the declared metadata,
    NOT in resolvedDependencies (which are verified)."""
    from agent.skills.attestation import build_attestation
    att = build_attestation(_record([_GOOD_ID]))
    bd = att["predicate"]["buildDefinition"]
    assert len(bd["internalParameters"]["tool_identities"]) == 1
    assert "Translate Spreadsheet Cell Ranges" not in str(bd["resolvedDependencies"])


# ── 5. identity by NAME is not identity — the known-source anchor ────────────────
#
# When the record knows the tool's repository (the authors' Dockerfile source, the image's
# source label), a same-named registry hit is adopted only if the registry's own metadata
# points at that repository. Otherwise the description is withheld and the collision is
# stated — the reader is told a same-named package exists elsewhere, never handed the
# other project's words. PyPI's `talos` is a Keras hyper-parameter sweep library; an env
# built from populationgenomics/talos must not be described by it.

from agent.models.core_data import IdentityCollision

_TALOS_SBOM = [{"name": "talos", "version": "12.2.0", "kind": "pypi"}]
_OTHER_TALOS = {"available": True,
                "summary": "Reproducible parameter sweeps for Keras, TensorFlow and PyTorch",
                "home_page": "https://github.com/autonomio/talos",
                "project_urls": {"Homepage": "https://autonom.io"},
                "package_url": "https://pypi.org/project/talos/"}
_KNOWN = "https://github.com/populationgenomics/talos"


def _pypi(monkeypatch, probe_for_name):
    from agent.skills import resolver as R
    monkeypatch.setattr(R, "probe_pypi", lambda name, timeout=12: probe_for_name(name))
    monkeypatch.setattr(R, "probe_conda", lambda name, timeout=12: {"available": False})


def test_a_same_named_pypi_package_pointing_elsewhere_is_a_collision(monkeypatch):
    _pypi(monkeypatch, lambda n: _OTHER_TALOS)
    idn = TI.capture(["talos"], _TALOS_SBOM, known_sources={"talos": _KNOWN})[0]
    assert idn["self_description"] is None, "the other project's blurb must not be adopted"
    # the install tie is still a fact: this package IS what shipped
    assert idn["source"] == "pypi" and idn["package"] == "talos" and idn["version"] == "12.2.0"
    assert idn["collision"] == {"registry": "pypi",
                                "points_at": "https://github.com/autonomio/talos",
                                "known_repo": _KNOWN}
    assert idn["note"] == ("a same-named pypi package `talos` exists elsewhere "
                           "(https://github.com/autonomio/talos); it is not "
                           f"{_KNOWN}, so its description is withheld")
    ToolIdentity.model_validate(idn)


def test_a_registry_hit_anchored_to_the_known_repo_is_adopted(monkeypatch):
    _pypi(monkeypatch, lambda n: dict(_OTHER_TALOS, summary="Rare-disease variant prioritisation",
                                      home_page=_KNOWN))
    idn = TI.capture(["talos"], _TALOS_SBOM, known_sources={"talos": _KNOWN})[0]
    assert idn["self_description"] == "Rare-disease variant prioritisation"
    assert idn["collision"] is None and idn["note"] is None


def test_the_anchor_reads_project_urls_case_insensitively(monkeypatch):
    """A `Source` project URL in a different case, with `.git`, still anchors; the
    homepage pointing at a docs site elsewhere does not turn it into a collision."""
    _pypi(monkeypatch, lambda n: dict(_OTHER_TALOS, summary="the real one",
                                      home_page="https://talos-docs.example.org",
                                      project_urls={"Source": "https://github.com/PopulationGenomics/Talos.git"}))
    idn = TI.capture(["talos"], _TALOS_SBOM, known_sources={"talos": "populationgenomics/talos"})[0]
    assert idn["self_description"] == "the real one" and idn["collision"] is None


def test_a_collision_points_at_the_source_url_before_the_homepage(monkeypatch):
    """`points_at` names where the OTHER project lives, so the most specific URL wins:
    a repository URL over a marketing homepage over the registry's own project page."""
    _pypi(monkeypatch, lambda n: dict(_OTHER_TALOS, home_page="https://autonom.io",
                                      project_urls={"Repository": "https://github.com/autonomio/talos"}))
    idn = TI.capture(["talos"], _TALOS_SBOM, known_sources={"talos": _KNOWN})[0]
    assert idn["collision"]["points_at"] == "https://github.com/autonomio/talos"


def test_a_collision_falls_back_to_the_registry_project_page(monkeypatch):
    _pypi(monkeypatch, lambda n: dict(_OTHER_TALOS, home_page="", project_urls={}))
    idn = TI.capture(["talos"], _TALOS_SBOM, known_sources={"talos": _KNOWN})[0]
    assert idn["collision"]["points_at"] == "https://pypi.org/project/talos/"


def test_the_known_source_is_scoped_to_its_own_tool(monkeypatch):
    """The repo builds talos; pysam beside it has its own home. Only the tool the source
    is tied to is checked against it — the other keeps its install-tied description."""
    _pypi(monkeypatch, lambda n: {"available": True, "summary": f"PYPI::{n}",
                                  "home_page": f"https://github.com/elsewhere/{n}",
                                  "project_urls": {}})
    sbom = _TALOS_SBOM + [{"name": "pysam", "version": "0.22", "kind": "pypi"}]
    ids = {i["tool"]: i for i in TI.capture(["talos", "pysam"], sbom,
                                             known_sources={"talos": _KNOWN})}
    assert ids["talos"]["collision"] is not None and ids["talos"]["self_description"] is None
    assert ids["pysam"]["collision"] is None and ids["pysam"]["self_description"] == "PYPI::pysam"


def test_a_conda_hit_whose_recipe_repo_differs_is_a_collision(monkeypatch):
    """conda's probe exposes the recipe's own GitHub repo (`repo`, from dev_url/home);
    a different repo than the known one is the collision, pointing at that repo."""
    from agent.skills import resolver as R
    monkeypatch.setattr(R, "probe_conda", lambda name, timeout=12: {
        "available": True, "summary": "Cluster analysis (base R)", "repo": "cran/cluster"})
    sbom = [{"name": "cluster", "version": "2.1.4", "kind": "conda"}]
    idn = TI.capture(["cluster"], sbom, known_sources={"cluster": "someone/cluster"})[0]
    assert idn["self_description"] is None
    assert idn["collision"] == {"registry": "conda", "points_at": "https://github.com/cran/cluster",
                                "known_repo": "someone/cluster"}


def test_a_conda_hit_whose_recipe_repo_matches_is_adopted(monkeypatch):
    from agent.skills import resolver as R
    monkeypatch.setattr(R, "probe_conda", lambda name, timeout=12: {
        "available": True, "summary": "SAM utils", "repo": "samtools/samtools"})
    idn = TI.capture(["samtools"], _SBOM, known_sources={"samtools": "git@github.com:samtools/samtools.git"})[0]
    assert idn["self_description"] == "SAM utils" and idn["collision"] is None


def test_a_hit_with_no_urls_cannot_be_tied_and_is_withheld_without_claiming_a_collision(fake_registry):
    """Nothing points anywhere, so no collision is asserted — but a blurb that cannot be
    tied to the known project is a guess, and the record says why it is withheld."""
    idn = TI.capture(["samtools"], _SBOM, known_sources={"samtools": "samtools/samtools"})[0]
    assert idn["self_description"] is None
    assert idn["collision"] is None
    assert idn["note"] == ("the conda package `samtools` publishes no homepage or repository, "
                           "so it cannot be tied to samtools/samtools; its description is withheld")
    ToolIdentity.model_validate(idn)


def test_no_known_source_means_the_sbom_tie_is_the_anchor(monkeypatch):
    """Unchanged behaviour when nothing is known: the install-tied summary is disclosed,
    and the renders label it unverified as they always did."""
    _pypi(monkeypatch, lambda n: _OTHER_TALOS)
    idn = TI.capture(["talos"], _TALOS_SBOM)[0]
    assert idn["self_description"] == _OTHER_TALOS["summary"]
    assert idn["collision"] is None and idn["note"] is None


def test_known_sources_with_blank_entries_are_ignored(monkeypatch):
    _pypi(monkeypatch, lambda n: _OTHER_TALOS)
    idn = TI.capture(["talos"], _TALOS_SBOM, known_sources={"talos": "", "": "x/y"})[0]
    assert idn["collision"] is None and idn["self_description"] == _OTHER_TALOS["summary"]


@pytest.mark.parametrize("known,needles", [
    ("populationgenomics/talos",
     ["github.com/populationgenomics/talos", "populationgenomics.github.io/talos"]),
    ("https://github.com/PopulationGenomics/Talos.git",
     ["github.com/populationgenomics/talos", "populationgenomics.github.io/talos"]),
    ("git@github.com:populationgenomics/talos.git",
     ["github.com/populationgenomics/talos", "populationgenomics.github.io/talos"]),
    ("https://gitlab.com/group/proj.git/", ["gitlab.com/group/proj"]),
    ("git@gitlab.com:group/proj.git", ["gitlab.com/group/proj"]),
    ("", []),
    ("   ", []),
])
def test_repo_needles_normalise_every_spelling_of_a_repository(known, needles):
    assert TI._repo_needles(known) == needles


def test_the_collision_model_is_closed_and_requires_every_field():
    IdentityCollision.model_validate({"registry": "pypi", "points_at": "https://x", "known_repo": "o/r"})
    with pytest.raises(ValidationError):
        IdentityCollision.model_validate({"registry": "pypi", "points_at": "https://x",
                                          "known_repo": "o/r", "summary": "leaks the other blurb"})
    with pytest.raises(ValidationError):
        IdentityCollision.model_validate({"registry": "", "points_at": "https://x", "known_repo": "o/r"})
    with pytest.raises(ValidationError):
        IdentityCollision.model_validate({"registry": "pypi", "known_repo": "o/r"})


def test_an_identity_written_before_collisions_existed_still_parses():
    """Records on disk predate the field; they read as "no collision detected"."""
    ti = ToolIdentity.model_validate(_GOOD_ID)
    assert ti.collision is None and ti.note is None


def test_capture_states_collision_and_note_on_every_record(fake_registry):
    for d in TI.capture(["samtools", "seurat", "pysam", "dorado"], _SBOM):
        assert "collision" in d and "note" in d
        ToolIdentity.model_validate(d)


_COLLIDED_ID = {"tool": "talos", "self_description": None, "source": "pypi",
                "package": "talos", "version": "12.2.0",
                "collision": {"registry": "pypi", "points_at": "https://github.com/autonomio/talos",
                              "known_repo": _KNOWN},
                "note": "a same-named pypi package `talos` exists elsewhere"}


def test_register_accepts_a_collision_record():
    from agent.skills.freeze import EnvCache
    cache = EnvCache(Path(tempfile.mkdtemp()) / "cache.json")
    cache.register("k", _record([_COLLIDED_ID]))
    assert tool_identities(cache.lookup("k"))[0].collision.registry == "pypi"


def test_env_html_shows_a_collision_as_a_collision_never_a_description():
    from agent.skills.env_report_html import render_env_report_html
    html = render_env_report_html(_record([_COLLIDED_ID]))
    assert "name collision" in html
    assert "autonomio/talos" in html and "populationgenomics/talos" in html
    assert "its description is not shown" in html
    assert "describes it as" not in html
    assert "Reproducible parameter sweeps" not in html


def test_recipe_md_shows_a_collision_as_a_collision():
    from agent.skills.env_recipe_render import render_recipe_markdown
    recipe = {"name": "talos", "primary_tools": ["talos"],
              "build_method": "container-native-build", "tool_identities": [_COLLIDED_ID]}
    md = render_recipe_markdown(recipe, _record([_COLLIDED_ID]))
    assert "What each tool says it is" in md
    assert "**name collision** — a same-named pypi package exists elsewhere" in md
    assert "autonomio/talos" in md and "its description is withheld" in md


def test_attestation_carries_the_collision_beside_the_identity():
    from agent.skills.attestation import build_attestation
    att = build_attestation(_record([_COLLIDED_ID]))
    ti = att["predicate"]["buildDefinition"]["internalParameters"]["tool_identities"][0]
    assert ti["collision"]["registry"] == "pypi" and ti["self_description"] is None
