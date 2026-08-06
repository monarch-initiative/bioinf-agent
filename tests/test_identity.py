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
#:
#: `license` / `license_disposition` are admitted deliberately, and the distinction is
#: worth stating because they LOOK like the thing this test bans. A verdict key answers
#: "is this the tool you meant" — the judgement Phase 2 moved to the ride. These answer
#: a different question the ride cannot ask any other way: what does the artifact's
#: licence permit? Every identity signal for bioconda's `novoalign` is correct, and it is
#: commercial. `license` is the registry's own string, quoted; `license_disposition` is
#: `core_data.license_disposition` over it — three-state, with `unrecognized` for what it
#: does not know, and the SAME function the contract runs against the licence observed in
#: the shipped image, so the two cannot disagree.
_FACT_KEYS = {"chosen_tier", "self_description", "has_description", "repo",
              "repo_source", "repo_anchored", "channel",
              "license", "license_disposition"}
_VERDICT_KEYS = {"confirmed", "anchor", "evidence", "note", "reason"}


def _stub(monkeypatch, *, conda=None, pip=None, cran=None, gh=None):
    """Stub the registry probes; drive the REAL resolve().

    NAME-AWARE, and it has to be. These stubs used to answer for ANY name, so the moment
    resolve() asked a second conda question — `probe_version_lineage` looks for the next
    major lineage under a different package name — the stub cheerfully reported that
    `samtools2` exists, and two tests asserting a clean install_call went red over a
    package nobody had described. A stub that cannot tell two probes apart is not a stub of
    the thing being tested. It answers for the FIRST name it is asked about and says no to
    every other, which is what a registry does."""
    _first: dict[str, str] = {}

    def _conda(n, t=12):
        return dict(conda) if conda and n == _first.setdefault("conda", n) else {"available": False}

    monkeypatch.setattr(R, "probe_conda", _conda)
    monkeypatch.setattr(R, "probe_pypi", lambda n, t=12: pip or {"available": False})
    monkeypatch.setattr(R, "probe_cran", lambda n, t=12: cran or {"available": False})
    monkeypatch.setattr(R, "probe_bioconductor", lambda n, t=12: {"available": False})
    # the authors gate needs no network for these cases
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: {})
    # A test that passes `github_repo=` DOES reach probe_github, and this helper used to
    # leave it live — so one test here talked to api.github.com about a repo invented for
    # the test. The default is the 404 that repo really returns, so behaviour is unchanged;
    # it just no longer depends on GitHub answering.
    monkeypatch.setattr(R, "probe_github", lambda repo, t=12: dict(gh or {
        "repo_exists": False, "has_release_assets": False, "assets": [],
        "is_fork": False, "parent": "", "upstream": "",
        "full_name": "", "default_branch": ""}))
    monkeypatch.setattr(R, "_canon_repo",
                        lambda repo, t=12: ((repo or "").strip().strip("/").lower(), "absent"))


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
    """resolve('cellranger') finds CRAN's spreadsheet-range parser — a real package, right
    name, wrong project. The resolver does NOT stamp a verdict on that: it reports the
    pick and attaches the package's OWN words, and the ride reads "Translate Spreadsheet
    Cell Ranges" and knows this is not 10x's Cell Ranger.

    A name-keyed refusal table lived here for one day (2026-08-06) and was removed: it was
    the only hardcoded tool name in `agent/` that changed behaviour, and it duplicated —
    in a list that can only rot — knowledge the ride already has. The seventh entry being
    correct never made the eighth's absence honest. What the resolver owes is the
    EVIDENCE, and that is what this test pins.

    Break it: drop `self_description` and the ride is judging on a bare name."""
    _stub(monkeypatch, cran={"available": True, "latest": "1.1.0",
                             "summary": _CRAN_CELLRANGER,
                             "url": "https://github.com/rsheets/cellranger",
                             "bug_reports": ""})
    d = R.resolve("cellranger")
    assert d["chosen"] == "cran"
    assert "Spreadsheet" in d["identity"]["self_description"]
    assert d["identity"]["has_description"] is True
    assert d["identity"]["repo_anchored"] is False    # scraped, so a CANDIDATE — not adopted


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


# ---------------------------------------------------------------------------
# what counts as a COMPETING MEANING — the ambiguity flag's calibration
#
# Three defects, one theme: a flag that fires on the healthy case is not a cautious
# flag, it is a broken one. The corpus's `false-accusation-noise` row states it as the
# rule — "this is the false-ACCUSE half that makes the true-positive half worthless:
# the banner is IDENTICAL in shape to cellranger's, so a reader who sees it on anndata
# learns it means nothing and strips it on cellranger."
# ---------------------------------------------------------------------------
def test_a_conda_pick_resolves_the_collision_instead_of_also_accusing(monkeypatch):
    """conda winning IS the disambiguation — this branch has always PROCEEDED on it — but
    `ambiguous` stayed True on the way out, so one decision said "here is your package" and
    "this name is dangerously ambiguous" at once.

    MEASURED: `resolve('anndata')` returned conda-forge anndata 0.13.2 (scverse/anndata),
    exactly what was asked for, flagged ambiguous because CRAN also has an `anndata` — which
    is a reticulate WRAPPER around that same Python project. Not merely a false alarm: not
    even a collision.

    The FACT is not lost, which is the test's second half: both tiers stay in `available`,
    so a reader still sees that the name exists in two ecosystems. What is removed is the
    verdict laid over it.

    Break it: raise `ambiguous` before checking what the ranking concluded."""
    _stub(monkeypatch,
          conda={"available": True, "channel": "conda-forge", "latest": "0.13.2",
                 "summary": "An annotated data matrix.", "repo": "scverse/anndata"},
          pip={"available": True, "latest": "0.13.2", "summary": "Annotated data.",
               "home_page": "", "project_urls": {"Source": "https://github.com/scverse/anndata"}},
          cran={"available": True, "latest": "0.8.0", "url": "https://github.com/dynverse/anndata",
                "summary": "'anndata' for R — A 'reticulate' wrapper for the Python package."})
    d = R.resolve("anndata")
    assert d["chosen"] == "conda"
    assert d["ambiguous"] is False, "a resolved collision must not also be reported as one"
    assert not d["install_call"].lstrip().startswith("#")
    assert {"pip", "cran"} <= set(d["available"]), \
        "the fact must survive where it belongs — dropping the flag must not drop the tiers"


def test_a_genuine_two_ecosystem_collision_still_refuses(monkeypatch):
    """The half that has to keep working, or the change above is just deletion. PyPI `ape`
    (a build system) and CRAN `ape` (phylogenetics) are two real projects with two real
    descriptions and no conda pick to arbitrate — refuse and ask."""
    _stub(monkeypatch,
          pip={"available": True, "latest": "0.5.0", "summary": "A build system",
               "home_page": "", "project_urls": {}},
          cran={"available": True, "latest": "5.8", "url": "",
                "summary": "Analyses of Phylogenetics and Evolution"})
    d = R.resolve("ape")
    assert d["chosen"] is None
    assert d["ambiguous"] is True
    assert d["refusal_reason"] == "needs_user_input"


def test_a_registry_stub_that_states_no_meaning_neither_wins_nor_makes_a_name_ambiguous(monkeypatch):
    """A name reservation is not a rival project, and it has to lose BOTH ways.

    `resolve('seurat')` failed both ways at once — which is why one gate was not enough.
    PyPI's `seurat` is version 0.0.2 with an empty summary, no homepage and no project URLs;
    CRAN's `Seurat` is 5.5.1, "Tools for Single Cell Genomics …", the standard R single-cell
    toolkit. The blank first COLLIDED with the real one into a refusal that asked the user
    to choose between a described bio tool and a blank — a question with one possible answer,
    and the taxes are what teach a ride to stop reading the asks. Lift only that, and the
    blank simply WON on tier order: `install_pip_package(env, "seurat", version="0.0.2")`.

    So the disqualification happens once, before ranking, and both consumers read it.

    Break it: fix the ambiguity flag without fixing the ranking, or vice versa."""
    _stub(monkeypatch,
          pip={"available": True, "latest": "0.0.2", "summary": "",
               "home_page": "", "project_urls": {}, "package_url": ""},
          cran={"available": True, "latest": "5.5.1", "resolved_name": "Seurat",
                "summary": "Tools for Single Cell Genomics — a toolkit for QC and analysis.",
                "url": "", "bug_reports": ""})
    d = R.resolve("seurat")
    assert d["chosen"] == "cran", "the described package must win over the blank"
    assert d["ambiguous"] is False, "a blank states no meaning, so there are not two meanings"
    assert d["degenerate_stubs"] and d["degenerate_stubs"][0]["tier"] == "pip"
    assert "DISQUALIFIED" in d["rationale"], "and never silently — say what lost, and why"
    # CRAN is case-SENSITIVE and so is install.packages(). The case-retry landed `Seurat`;
    # the emitted call spelled the QUERY, which does not install. Reachable only once the
    # line above let `seurat` reach the cran tier at all — a wrong tool traded for a broken one.
    assert 'install_r_package(env, "Seurat", source="cran")' in d["install_call"]


def test_nothing_is_muted_when_every_registry_hit_is_equally_mute(monkeypatch):
    """The floor: if all we have is a blank, a blank is all we have. Muting them all would
    manufacture a dead end out of a thin one — the ABSENT-≠-UNCHECKED failure in a new
    costume, since the caller would be told nothing was found when something was.

    Break it: drop the `if not speaking` guard."""
    _stub(monkeypatch, pip={"available": True, "latest": "0.0.1", "summary": "",
                            "home_page": "", "project_urls": {}, "package_url": ""})
    d = R.resolve("nobodyhome")
    assert d["chosen"] == "pip"
    assert "degenerate_stubs" not in d


def test_a_conda_hit_is_never_muted_for_a_thin_recipe(monkeypatch):
    """`channel` counts as a stated meaning, so this sniff can never overturn conda-first.

    Not a carve-out: being carried on a curated bioinformatics channel IS a statement — the
    ECOSYSTEM-not-identity distinction the corpus draws — and conda-first is a measured,
    load-bearing rule that a metadata heuristic has no business relitigating. A bioconda
    recipe with a terse summary is thin, not meaningless.

    Break it: drop `channel` from `_states_a_meaning`."""
    _stub(monkeypatch,
          conda={"available": True, "channel": "bioconda", "latest": "1.0", "summary": ""},
          pip={"available": True, "latest": "2.0", "summary": "A very well described package",
               "home_page": "https://example.org", "project_urls": {}})
    d = R.resolve("terse")
    assert d["chosen"] == "conda"
    assert "degenerate_stubs" not in d


# ---------------------------------------------------------------------------
# the name is a LINEAGE — bioinformatics versions tools by renaming the package
# ---------------------------------------------------------------------------
def test_the_next_major_lineage_is_disclosed_when_it_is_a_different_package(monkeypatch):
    """`resolve('gatk')` emits `gatk=3.8` — a 2017 release — because bioconda ships GATK4 as
    `gatk4`, a different package. Every fact about that pick is CORRECT: right project, right
    channel, right description, and a decade stale. Nothing anywhere said the thing almost
    everyone means is one character away.

    Break it: stop probing the successor, or stop poisoning the call."""
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda", "latest": "3.8",
                              "summary": "The full Genome Analysis Toolkit (GATK) framework."})
    monkeypatch.setattr(R, "probe_conda", lambda n, t=12: (
        {"available": True, "channel": "bioconda", "latest": "3.8",
         "summary": "The full Genome Analysis Toolkit (GATK) framework."} if n == "gatk"
        else {"available": True, "channel": "bioconda", "latest": "4.6.2.0",
              "summary": "Genome Analysis Toolkit (GATK4)"} if n == "gatk4"
        else {"available": False}))
    d = R.resolve("gatk")
    assert d["chosen"] == "conda", "a FACT, not a refusal — the pick stands"
    assert d["version_lineage"]["successor"] == "gatk4"
    assert d["version_lineage"]["successor_latest"] == "4.6.2.0"
    assert d["install_call"].lstrip().startswith("#"), "do not paste this one blind"
    assert "resolve_tool('gatk4')" in d["install_call"], "name the next call, not just the problem"
    assert d["install_call"].rstrip().endswith(
        'install_conda_packages(env, [{"spec": "gatk=3.8", "channel": "bioconda"}])'), \
        "the runnable line survives — a caller who DID mean GATK3 is not blocked"


def test_a_pinned_version_names_its_own_lineage_and_costs_no_probe(monkeypatch):
    """A caller who typed a version chose a lineage; asking them again is noise, and the
    successor probe is one HTTP round trip we then owe nothing for.

    Break it: run the lineage probe unconditionally."""
    asked = []

    def _conda(n, t=12):
        asked.append(n)
        return {"available": True, "channel": "bioconda", "latest": "3.8", "summary": "GATK"}

    monkeypatch.setattr(R, "probe_pypi", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_cran", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_bioconductor", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: {})
    monkeypatch.setattr(R, "probe_conda", _conda)
    d = R.resolve("gatk", version="3.8")
    assert "version_lineage" not in d
    assert asked == ["gatk"], f"one probe, not two: {asked}"


def test_no_successor_no_noise(monkeypatch):
    """The calibration half. bioconda carries no `tophat3`, so a user reproducing a 2013
    paper with tophat 2.1.2 gets a clean call — the corpus's deprecated-tools row is a guard
    against exactly this kind of over-correction, and it is not a hypothetical: the mechanism
    fires on only 3 of 18 common bioconda tools (gatk, bowtie, macs2), each a real split.

    Break it: disclose on any numbered name, or on a successor that does not exist."""
    monkeypatch.setattr(R, "probe_pypi", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_cran", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_bioconductor", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: {})
    monkeypatch.setattr(R, "probe_conda", lambda n, t=12: (
        {"available": True, "channel": "bioconda", "latest": "2.1.2",
         "summary": "A spliced read mapper"} if n == "tophat" else {"available": False}))
    d = R.resolve("tophat")
    assert d["chosen"] == "conda"
    assert "version_lineage" not in d
    assert d["install_call"].startswith("install_conda_packages(")


def test_a_metadata_less_probe_is_never_called_meaningless(monkeypatch):
    """The disqualification must speak only about tiers whose probe COLLECTS a description.

    Caught before landing: `probe_bioconductor` is an existence check — `_fetch_ok` on the
    release HTML page, returning `{"available": bool}` and nothing else — so under a naive
    reading it "states no meaning" for every package Bioconductor has ever shipped, and
    `resolve('deseq2', language='r')` disqualified the tier with a rationale asserting that
    the ENTRY says nothing about itself. That is a fact about our probe reported as a fact
    about the package: the report lie this repo exists to refuse, and it would have been
    invisible because cran outranks bioconductor anyway.

    Break it: put a metadata-less tier back into `_REGISTRY_TIERS`."""
    monkeypatch.setattr(R, "probe_conda", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_pypi", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_cran", lambda n, t=12: {
        "available": True, "latest": "3.0", "summary": "a real R package",
        "url": "", "bug_reports": ""})
    monkeypatch.setattr(R, "probe_bioconductor", lambda n, t=12: {"available": True})
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: {})
    d = R.resolve("deseq2", language="r")
    assert "degenerate_stubs" not in d
    assert "bioconductor" in d["available"], \
        "the tier must stay rankable — it was never a stub, only a probe that reads no prose"
    assert "DISQUALIFIED" not in d["rationale"]
