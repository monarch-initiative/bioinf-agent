"""Identity — is the registry entry we picked the tool the caller MEANT?

The gap this closes (audit 2026-07-16 §3): every other check in the codebase verifies
INTEGRITY (it builds, it runs, it ships as validated) and none verifies IDENTITY. A
wrong-but-working tool therefore passes every gate and ships green — `library(cellranger)`
loads fine, so an R spreadsheet-range parser earns a content digest, an SLSA attestation,
an ENV report saying "validated in shipped image", and a .sif on the cluster.

Identity can't be machine-verified — "did you mean this one?" is about intent. So the
contract here is DISCLOSURE, and these tests pin both halves of it:
  - the two real misroutes are flagged, with the package's own words as the evidence
  - real bioinformatics tools are NOT flagged (alarm fatigue would undo the whole thing)

Registry data is stubbed with the REAL strings these registries return (captured live), so
these run offline while testing the real resolve() path end to end.
"""

from __future__ import annotations

import pytest

from agent.skills import resolver as R


# --- real strings, captured from the live registries 2026-07-16 -------------
_CRAN_CELLRANGER = ('Translate Spreadsheet Cell Ranges to Rows and Columns — Helper '
                    'functions to work with spreadsheets and the "A1:D10" style of cell '
                    'range specification.')
_PYPI_TALOS = "Talos Hyperparameter Tuning for Keras"
_CONDA_SAMTOOLS = "Tools for dealing with SAM, BAM and CRAM files"
_PYPI_CYVCF2 = "fast vcf parsing with cython + htslib"
_CRAN_APE = ("Analyses of Phylogenetics and Evolution — Functions for reading, writing, "
             "plotting, and manipulating phylogenetic trees.")


def _stub(monkeypatch, *, conda=None, pip=None, cran=None):
    """Stub the registry probes; drive the REAL resolve()."""
    monkeypatch.setattr(R, "probe_conda", lambda n, t=12: conda or {"available": False})
    monkeypatch.setattr(R, "probe_pypi", lambda n, t=12: pip or {"available": False})
    monkeypatch.setattr(R, "probe_cran", lambda n, t=12: cran or {"available": False})
    monkeypatch.setattr(R, "probe_bioconductor", lambda n, t=12: {"available": False})
    # the authors gate needs no network for these cases
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: {})


# ---------------------------------------------------------------------------
# domain_signal — pure
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("text,expect_hit", [
    (_CONDA_SAMTOOLS, True),        # sam/bam/cram
    (_PYPI_CYVCF2, True),           # vcf/htslib
    (_CRAN_APE, True),              # phylogenetic
    ("Genome assembly and variant calling", True),
    (_CRAN_CELLRANGER, False),      # spreadsheets
    (_PYPI_TALOS, False),           # keras
    ("Fundamental package for array computing", False),
    ("", False),
])
def test_domain_signal_discriminates(text, expect_hit):
    assert bool(R.domain_signal(text)) is expect_hit, R.domain_signal(text)


def test_domain_signal_respects_word_boundaries():
    """'sam' must not fire inside 'same', 'bed' not inside 'bedroom'. A loose match would
    silently CONFIRM a wrong tool, which is worse than not matching at all."""
    assert R.domain_signal("the same thing in the bedroom") == []
    assert "sam" in R.domain_signal("reads a SAM file")


def test_domain_signal_returns_the_matched_terms_not_a_bool():
    """The decision must be able to SHOW its reasoning — a bare bool is one more
    assertion to take on faith, which is what this codebase refuses to do."""
    assert set(R.domain_signal(_PYPI_CYVCF2)) >= {"vcf", "htslib"}


# ---------------------------------------------------------------------------
# the two real misroutes
# ---------------------------------------------------------------------------
def test_cellranger_the_wrong_tool_is_flagged(monkeypatch):
    """resolve_tool('cellranger') returns CRAN's spreadsheet-range parser, not 10x
    Genomics' Cell Ranger. It used to come back as a confident install_call with
    cross_namespace_collisions == [] and nothing anywhere hinting at doubt."""
    _stub(monkeypatch, cran={"available": True, "latest": "1.1.0",
                             "summary": _CRAN_CELLRANGER,
                             "url": "https://github.com/rsheets/cellranger",
                             "bug_reports": ""})
    d = R.resolve("cellranger")
    assert d["chosen"] == "cran"
    assert d["identity"]["confirmed"] is False
    assert d["identity"]["anchor"] == "none"
    # the package's OWN words must be in the decision — that is the whole point
    assert "Spreadsheet" in d["identity"]["self_description"]
    # and the string an agent copies must carry the doubt, not just a sibling key
    assert d["install_call"].startswith("# IDENTITY UNCONFIRMED")
    assert "install_r_package" in d["install_call"]
    assert "IDENTITY UNCONFIRMED" in d["rationale"]


def test_talos_the_wrong_tool_is_flagged(monkeypatch):
    """PyPI 'talos' is autonomio's Keras tuner — not the rare-disease pipeline this
    project was built around."""
    _stub(monkeypatch, pip={"available": True, "latest": "1.4", "summary": _PYPI_TALOS,
                            "home_page": "", "project_urls": {}, "package_url": ""})
    d = R.resolve("talos")
    assert d["chosen"] == "pip"
    assert d["identity"]["confirmed"] is False
    assert "Keras" in d["identity"]["self_description"]
    assert d["install_call"].startswith("# IDENTITY UNCONFIRMED")


# ---------------------------------------------------------------------------
# real tools must NOT be flagged — alarm fatigue would undo the disclosure
# ---------------------------------------------------------------------------
def test_bioconda_membership_alone_confirms_identity(monkeypatch):
    """bioconda is a bioinformatics-only channel, so membership IS identity — exact,
    free, and no text heuristic on the commonest path by far."""
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda",
                              "latest": "1.21", "summary": _CONDA_SAMTOOLS})
    d = R.resolve("samtools")
    assert d["chosen"] == "conda"
    assert d["identity"]["confirmed"] is True
    assert d["identity"]["anchor"] == "bioconda-channel"
    assert not d["install_call"].startswith("#")


def test_conda_forge_package_falls_back_to_its_description(monkeypatch):
    """conda-forge is NOT bio-only, so the channel proves nothing — the description has
    to carry it."""
    _stub(monkeypatch, conda={"available": True, "channel": "conda-forge",
                              "latest": "1.0", "summary": _PYPI_CYVCF2})
    d = R.resolve("cyvcf2")
    assert d["identity"]["confirmed"] is True
    assert d["identity"]["anchor"] == "domain-terms"
    assert "vcf" in d["identity"]["evidence"]


def test_generic_conda_forge_package_is_flagged_not_refused(monkeypatch):
    """numpy genuinely is not a bioinformatics tool, so saying so is correct — but it is a
    perfectly legitimate thing to install, so this must never become a refusal."""
    _stub(monkeypatch, conda={"available": True, "channel": "conda-forge",
                              "latest": "2.0", "summary": "Fundamental package for array computing"})
    d = R.resolve("numpy")
    assert d["chosen"] == "conda"                 # still resolves
    assert d["identity"]["confirmed"] is False    # honestly flagged
    assert "install_conda_packages" in d["install_call"]   # still usable


def test_missing_description_reads_as_unchecked_not_as_a_wrong_tool(monkeypatch):
    """No evidence and CONTRARY evidence must not read the same.

    'this package says it parses spreadsheets' is a reason to suspect the PICK; 'we have no
    description to check against' is a reason to suspect our own PROBE. Crying "may not be
    the tool you mean" about a tool we merely failed to check is the noise that trains a
    reader to skip warnings — and a skipped warning is worth nothing.

    Vehicle: a registry entry that resolves but carries an EMPTY summary. Any tier can hand
    us that, so the branch is reached by the probe returning nothing to read — never by a
    claim about which tier is metadata-poor. (The original vehicle was `vep` via the spack
    tier, retired in tier 7. Its docstring blamed Spack for "carrying no metadata at all",
    which was false: Spack publishes a description in the package.py class docstring — our
    HEAD-only probe discarded the body. The tier was innocent; the probe was lazy.)
    """
    _stub(monkeypatch, pip={"available": True, "latest": "1.0.0", "summary": ""})
    d = R.resolve("foo")
    assert d["chosen"] == "pip"
    idy = d["identity"]
    assert idy["confirmed"] is False
    assert idy["reason"] == "no_description"
    assert "UNCHECKED" in idy["note"]
    assert "may not be the" not in idy["note"], \
        "missing evidence must not be reported as evidence of a wrong tool"


def test_contrary_description_reads_as_a_probable_wrong_tool(monkeypatch):
    """The mirror of the above: when the entry's own words contradict the domain, say so
    in the stronger language — that IS evidence."""
    _stub(monkeypatch, cran={"available": True, "latest": "1.1.0",
                             "summary": _CRAN_CELLRANGER, "url": "", "bug_reports": ""})
    idy = R.resolve("cellranger")["identity"]
    assert idy["reason"] == "no_domain_signal"
    assert "UNCONFIRMED" in idy["note"] and "may not be the" in idy["note"]


def test_explicit_github_repo_anchors_identity(monkeypatch):
    """The caller naming the repo is authoritative — they told us which project they mean,
    so no description heuristic should second-guess it."""
    _stub(monkeypatch, pip={"available": True, "latest": "1.0", "summary": "no bio words here",
                            "home_page": "https://github.com/owner/mytool",
                            "project_urls": {}, "package_url": ""})
    d = R.resolve("mytool", github_repo="owner/mytool")
    assert d["identity"]["confirmed"] is True
    assert d["identity"]["anchor"] == "github_repo"


def test_repo_backed_tiers_are_identity_anchored_by_construction(monkeypatch):
    """binary/source/synthesis/author tiers ARE a repo — identity is whatever repo was
    named or discovered, so they must not be flagged on description grounds."""
    _stub(monkeypatch)
    monkeypatch.setattr(R, "probe_github", lambda o, r, t=12: {
        "available": True, "repo_exists": True, "has_release_assets": False,
        "assets": [], "tag": "v1.0"})
    d = R.resolve("seqtk", github_repo="lh3/seqtk")
    assert d["chosen"] in ("source", "synthesis", "binary")
    assert d["identity"]["confirmed"] is True
    assert d["identity"]["anchor"] == "repo"
