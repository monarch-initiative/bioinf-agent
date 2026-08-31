"""The resolver used to investigate hardest when it found nothing, and stop the moment it
found anything — including the wrong thing. And the one fact that could say "a human must
fetch this" was fed by the single tier gated tools cannot be on.

Two findings from the same sea trial (2026-08-06), both measured on the real registries,
both fixed here. They are one file because they are one disease: the answer got MORE
confident exactly where the evidence got thinner.

F15 — DISCOVERY RAN ONLY AT A DEAD END. Five tools, back to back, one variable (does ANY
registry carry the name):

    annovar     no registry hit -> 5 repos investigated -> refusal that names the ask   ✅
    signalp     no registry hit -> 5 repos investigated -> refusal that names the ask   ✅
    exomiser    no registry hit -> 5 repos investigated -> the RIGHT tool, adopted      ✅
    cellranger  CRAN hit        -> 0 repos, none run    -> install_r_package(cellranger),
                                                           a SPREADSHEET-RANGE PARSER
    dorado      PyPI hit        -> 0 repos, none run    -> install_pip_package(dorado),
                                                           an ASTRONOMY package

A same-name registry entry SUPPRESSED the investigation that would have surfaced the real
tool — and a squatter existing is the strongest available signal that a name is contested.
The investigative effort was inversely proportional to how wrong the answer was about to
be. The fix is one more probe and a disclosure: no tool-name table, and no judgement inside
the resolver. The 2026-08-06 ruling that identity is the RIDE's call stands — it was made
about what the resolver may CONCLUDE, and looking is not concluding.

F14 — GATEDNESS HAD ONE INPUT, AND IT WAS THE WRONG ONE. `identity.license` was read from
the chosen tier, and only the conda probe ever populated it. Measured across six probes:

    samtools=1.21  conda  "MIT"  -> permissive     ✅
    dorado         pip    ""     -> unrecognized
    cellranger     cran   ""     -> unrecognized
    exomiser       binary ""     -> unrecognized
    cellranger + github_repo=10XGenomics/cellranger  synthesis "" -> unrecognized

That is structural, not incidental: a tool is licence-gated BECAUSE its bytes may not be
redistributed, which is exactly why it is not on bioconda — so the tier carrying the fact
and the tier gated tools live on are disjoint by construction. The `restricted` disclosure
was unreachable precisely where it was needed. Worse, every one of those rows says
`unrecognized`, which is a CLAIM ("we read a licence and could not place it") about a field
nothing had read.

NEITHER FIX MAY REINTRODUCE A NAME LIST. `resolver.py` records why `VENDOR_GATED` (seven
proprietary names) was removed on 2026-08-06: a finite list silently promises a
completeness it cannot keep. Nothing below keys on a tool name; the tool names in this file
are test DATA and appear in no table that changes behaviour.
"""

from __future__ import annotations

import pytest

from agent.skills import resolver as R


# --- real strings, captured from the live registries -------------------------------
_CRAN_CELLRANGER = ("Translate Spreadsheet Cell Ranges to Rows and Columns — Helper "
                    "functions to work with spreadsheets and the \"A1:D10\" style of cell "
                    "range specification.")
_CONDA_SAMTOOLS = "Tools for dealing with SAM, BAM and CRAM files"
_TENX = "Cell Ranger is a set of analysis pipelines that process Chromium single cell data"
_ASTRO = "A python package for reducing astronomical images"


def _search(*repos):
    """A `probe_github_search` result from (repo, stars, description, exact?) tuples."""
    return {"found": bool(repos),
            "candidates": [{"repo": r, "stars": s, "description": d,
                            "language": None, "exact_name_match": e}
                           for r, s, d, e in repos]}


def _stub(monkeypatch, *, conda=None, pip=None, cran=None, bioc=False, search=None,
          gh=None):
    """Stub every probe; drive the REAL resolve(). Name-aware on conda, because resolve
    asks it twice (bare name, then `bioconductor-{name}`) and a stub that cannot tell the
    two apart answers a question nobody asked."""
    calls: list[str] = []

    def _conda(n, t=12):
        if n.lower().startswith(("bioconductor-", "r-")):
            return {"available": False}
        return dict(conda) if conda else {"available": False}

    monkeypatch.setattr(R, "probe_conda", _conda)
    monkeypatch.setattr(R, "probe_pypi", lambda n, t=12: dict(pip) if pip else {"available": False})
    monkeypatch.setattr(R, "probe_cran", lambda n, t=12: dict(cran) if cran else {"available": False})
    monkeypatch.setattr(R, "probe_bioconductor", lambda n, t=12: {"available": bool(bioc)})
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: {})
    monkeypatch.setattr(R, "probe_github", lambda repo, t=12: dict(gh or {
        "repo_exists": False, "has_release_assets": False, "assets": [],
        "is_fork": False, "parent": "", "upstream": "", "full_name": "",
        "default_branch": ""}))
    monkeypatch.setattr(R, "_canon_repo",
                        lambda repo, t=12: ((repo or "").strip().strip("/").lower(), "absent"))
    monkeypatch.setattr(R, "_fetch_json",
                        lambda url, timeout=12: ([], "") if "/search?" in url
                        else (None, "not stubbed in this test"))

    def _gh_search(name, t=12, limit=5):
        calls.append(name)
        return dict(search if search is not None else {"found": False, "candidates": []})

    monkeypatch.setattr(R, "probe_github_search", _gh_search)
    return calls


# ===================================================================================
# F15 — the comparison itself. Pure, so every shape is cheap to pin.
# ===================================================================================
def test_the_entry_being_one_of_three_projects_is_not_corroboration():
    """THE TRAP, and the reason `matched` is stated separately from the status. PyPI's
    `dorado` points at Mucephie/DORADO — which IS an exact-name repo — so a rule that
    stopped at "is the entry's repo among the hits" would stamp CORROBORATED on the
    astronomy package, in the exact case this whole fix exists for."""
    c = R.name_corroboration("dorado", "Mucephie/DORADO", _search(
        ("nanoporetech/dorado", 700, "ONT basecaller", True),
        ("Mucephie/DORADO", 8, _ASTRO, True)))
    assert c["status"] == "diverges"
    assert c["matched"] is True, "the entry IS one of them — and that is not the question"
    assert [r["repo"] for r in c["divergent_repos"]] == ["nanoporetech/dorado"]


def test_an_entry_pointing_somewhere_else_diverges():
    """The cellranger shape: CRAN's entry names rsheets/cellranger and github's exact-name
    repo is somebody else's project entirely."""
    c = R.name_corroboration("cellranger", "rsheets/cellranger", _search(
        ("10XGenomics/cellranger", 400, _TENX, True)))
    assert c["status"] == "diverges" and c["matched"] is False
    assert "rsheets/cellranger" in c["detail"]


def test_an_entry_with_no_repo_at_all_says_so_rather_than_implying_a_conflict():
    """bioconda's `trinity` recipe carries no dev_url. "The entry points elsewhere" would
    be false; the honest fact is that nothing links these two, and nothing separates them
    either."""
    c = R.name_corroboration("thing", "", _search(("someone/thing", 20, "a thing", True)))
    assert c["status"] == "diverges" and c["matched"] is False
    assert "names no repo" in c["detail"]


def test_the_sole_exact_name_repo_being_the_entrys_own_is_corroboration():
    c = R.name_corroboration("samtools", "samtools/samtools", _search(
        ("samtools/samtools", 1900, _CONDA_SAMTOOLS, True),
        ("brentp/hts-nim", 200, "not the same name", False)))
    assert c["status"] == "corroborated"
    assert c["divergent_repos"] == []


def test_matched_is_stated_as_unknown_where_there_was_nothing_to_match():
    """`None`, not `False`. "There were competing repos and this entry is none of them"
    and "there were none" are different facts, and a reader must not have to know which
    statuses carry the key to tell them apart."""
    for c in (R.name_corroboration("x", "o/x", _search()),
              R.name_corroboration("x", "o/x", {"found": False, "candidates": [],
                                                "probe_error": "rate_limited"})):
        assert c["matched"] is None


def test_a_near_miss_name_is_not_a_competing_claim():
    """Only an EXACT name match counts. `samtools-legacy` owning a related name is not
    another project claiming to BE `samtools`, and treating it as one is the alarm fatigue
    that gets the real disclosure skipped."""
    c = R.name_corroboration("samtools", "samtools/samtools", _search(
        ("someone/samtools-legacy", 90, "a mirror", False)))
    assert c["status"] == "no_same_name_repo"


def test_a_search_that_did_not_answer_is_unobserved_never_a_clean_bill():
    """github search is 10 req/min unauthenticated, so this fires in normal use. "We
    searched and nothing owns this name" and "we could not search" are opposite facts
    about the tool, and the whole probe-honesty file exists because one value used to
    serve both."""
    c = R.name_corroboration("dorado", "Mucephie/DORADO",
                             {"found": False, "candidates": [], "probe_error": "rate_limited"})
    assert c["status"] == "unobserved"
    assert c["status"] != "no_same_name_repo"
    assert c["probe_error"] == "rate_limited"
    assert "did not get to look" in c["detail"]


def test_every_status_is_declared():
    """A status a reader has never seen is one nothing branches on correctly."""
    assert set(R.NAME_CORROBORATION_STATUSES) == {
        "corroborated", "diverges", "no_same_name_repo", "unobserved", "not_applicable"}


# ===================================================================================
# F15 — the resolver actually runs it, on a HIT
# ===================================================================================
def test_a_registry_hit_no_longer_suppresses_the_github_search(monkeypatch):
    """THE REGRESSION. Before this, `probe_github_search` was reached only when
    `chosen is None`, so the two names with a same-name squatter got no investigation at
    all — the two most contested names in the measured set.

    Break it: put the search back behind `if decision["chosen"] is None`."""
    calls = _stub(monkeypatch, conda={"available": True, "channel": "bioconda",
                                      "latest": "1.21", "summary": _CONDA_SAMTOOLS,
                                      "repo": "samtools/samtools", "license": "MIT",
                                      "license_source": "the bioconda recipe's `license`"})
    d = R.resolve("samtools")
    assert d["chosen"] == "conda"
    assert calls == ["samtools"], "the name search must run on a hit, exactly once"


def test_a_squatter_pick_carries_the_real_project_and_its_own_words(monkeypatch):
    """The measured cellranger answer: a clean `install_r_package(env, "cellranger")` for a
    spreadsheet-range parser, with nothing anywhere naming the project the user meant. The
    pick is UNCHANGED — judging is the ride's call — but the evidence it judges on now has
    to include the other project's existence and its own description."""
    _stub(monkeypatch,
          cran={"available": True, "latest": "1.1.0", "summary": _CRAN_CELLRANGER,
                "url": "https://github.com/rsheets/cellranger", "bug_reports": "",
                "license": "MIT + file LICENSE", "license_source": "CRAN's `License` field"},
          search=_search(("10XGenomics/cellranger", 400, _TENX, True)))
    d = R.resolve("cellranger")

    assert d["chosen"] == "cran", "the resolver states facts; it does not overturn the pick"
    assert d["identity_corroboration"]["status"] == "diverges"
    call = d["install_call"]
    assert "SAME NAME, DIFFERENT PROJECTS" in call
    assert "10XGenomics/cellranger" in call, "name the project that also owns the name"
    assert _TENX in call, "its OWN words are what the ride judges on"
    assert "github_repo='10XGenomics/cellranger'" in call, "and the exact next call"


def test_the_disclosure_says_that_nothing_downstream_asks_this(monkeypatch):
    """THE GATE IS THE GUIDE, and the target shape measured on `I12.accel_absent_from_image`:
    say where we looked, why it matters, and what to do. The why here is specific — a
    wrong-but-working tool passes every other gate in this system, because they all verify
    integrity and none verifies meaning."""
    _stub(monkeypatch,
          pip={"available": True, "latest": "1.0", "summary": _ASTRO,
               "home_page": "https://github.com/Mucephie/DORADO",
               "project_urls": {}, "package_url": "", "license": "MIT",
               "license_source": "PyPI's `license` field"},
          search=_search(("nanoporetech/dorado", 700, "ONT basecaller", True),
                         ("Mucephie/DORADO", 8, _ASTRO, True)))
    note = R.resolve("dorado")["rationale"]
    assert "searched github" in note                       # where we looked
    assert "builds, validates, and ships green" in note    # why it matters
    assert "re-run with github_repo=" in note              # the remedy
    assert "proceed" in note                               # ...and the other remedy


def test_a_corroborated_name_stays_quiet_and_the_call_stays_clean(monkeypatch):
    """A disclosure that fires on the healthy case is one readers learn to strip on the
    case that matters — the calibration the `ambiguous` flag was rewritten for."""
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda", "latest": "1.21",
                              "summary": _CONDA_SAMTOOLS, "repo": "samtools/samtools",
                              "license": "MIT",
                              "license_source": "the bioconda recipe's `license`"},
          search=_search(("samtools/samtools", 1900, _CONDA_SAMTOOLS, True)))
    d = R.resolve("samtools")
    assert d["identity_corroboration"]["status"] == "corroborated"
    assert "SAME NAME" not in d["install_call"]
    assert d["install_call"].startswith("install_conda_packages("), d["install_call"]


def test_an_unobserved_search_is_stated_but_does_not_poison_the_call(monkeypatch):
    """Absence is STATED — in the rationale and machine-readably — and is not rendered as a
    finding. The loud channel is reserved for what we FOUND: this probe's quota is 10/min,
    so stamping a banner on every pick whose search timed out would train the reader to
    skip the banner that means something."""
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda", "latest": "1.21",
                              "summary": _CONDA_SAMTOOLS, "repo": "samtools/samtools",
                              "license": "MIT",
                              "license_source": "the bioconda recipe's `license`"},
          search={"found": False, "candidates": [], "probe_error": "rate_limited"})
    d = R.resolve("samtools")
    assert d["identity_corroboration"]["status"] == "unobserved"
    assert "NAME NOT CORROBORATED (unobserved, not clean)" in d["rationale"]
    assert not d["install_call"].lstrip().startswith("#"), d["install_call"]


def test_a_caller_supplied_repo_costs_no_second_search(monkeypatch):
    """The caller anchored identity themselves and the cross-namespace guard already
    reconciles the registry hit against it. This also keeps auto-adopt — which re-enters
    resolve WITH the repo it found — at one search per resolve rather than two."""
    calls = _stub(monkeypatch,
                  pip={"available": True, "latest": "1.0", "summary": "a tool",
                       "home_page": "https://github.com/owner/mytool", "project_urls": {},
                       "package_url": ""},
                  gh={"repo_exists": True, "has_release_assets": False, "assets": [],
                      "is_fork": False, "parent": "", "upstream": "",
                      "full_name": "owner/mytool", "default_branch": "main"})
    d = R.resolve("mytool", github_repo="owner/mytool")
    assert calls == []
    assert d["identity_corroboration"]["status"] == "not_applicable"
    assert "caller named the repo" in d["identity_corroboration"]["detail"]


def test_the_key_is_stated_on_a_refusal_too(monkeypatch):
    """`None` is a value a producer must STATE. An absent key cannot be told apart from a
    producer that forgot, and on a refusal the reader most needs to know which."""
    _stub(monkeypatch)
    d = R.resolve("nope-not-a-tool")
    assert d["chosen"] is None
    assert d["identity_corroboration"]["status"] == "not_applicable"
    assert "no tier was chosen" in d["identity_corroboration"]["detail"]


def test_no_tool_name_decides_anything(monkeypatch):
    """The removed `VENDOR_GATED` table's lesson, pinned. Two runs differing ONLY in the
    tool's name and the repos github returns must produce the same SHAPE of answer — the
    behaviour is driven by what the probes returned, never by which name was typed."""
    for name in ("cellranger", "some-tool-nobody-has-heard-of"):
        _stub(monkeypatch,
              cran={"available": True, "latest": "1.0", "summary": "an R package",
                    "url": "https://github.com/rsheets/thing", "bug_reports": ""},
              search=_search((f"vendor/{name}", 400, "the other project", True)))
        d = R.resolve(name)
        assert d["chosen"] == "cran"
        assert d["identity_corroboration"]["status"] == "diverges"
    # ...and the module defines no name list to consult. (The removed table is DESCRIBED in
    # a block comment, deliberately — that is where a tool name belongs.)
    assert not hasattr(R, "VENDOR_GATED")


# ===================================================================================
# F14 — the licence comes off whichever tier we picked
# ===================================================================================
@pytest.mark.parametrize("info,expect_text,expect_src", [
    ({"license_expression": "MIT"}, "MIT", "SPDX"),
    ({"license_expression": "", "classifiers": ["License :: OSI Approved :: MIT License",
                                                "Topic :: Scientific/Engineering"]},
     "MIT License", "classifiers"),
    # the bare parent classifier names no licence and must not stand in for one
    ({"classifiers": ["License :: OSI Approved"]}, "", ""),
    ({"license": "BSD-3-Clause"}, "BSD-3-Clause", "`license` field"),
    ({}, "", ""),
])
def test_the_pypi_licence_is_taken_from_the_best_structured_source(info, expect_text,
                                                                   expect_src):
    """PyPI publishes a licence three ways and the resolver read none of them, so every
    pip pick reported `unrecognized` — a fact about which tier we happened to land on."""
    text, src = R._pypi_license(info)
    assert text == expect_text
    assert expect_src in src if expect_src else src == ""


def test_a_licence_BODY_is_reported_as_prose_rather_than_keyword_matched():
    """Some packages paste the whole licence into `info.license`. Keyword-matching a
    licence BODY is the `bcftools --version` mistake on a new axis: GPLv3's own text
    discusses commercial redistribution, so a regex over it returns `restricted` for one of
    the most permissive licences in the field. We say what we have and decline to classify
    it — which is not the same as saying nothing was published."""
    body = "Permission is hereby granted...\n" + ("x" * 400)
    text, src = R._pypi_license({"license": body})
    assert text == ""
    assert "TEXT rather than an identifier" in src
    assert R.license_evidence("pip", text, src) == "unclassified_text"


@pytest.mark.parametrize("tier,text,source,expect", [
    ("conda", "MIT", "the bioconda recipe's `license`", "published"),
    ("pip", "", "PyPI's `license` field, which holds the licence TEXT",
     "unclassified_text"),
    ("cran", "", "", "not_published"),
    ("bioconductor", "", "", "not_probed"),
    ("synthesis", "", "", "no_channel"),
    ("binary", "", "", "no_channel"),
    ("author_image", "", "", "no_channel"),
])
def test_the_five_silences_are_five_different_facts(tier, text, source, expect):
    """`""` was answering five questions with one value, and `license_disposition("")`
    rounded all five to `unrecognized` — a claim about a field nothing read. `not_probed`
    is the one that is about US: `probe_bioconductor` is an existence check that collects
    no licence, so reporting "the entry publishes none" would state a fact about our probe
    as a fact about the package."""
    assert R.license_evidence(tier, text, source) == expect


def test_the_pypi_probe_captures_the_licence_it_had_already_fetched(monkeypatch):
    """Driven through the REAL probe at the I/O seam, because the defect was not in the
    classification — it was that nobody CAPTURED the string, out of a response the probe
    was already parsing for `summary` and `project_urls`.

    Break it: drop the capture and every pip pick reports an unread field as a finding."""
    monkeypatch.setattr(R, "_fetch_json", lambda url, timeout=12: ({
        "info": {"version": "4.0", "summary": "a vendor tool", "home_page": "",
                 "project_urls": {}, "package_url": "",
                 "classifiers": ["License :: Other/Proprietary License"]},
        "releases": {"4.0": [{"yanked": False}]}}, ""))
    probe = R.probe_pypi("vendortool")
    assert probe["license"] == "Other/Proprietary License"
    assert "classifiers" in probe["license_source"]


def test_the_cran_probe_captures_the_licence_it_had_already_fetched(monkeypatch):
    monkeypatch.setattr(R, "_fetch_json", lambda url, timeout=12: (
        {"Package": "thing", "Version": "1.1.0", "Title": "an R pkg",
         "License": "GPL-3", "URL": "", "BugReports": ""}, ""))
    probe = R.probe_cran("thing")
    assert probe["license"] == "GPL-3"
    assert "CRAN" in probe["license_source"]


def test_a_restricted_licence_off_conda_now_reaches_the_disclosure(monkeypatch):
    """THE HOLE, in one test. A tool is licence-gated BECAUSE its bytes may not be
    redistributed — which is exactly why it is not on bioconda — so the tier carrying the
    licence and the tier gated tools live on were disjoint by construction, and the
    LICENSE-GATED disclosure could never fire where it was needed.

    Break it: read `license` from the conda probe only, and this pick goes out clean."""
    _stub(monkeypatch,
          pip={"available": True, "latest": "4.0", "summary": "a vendor tool",
               "home_page": "", "project_urls": {}, "package_url": "",
               "license": "Other/Proprietary License",
               "license_source": "PyPI's `License ::` classifiers"})
    d = R.resolve("vendortool")
    assert d["chosen"] == "pip"
    assert d["identity"]["license"] == "Other/Proprietary License"
    assert d["identity"]["license_evidence"] == "published"
    assert d["identity"]["license_disposition"] == "restricted"
    assert "LICENSE-GATED" in d["install_call"]
    assert "gated=True" in d["install_call"], "name the declaration freeze will demand"


def test_the_cran_licence_reaches_the_decision(monkeypatch):
    """CRAN requires a License field of every package. Reading it costs nothing and stops
    `unrecognized` from being an artifact of which tier won."""
    _stub(monkeypatch, cran={"available": True, "latest": "1.1.0", "summary": "an R pkg",
                             "url": "", "bug_reports": "", "license": "GPL-3",
                             "license_source": "CRAN's `License` field"})
    idy = R.resolve("thing")["identity"]
    assert idy["license"] == "GPL-3"
    assert idy["license_disposition"] == "permissive"
    assert "CRAN" in idy["license_source"]


def test_an_unpublished_licence_is_unobserved_not_unrecognized(monkeypatch):
    """`unrecognized` is a finding — "we read a licence and could not place it", which is
    the honest answer for a real vendor licence that matches no OSI identifier. Emitting it
    over a field nobody read is absence rounding up into a verdict, and it is the reason
    this whole channel read as working."""
    _stub(monkeypatch, cran={"available": True, "latest": "1.1.0", "summary": "an R pkg",
                             "url": "", "bug_reports": ""})
    idy = R.resolve("thing")["identity"]
    assert idy["license"] == ""
    assert idy["license_evidence"] == "not_published"
    assert idy["license_disposition"] == "unobserved"
    assert idy["license_disposition"] != "unrecognized"


def test_a_repo_route_says_that_no_gate_downstream_will_ask_this(monkeypatch):
    """The sharpest measured case was `resolve("cellranger",
    github_repo="10XGenomics/cellranger")`: the caller anchored the most licence-gated repo
    in the field and got `license_disposition: unrecognized` plus "go synthesize a build
    from it", with no mention that the binary sits behind a EULA and a download form.

    No name table can fix that, and none is used here. What is nameable without naming
    anybody is the SHAPE: nothing on a repo/release route publishes a licence, and I13 arms
    on a licence OBSERVED in the shipped image's conda-meta — which a tool built from
    source does not carry. So this is the end of the line for the question, and the note
    says so."""
    _stub(monkeypatch,
          gh={"repo_exists": True, "has_release_assets": False, "assets": [],
              "is_fork": False, "parent": "", "upstream": "",
              "full_name": "vendor/tool", "default_branch": "main"})
    d = R.resolve("tool", github_repo="vendor/tool")
    assert d["chosen"] in ("synthesis", "source")
    assert d["identity"]["license_evidence"] == "no_channel"
    assert d["identity"]["license_disposition"] == "unobserved"
    note = d["rationale"]
    assert "LICENCE UNOBSERVED" in note
    assert "I13 arms on a licence it can OBSERVE" in note, "say why nothing downstream helps"
    assert "behind an account or a click-through" in note, "name the shape, never the tool"
    assert "gated=True" in note


def test_a_published_licence_adds_no_note(monkeypatch):
    """The quiet case must stay quiet, or the note is one more thing readers learn to
    skip."""
    _stub(monkeypatch, conda={"available": True, "channel": "bioconda", "latest": "1.21",
                              "summary": _CONDA_SAMTOOLS, "repo": "samtools/samtools",
                              "license": "MIT",
                              "license_source": "the bioconda recipe's `license`"},
          search=_search(("samtools/samtools", 1900, _CONDA_SAMTOOLS, True)))
    d = R.resolve("samtools")
    assert d["identity"]["license_evidence"] == "published"
    assert "LICENCE UNOBSERVED" not in d["rationale"]


def test_the_classification_still_lives_in_exactly_one_place():
    """Both new licence readers must keep going through `core_data.license_disposition` —
    the contract classifies a licence OBSERVED in the shipped image with the same function,
    and a tool that is commercial at freeze and free at resolve is the drift that rule
    exists to prevent."""
    import inspect
    src = inspect.getsource(R)
    assert "_LICENSE_RESTRICTED" not in src and "_LICENSE_PERMISSIVE" not in src
    assert "_core_data.license_disposition" in inspect.getsource(R.identity_facts)


# ---------------------------------------------------------------------------
# THE RECURRENCE, found by audit hours after this file's fix shipped.
#
# The divergence disclosure had two defects, both introduced BY the fix for F15
# — the finding about name confusion:
#
#   1. it printed `divergent_repos[:3]` under a sentence stating how many repos
#      own the name. On `dorado` that reads "4 repo(s) own the name" above three
#      rows. One field, two readings, in the message built for comparing.
#   2. the copy-pasteable remedy hard-named `divergent_repos[0]`. That list is
#      ordered by STARS (github search, top 5 by stars), so we recommended the
#      star maximum: for `dorado`, a 1222-star PowerShell Scoop bucket, over the
#      859-star `nanoporetech/dorado` printed directly beneath it.
#
# Star-dominance is the heuristic this repo records as removed from
# `auto_adoptable`. It came back inside the remedy string of the fix built to
# cure name confusion, and it passed 37 tests, because every test used a fixture
# where the star maximum happened to be the RIGHT answer.
# ---------------------------------------------------------------------------

def _diverging_note(monkeypatch, tool, repos, entry_repo=""):
    """Drive resolve() to a `diverges` disclosure over a controlled candidate list."""
    from agent.skills import resolver

    monkeypatch.setattr(resolver, "probe_conda",
                        lambda t, timeout=12, **kw: {"available": False}, raising=False)
    monkeypatch.setattr(resolver, "probe_cran",
                        lambda t, timeout=12, **kw: {"available": False}, raising=False)
    monkeypatch.setattr(resolver, "probe_bioconductor",
                        lambda t, timeout=12, **kw: {"available": False}, raising=False)
    monkeypatch.setattr(resolver, "probe_pypi",
                        lambda t, timeout=12, **kw: {"available": True, "version": "1.0",
                                                     "home_page": "", "repo": entry_repo},
                        raising=False)
    monkeypatch.setattr(
        resolver, "probe_github_search",
        lambda *a, **kw: {"available": True,
                          "candidates": [{**r, "exact_name_match": True} for r in repos]},
        raising=False)
    d = resolver.resolve(tool=tool, timeout=1)
    return (d.get("install_call") or "") + " || " + (d.get("rationale") or "")


#: Star order deliberately DISAGREES with relevance — the shape the fixture suite missed.
_STARS_LIE = [
    {"repo": "scoopbucket/thing", "stars": 1222, "description": "A Scoop bucket for Windows apps"},
    {"repo": "realorg/thing", "stars": 859, "description": "The actual scientific tool"},
    {"repo": "someone/thing", "stars": 40, "description": "A student reimplementation"},
    {"repo": "other/thing", "stars": 3, "description": "Unrelated toy"},
]


def test_the_remedy_does_not_single_out_the_star_maximum(monkeypatch):
    """THE defect. Naming exactly one candidate makes the resolver choose, and the
    only ordering it has is popularity. The 2026-08-06 ruling puts that judgement
    with the ride, which is the reader that has world knowledge."""
    note = _diverging_note(monkeypatch, "thing", _STARS_LIE)
    assert "SAME NAME, DIFFERENT PROJECTS" in note

    named = [r["repo"] for r in _STARS_LIE if f"github_repo='{r['repo']}'" in note]
    assert len(named) != 1, (
        f"the disclosure offers exactly one repo ({named}) as THE next call. With the "
        f"list ordered by stars that is a popularity guess wearing a remedy's clothes.")


def test_every_candidate_is_equally_copy_pasteable(monkeypatch):
    """Not-picking must not cost actionability: each row carries its own call."""
    note = _diverging_note(monkeypatch, "thing", _STARS_LIE)
    for r in _STARS_LIE:
        assert f"github_repo='{r['repo']}'" in note, f"{r['repo']} has no copy-pasteable call"


def test_the_count_and_the_rows_are_one_reading(monkeypatch):
    """"4 repo(s) own the name" over three rows. The page said four and showed three."""
    note = _diverging_note(monkeypatch, "thing", _STARS_LIE)
    shown = sum(1 for r in _STARS_LIE if r["repo"] in note)
    assert shown == len(_STARS_LIE), (
        f"{shown} of {len(_STARS_LIE)} divergent repos reached the message, which states "
        f"the full count — a reader cannot compare candidates they were not shown")


def test_the_message_says_the_ordering_is_popularity_not_relevance(monkeypatch):
    """A ranked list with no stated ordering reads as a recommendation. Saying what
    the order MEANS is what stops the first row being taken as the answer."""
    note = _diverging_note(monkeypatch, "thing", _STARS_LIE).lower()
    assert "stars" in note and ("popularity, not relevance" in note or "not relevance" in note)
