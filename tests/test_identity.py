"""Identity — does resolve() surface the FACTS the ride needs to judge "is this the
tool you MEANT?"

Phase 2 (2026-07-17, the reverse theme park) moved identity JUDGMENT to the LLM ride.
The resolver no longer stamps a `confirmed` verdict or poisons `install_call` with a
word-list warning — `assess_identity` and the 86-word `_DOMAIN_TERMS` list are DELETED.
The reasoning (the user's): an LLM in a bioinformatics context is a far better judge of
"ONT's dorado, not the PyPI astronomy package" than any regex could ever be, and when it
lands on a tool it is probably right — the honesty contract downstream is the net that
catches a mechanical error, and a genuinely-undecidable case becomes an ASK.

So the resolver's job here shrank to ONE thing: put the evidence on the table, honestly.
These tests pin that the evidence is present and correct — the entry's OWN self-description,
its repo provenance, the missing-vs-contrary distinction — and that identity adds NO
poisoning to `install_call`. They do NOT assert a verdict, because the resolver no longer
makes one; that is the ride's call, exercised by an LLM-in-the-loop eval, not here.

Registry data is stubbed with the REAL strings these registries return (captured live),
so these run offline while driving the real resolve() path end to end.
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


#: the fact keys the ride judges on — and the verdict keys that must NOT reappear.
_FACT_KEYS = {"chosen_tier", "self_description", "has_description", "repo",
              "repo_source", "repo_anchored", "channel"}
_VERDICT_KEYS = {"confirmed", "anchor", "evidence", "note", "reason"}


def _stub(monkeypatch, *, conda=None, pip=None, cran=None):
    """Stub the registry probes; drive the REAL resolve()."""
    monkeypatch.setattr(R, "probe_conda", lambda n, t=12: conda or {"available": False})
    monkeypatch.setattr(R, "probe_pypi", lambda n, t=12: pip or {"available": False})
    monkeypatch.setattr(R, "probe_cran", lambda n, t=12: cran or {"available": False})
    monkeypatch.setattr(R, "probe_bioconductor", lambda n, t=12: {"available": False})
    # the authors gate needs no network for these cases
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: {})


# ---------------------------------------------------------------------------
# the shape of the contract: facts, never a verdict
# ---------------------------------------------------------------------------
def test_identity_is_a_facts_block_with_no_verdict_keys(monkeypatch):
    """The whole point of Phase 2: `identity` carries evidence for the ride, not a
    `confirmed` boolean the resolver invented from a word-list."""
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda",
                              "latest": "1.21", "summary": _CONDA_SAMTOOLS,
                              "repo": "samtools/samtools", "repo_source": "conda"})
    d = R.resolve("samtools")
    idy = d["identity"]
    assert set(idy) == _FACT_KEYS
    assert not (_VERDICT_KEYS & set(idy)), "a resolver identity VERDICT has come back"


def test_no_chosen_means_no_identity(monkeypatch):
    """Absence renders as absence (Rule 2): when nothing resolves there is no entry to
    describe, so `identity` is None — not an empty facts block asserting things about
    a tool we never picked."""
    _stub(monkeypatch)
    monkeypatch.setattr(R, "probe_github_search",
                        lambda *a, **k: {"found": False, "candidates": []})
    d = R.resolve("nope-not-a-tool")
    assert d["chosen"] is None
    assert d["identity"] is None


# ---------------------------------------------------------------------------
# the two real misroutes: the entry's OWN words are surfaced as the evidence
# ---------------------------------------------------------------------------
def test_wrong_tool_surfaces_its_own_words_for_the_ride(monkeypatch):
    """resolve('cellranger') returns CRAN's spreadsheet-range parser, not 10x Genomics'
    Cell Ranger. The resolver no longer flags it — but it MUST hand the ride the package's
    own description, because that string ("Spreadsheet Cell Ranges") is exactly what lets
    the LLM see it is the wrong tool."""
    _stub(monkeypatch, cran={"available": True, "latest": "1.1.0",
                             "summary": _CRAN_CELLRANGER,
                             "url": "https://github.com/rsheets/cellranger",
                             "bug_reports": ""})
    d = R.resolve("cellranger")
    assert d["chosen"] == "cran"
    assert "Spreadsheet" in d["identity"]["self_description"]
    assert d["identity"]["has_description"] is True


def test_talos_keras_self_description_is_surfaced(monkeypatch):
    """PyPI 'talos' is autonomio's Keras tuner. The ride reads "Hyperparameter Tuning for
    Keras" and knows this is not the rare-disease pipeline."""
    _stub(monkeypatch, pip={"available": True, "latest": "1.4", "summary": _PYPI_TALOS,
                            "home_page": "", "project_urls": {}, "package_url": ""})
    d = R.resolve("talos")
    assert d["chosen"] == "pip"
    assert "Keras" in d["identity"]["self_description"]


# ---------------------------------------------------------------------------
# channel membership is a FACT the ride weighs — surfaced, not adjudicated
# ---------------------------------------------------------------------------
def test_bioconda_channel_is_surfaced_as_a_fact(monkeypatch):
    """bioconda is a bioinformatics-only channel, so membership is a strong identity signal.
    The resolver reports the fact (`channel: bioconda`); the ride does the weighing. And
    because nothing is in doubt to the RESOLVER, `install_call` is a clean one-liner."""
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda",
                              "latest": "1.21", "summary": _CONDA_SAMTOOLS,
                              "repo": "samtools/samtools", "repo_source": "conda"})
    d = R.resolve("samtools")
    assert d["chosen"] == "conda"
    assert d["identity"]["channel"] == "bioconda"
    assert not d["install_call"].lstrip().startswith("#"), \
        "identity must not poison install_call — that verdict is the ride's now"


def test_generic_conda_forge_package_resolves_clean(monkeypatch):
    """numpy is genuinely not a bioinformatics tool, but it is a perfectly legitimate
    install. The resolver surfaces its words and its non-bio channel and does NOT editorialize
    — no refusal, no poisoning. The ride decides whether numpy-as-a-dep makes sense."""
    _stub(monkeypatch, conda={"available": True, "channel": "conda-forge", "latest": "2.0",
                              "summary": "Fundamental package for array computing",
                              "repo": "numpy/numpy", "repo_source": "conda"})
    d = R.resolve("numpy")
    assert d["chosen"] == "conda"
    assert d["identity"]["channel"] == "conda-forge"
    assert "array computing" in d["identity"]["self_description"]
    assert "install_conda_packages" in d["install_call"]


# ---------------------------------------------------------------------------
# missing evidence != contrary evidence — the distinction survives as a FACT
# ---------------------------------------------------------------------------
def test_missing_description_reads_as_missing_not_as_contrary(monkeypatch):
    """No evidence and contrary evidence are different situations and the ride must be able
    to tell them apart. The resolver does not editorialize ('may not be the tool you mean')
    about a tool it simply could not read — it states the fact: has_description is False."""
    _stub(monkeypatch, pip={"available": True, "latest": "1.0.0", "summary": ""})
    d = R.resolve("foo")
    assert d["chosen"] == "pip"
    assert d["identity"]["has_description"] is False
    assert d["identity"]["self_description"] == ""


def test_present_but_off_domain_description_reads_as_present(monkeypatch):
    """The mirror: a description that exists but is about something else is has_description
    True with the words intact — the ride reads them and judges 'contrary'. The resolver
    draws no such conclusion (no word-list to draw it from)."""
    _stub(monkeypatch, cran={"available": True, "latest": "1.1.0",
                             "summary": _CRAN_CELLRANGER, "url": "", "bug_reports": ""})
    idy = R.resolve("cellranger")["identity"]
    assert idy["has_description"] is True
    assert "Spreadsheet" in idy["self_description"]


# ---------------------------------------------------------------------------
# repo provenance is a FACT: anchor vs candidate
# ---------------------------------------------------------------------------
def test_explicit_github_repo_is_recorded_as_a_user_anchor(monkeypatch):
    """The caller naming the repo is authoritative for WHICH project they mean, so the
    resolver records it as a user-sourced anchor. (Whether that repo IS the tool is still
    the ride's judgment — a 200 from a repo URL proves it exists, nothing more.)"""
    _stub(monkeypatch, pip={"available": True, "latest": "1.0", "summary": "no bio words here",
                            "home_page": "https://github.com/owner/mytool",
                            "project_urls": {}, "package_url": ""})
    d = R.resolve("mytool", github_repo="owner/mytool")
    assert d["identity"]["repo"] == "owner/mytool"
    assert d["identity"]["repo_source"] == "user"
    assert d["identity"]["repo_anchored"] is True


def test_scraped_repo_is_a_candidate_not_an_anchor(monkeypatch):
    """A repo scraped from pip/cran metadata is surfaced as a CANDIDATE (repo_anchored
    False) — the INVESTIGATE signal. This is what keeps a bare `dorado` from adopting the
    astronomy repo's image at the tier above conda: unanchored, so the authors' path is
    not auto-taken; the ride confirms the repo first."""
    _stub(monkeypatch, pip={"available": True, "latest": "1.0", "summary": "some astronomy pkg",
                            "home_page": "https://github.com/someone/notthetool",
                            "project_urls": {}, "package_url": ""})
    d = R.resolve("mytool")
    assert d["identity"]["repo"] == "someone/notthetool"
    assert d["identity"]["repo_source"] == "pip"
    assert d["identity"]["repo_anchored"] is False


def test_repo_backed_tier_surfaces_the_repo(monkeypatch):
    """binary/source/synthesis tiers ARE a repo — the resolver surfaces which one, anchored
    because the caller named it."""
    _stub(monkeypatch)
    monkeypatch.setattr(R, "probe_github", lambda o, r, t=12: {
        "available": True, "repo_exists": True, "has_release_assets": False,
        "assets": [], "tag": "v1.0"})
    d = R.resolve("seqtk", github_repo="lh3/seqtk")
    assert d["chosen"] in ("source", "synthesis", "binary")
    assert d["identity"]["repo"] == "lh3/seqtk"
    assert d["identity"]["repo_anchored"] is True
