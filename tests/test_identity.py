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
#:
#: `license_evidence` / `license_source` joined them 2026-08-07 and are facts of the same
#: kind: WHERE the string came from, and — when there is none — which of five different
#: silences we are in. They answer "what did anyone publish about these bytes", never "is
#: this the tool you meant". See tests/test_resolver_investigates_on_a_hit.py.
_FACT_KEYS = {"chosen_tier", "self_description", "has_description", "repo",
              "repo_source", "repo_anchored", "channel",
              "license", "license_source", "license_evidence", "license_disposition"}
_VERDICT_KEYS = {"confirmed", "anchor", "evidence", "note", "reason"}


def _stub(monkeypatch, *, conda=None, pip=None, cran=None, gh=None):
    """Stub the registry probes; drive the REAL resolve().

    NAME-AWARE, and it has to be. These stubs used to answer for ANY name, so the moment
    resolve() asked a second conda question the stub cheerfully reported that `samtools2`
    exists, and two tests asserting a clean install_call went red over a package nobody had
    described. A stub that cannot tell two probes apart is not a stub of the thing being
    tested. It answers for the FIRST name it is asked about and says no to every other,
    which is what a registry does.

    It also answers the anaconda SEARCH endpoint with an empty family, because
    `probe_package_family` runs on every conda win. Left unstubbed, the hermetic tier's
    socket guard fires — correctly: a test CI gates merges on must not be able to fail
    because a registry rate-limited us. A test that is ABOUT families calls
    `_family_search` afterwards to override this."""
    _first: dict[str, str] = {}

    def _conda(n, t=12):
        return dict(conda) if conda and n == _first.setdefault("conda", n) else {"available": False}

    monkeypatch.setattr(R, "probe_conda", _conda)
    monkeypatch.setattr(R, "_fetch_json",
                        lambda url, timeout=12: ([], "") if "/search?" in url
                        else (None, "not stubbed in this test"))
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
# the name is a FAMILY — a registry hit is not the same as an answer
# ---------------------------------------------------------------------------

def _family_search(monkeypatch, packages):
    """Stub the anaconda SEARCH endpoint with a list of package records."""
    monkeypatch.setattr(R, "_fetch_json",
                        lambda url, timeout=12: (packages, "") if "/search?" in url
                        else (None, "not stubbed"))


_GATK_FAMILY = [
    {"owner": "bioconda", "name": "gatk", "latest_version": "3.8",
     "summary": "The full Genome Analysis Toolkit (GATK) framework, license restricted.",
     "home": "https://www.broadinstitute.org/gatk/"},
    {"owner": "bioconda", "name": "gatk4", "latest_version": "4.6.2.0",
     "summary": "Genome Analysis Toolkit (GATK4)",
     "dev_url": "https://github.com/broadinstitute/gatk"},
    # the noise a substring match would drag in, and this one must not appear
    {"owner": "bioconda", "name": "gatk4-spark", "latest_version": "4.0.0", "summary": ""},
]


def test_the_whole_family_reaches_the_ride_in_one_call(monkeypatch):
    """THE DEFECT THIS EXISTS FOR: the resolver stopped investigating the moment a registry
    answered. `resolve('gatk')` hits bioconda's `gatk`, so the dead-end discovery path never
    fires, and the answer is `gatk=3.8` — a 2017 release — because this field versions tools
    by RENAMING the package and GATK4 ships as `gatk4`. Every fact about that pick is
    correct: right project, right channel, right description, a decade stale.

    A registry hit is not an answer. So we research on a HIT too, and the family arrives in
    ONE call with each member's version and its own words — the ride reads "gatk 3.8 /
    gatk4 4.6.2.0, both the Genome Analysis Toolkit" and picks in the same turn. Requiring a
    second round trip was the whole complaint against the earlier behaviour.

    An earlier cut GUESSED one successor (`{base}{major+1}`); it worked for gatk by
    arithmetic accident and could never have seen `macs` 1.4.3 under `macs2`/`macs3`.

    Break it: drop the search, or emit a clean install_call over a multi-member family."""
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda", "latest": "3.8",
                              "summary": "The full Genome Analysis Toolkit (GATK) framework."})
    _family_search(monkeypatch, _GATK_FAMILY)
    d = R.resolve("gatk")
    assert d["chosen"] == "conda", "a FACT, not a refusal — the pick stands"
    names = [m["name"] for m in d["package_family"]["members"]]
    assert names == ["gatk", "gatk4"],         f"the family must be the bare name + numbered siblings only, got {names}"
    assert d["package_family"]["members"][1]["repo"] == "broadinstitute/gatk"
    assert d["install_call"].lstrip().startswith("#"), "do not paste one member of a family blind"
    assert "gatk4=4.6.2.0" in d["install_call"], "the alternative must be pasteable, not hinted"
    assert "Genome Analysis Toolkit (GATK4)" in d["install_call"],         "each member's OWN words are what the ride judges on"
    assert d["install_call"].rstrip().endswith(
        'install_conda_packages(env, [{"spec": "gatk=3.8", "channel": "bioconda"}])'),         "the runnable line survives — a caller who did mean GATK3 is not blocked"


def test_the_family_is_never_ranked_for_the_ride(monkeypatch):
    """NEWEST DOES NOT WIN, and this is the case that proves why. bowtie and bowtie2 are
    different repos, different capabilities (ungapped vs gapped) and different user bases:
    two TOOLS that share a prefix, not two versions of one. Auto-routing to the higher number
    would hand a bowtie1 user a different aligner.

    The resolver states the family and stops. Which member the request means is world
    knowledge, and world knowledge belongs to the ride — the same ruling that deleted the
    vendor name table.

    Break it: re-rank on the numeric suffix, or refuse when a family has more than one member."""
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda", "latest": "1.3.1",
                              "summary": "An ultrafast memory-efficient short read aligner"})
    _family_search(monkeypatch, [
        {"owner": "bioconda", "name": "bowtie", "latest_version": "1.3.1",
         "summary": "An ultrafast memory-efficient short read aligner",
         "home": "https://github.com/BenLangmead/bowtie"},
        {"owner": "bioconda", "name": "bowtie2", "latest_version": "2.5.5",
         "summary": "A fast and sensitive gapped read aligner.",
         "dev_url": "https://github.com/BenLangmead/bowtie2"},
    ])
    d = R.resolve("bowtie")
    assert d["chosen"] == "conda"
    assert d["install_call"].rstrip().endswith(
        'install_conda_packages(env, [{"spec": "bowtie=1.3.1", "channel": "bioconda"}])'),         "bowtie1 must still be installable — it is a live tool, not a stale lineage"
    assert "BenLangmead/bowtie2" in d["install_call"],         "the DIFFERENT repo is the fact that tells a reader these are sibling projects"


def test_naming_the_newest_member_earns_silence(monkeypatch):
    """`resolve('gatk4')` does not need to be told `gatk` exists: the request names the
    current lineage, the pick IS that lineage, and no decision is left to inform. Same for
    bowtie2, macs3, hisat2 — which is most of the names anyone actually types.

    A disclosure that fires when there is nothing to decide is the calibration failure the
    `anndata` ambiguity flag was deleted for, one file over.

    Break it: disclose whenever a family has more than one member."""
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda", "latest": "4.6.2.0",
                              "summary": "Genome Analysis Toolkit (GATK4)"})
    _family_search(monkeypatch, _GATK_FAMILY)
    d = R.resolve("gatk4")
    assert "package_family" not in d
    assert d["install_call"].startswith("install_conda_packages(")


def test_a_pinned_version_costs_no_family_search(monkeypatch):
    """A caller who typed a version chose a lineage; the search is one HTTP round trip we
    then owe nothing for.

    The github NAME search is a different question and still runs: a version pin says which
    lineage of a project you want, never which project — `cellranger==1.1.0` is a
    spreadsheet-range parser at every version it has. So this asserts on the anaconda
    family endpoint specifically, not on 'no HTTP happened'.

    Break it: run the family search unconditionally."""
    seen = []
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda", "latest": "3.8",
                              "summary": "GATK", "versions": ["3.7", "3.8"]})
    monkeypatch.setattr(R, "_fetch_json",
                        lambda url, timeout=12: (seen.append(url), (None, "unexpected"))[1])
    d = R.resolve("gatk", version="3.8")
    assert "package_family" not in d
    family = [u for u in seen if "/search?" in u]
    assert not family, f"a pinned version must not trigger the family search: {family}"


def test_a_failed_family_search_is_unchecked_not_absent(monkeypatch):
    """"This tool has no siblings" must never be inferred from a search that did not run.
    The whole file spends its length on this distinction; a new probe does not get an
    exemption.

    Break it: return {} on a probe error and the caller reads silence as "no family"."""
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda", "latest": "3.8",
                              "summary": "GATK"})
    monkeypatch.setattr(R, "_fetch_json",
                        lambda url, timeout=12: (None, "rate_limited: 429"))
    d = R.resolve("gatk")
    assert d["package_family_unchecked"] == "rate_limited: 429"
    assert "package_family" not in d
    assert "NOT CHECKED" in d["install_call"]
    assert "unchecked, not absent" in d["install_call"]


def test_a_single_member_family_is_silence(monkeypatch):
    """Most tools. samtools has no numbered siblings, so the search returns one member and
    the call stays a clean one-liner — the measured shape for 7 of 10 common tools."""
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda", "latest": "1.24",
                              "summary": "Tools for dealing with SAM, BAM and CRAM files"})
    _family_search(monkeypatch, [
        {"owner": "bioconda", "name": "samtools", "latest_version": "1.24",
         "summary": "Tools for dealing with SAM, BAM and CRAM files"},
        {"owner": "bioconda", "name": "bioconductor-rsamtools", "latest_version": "2.0",
         "summary": "an R binding"},          # shares the letters, not the family
    ])
    d = R.resolve("samtools")
    assert "package_family" not in d
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


def test_a_channel_that_never_answered_does_not_make_a_version_the_latest(monkeypatch):
    """A HIT is a fact; "this is the newer build" is a COMPARISON — and a comparison against
    a channel that never answered is a claim about what we could REACH.

    `probe_conda` queries bioconda AND conda-forge and returns the higher real version. That
    comparison IS the guard against an abandoned build on one channel shadowing the
    maintained package on the other, and it used to fail silently open: one channel down, the
    guard runs on one input, and the output is indistinguishable from a real answer.

    MEASURED LIVE 2026-08-06, driving `seurat` end to end. With conda-forge timing out,
    `resolve('seurat', language='r')` emitted `r-seurat=3.0.2` — a 2019 build — while
    conda-forge carries 5.5.1. Two calls seconds apart (`seurat` vs `Seurat`, which lowercase
    to the SAME URL) disagreed by two major versions of the standard single-cell toolkit and
    neither said why. The old code said so in a comment — "errors only matter when NOTHING
    was found" — which is right about AVAILABILITY and wrong about the pick.

    Hermetic: `_fetch_json` is stubbed for BOTH channels, so this drives the real
    `probe_conda` merge without a socket. The stale-vs-current versions are the real ones
    the live probe returned.

    Break it: drop `channel_errors`, or disclose it only when nothing was found."""
    def flaky(url, timeout=12):
        if "conda-forge" in url:
            return None, "TimeoutError: The read operation timed out"
        return ({"versions": ["3.0.0", "3.0.2"], "latest_version": "3.0.2",
                 "summary": "single cell toolkit", "license": "GPL-3"}, "")

    monkeypatch.setattr(R, "_fetch_json", flaky)
    monkeypatch.setattr(R, "probe_pypi", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_cran", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_bioconductor", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: {})
    monkeypatch.setattr(R, "_fetch_ok", lambda u, t=12: (False, ""))

    rec = R.probe_conda("anytool")
    assert rec["available"] and rec["channel"] == "bioconda" and rec["latest"] == "3.0.2"
    assert rec["channel_errors"] == {
        "conda-forge": "TimeoutError: The read operation timed out"}, \
        "the probe kept a hit and threw away the fact that the comparison was one-sided"

    d = R.resolve("anytool")
    assert d["chosen"] == "conda", "a reachable hit is still a real answer — do not refuse"
    assert "conda-forge" in d["conda_channel_unchecked"]
    assert d["install_call"].lstrip().startswith("#"), \
        "an unproven-latest version must not be pasted blind into a pin"
    assert "NOT PROVEN LATEST" in d["install_call"]
    assert d["install_call"].rstrip().endswith('])'), \
        "the runnable line survives — this is a disclosure, not a refusal"


# ---------------------------------------------------------------------------
# the ride's normalization is a CLAIM, and claims get checked
# ---------------------------------------------------------------------------
def test_a_rename_across_a_major_version_is_supported_and_recorded(monkeypatch):
    """`user_said='gatk'` with `tool='gatk4'` says: the user typed one thing, I am installing
    another, deliberately. That is what SHOULD happen — an LLM knows GATK4 is the current
    line and the bare `gatk` package is GATK3 from 2017, and the system's job is to USE that
    knowledge rather than re-derive it mechanically.

    Both names are kept, because the pair is what a human reviewing the ENV report needs in
    order to disagree with us: "you asked for X, we installed Y".

    Break it: drop `normalized_from` and the substitution becomes invisible."""
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda", "latest": "4.6.2.0",
                              "summary": "Genome Analysis Toolkit (GATK4)"})
    d = R.resolve("gatk4", user_said="gatk")
    assert d["chosen"] == "conda"
    assert d["normalized_from"] == "gatk"
    assert "normalization_rejected" not in d
    assert d["install_call"].startswith("install_conda_packages(")


def test_swapping_in_a_different_tool_is_refused(monkeypatch):
    """The check that keeps the above from being a licence to hallucinate. A ride answering
    'bwa' with 'bowtie2' is not normalizing across a major version, it is substituting a
    different aligner — and nothing in the registries can tell the user it happened, because
    both are real, correct, well-described packages.

    Same family (gatk/gatk4, macs2/macs3) passes; different family refuses. This is the
    posture every other agent-authored field in this codebase gets, applied to the one with
    the most leverage.

    Break it: trust `tool` and record `user_said` as decoration."""
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda", "latest": "2.5.5",
                              "summary": "A fast and sensitive gapped read aligner."})
    d = R.resolve("bowtie2", user_said="bwa")
    assert d["chosen"] is None
    assert d["install_call"] is None
    assert d["refusal_reason"] == "investigation_contradicted"
    assert d["normalization_rejected"] == {"user_said": "bwa", "resolved_to": "bowtie2"}
    assert "NORMALIZATION REJECTED" in d["rationale"]


def test_a_channel_naming_convention_is_not_a_substitution(monkeypatch):
    """`r-{name}` and `bioconductor-{name}` are THIS MODULE's channel conventions, not a
    rename by the ride. Comparing them raw would make every `language='r'` call look like a
    cross-family substitution and refuse it.

    Break it: drop the prefix strip and R packages stop resolving."""
    _stub(monkeypatch, conda={"available": True, "channel": "conda-forge", "latest": "5.5.1",
                              "summary": "Tools for Single Cell Genomics"})
    d = R.resolve("r-seurat", user_said="seurat")
    assert d["chosen"] == "conda"
    assert "normalization_rejected" not in d
