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
    assert "self-described" in html and "unverified" in html


def test_env_html_omits_row_when_no_description():
    """A tool with no self_description gets no sub-row (absence is not fabricated as data)."""
    from agent.skills.env_report_html import render_env_report_html
    none_id = {"tool": "samtools", "self_description": None, "source": None,
               "package": None, "version": None}
    html = render_env_report_html(_record([none_id]))
    assert "self-described" not in html


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
