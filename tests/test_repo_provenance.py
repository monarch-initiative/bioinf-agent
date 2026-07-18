"""Repo provenance — a repo may only be taken from the candidate that WON, and it can
never be vouched for more strongly than the entry it was scraped from.

`_github_owner_repo` read pip/cran metadata unconditionally and handed the result to the
AUTHOR tiers, which outrank conda. Live, that meant the authors' tiers probed the
SQUATTER's repo:

    resolve('dorado')  -> author_image.repo = 'Mucephie/DORADO'   (an astronomy package)
    resolve('talos')   -> author_image.repo = 'autonomio/talos'   (a Keras tuner)
    resolve('trinity') -> author_image.repo = 'ethereum/trinity'  (conda WON — the repo came
                                                                   from the LOSING candidate)
    resolve('cellranger') -> 'rsheets/cellranger'                 (a spreadsheet parser)

and `assess_identity` stamped `confirmed: True, anchor: 'repo'` on them. The only thing
keeping the right answers standing was the accident that those squat repos publish no
image: had Mucephie/DORADO shipped one, `dorado` would have adopted an ASTRONOMY image by
digest, at the tier that outranks conda, validated in-image, POLICY_CLEAN, shipped green.

The fix was free. api.anaconda.org already returns `dev_url`/`home` in the response
`probe_conda` was already fetching — a curated recipe maintainer's own link to the project.
That is what makes the winner's entry answerable, and it is why the "a repo must come from
the winning candidate" rule works now: it was previously unsatisfiable, because probe_conda
captured no URLs at all, so EVERY repo necessarily came from pip/cran.

Note what the conda anchor does NOT ask: whether the tool is bioinformatics. `uv` is a
Python package manager and is the project's own live proof that the author_image tier
fires. "Did the authors publish this?" and "is this a bio tool?" are different questions,
and the author tiers only ever asked the first.
"""

from __future__ import annotations

from agent.skills import resolver as R


_PYPI_DORADO = "Digitized Observatory Resources for Automated Data Operations"
_PYPI_MULTIQC = "Create aggregate bioinformatics analysis reports across many samples and tools"


def _conda(channel="bioconda", repo="", **kw):
    out = {"available": True, "channel": channel, "latest": "1.0",
           "summary": "a tool", **kw}
    if repo:
        out["repo"] = repo
        out["repo_field"] = "dev_url"
    return out


def _pip(summary="", home=""):
    return {"available": True, "latest": "1.0", "summary": summary,
            "home_page": home, "project_urls": {}, "package_url": ""}


# ---------------------------------------------------------------------------
# probe_conda captures the recipe's own project link — free, same request
# ---------------------------------------------------------------------------
def test_conda_probe_captures_the_recipes_dev_url(monkeypatch):
    payload = {"versions": ["1.21"], "latest_version": "1.21", "summary": "SAM/BAM tools",
               "dev_url": "https://github.com/samtools/samtools",
               "home": "https://www.htslib.org/"}
    monkeypatch.setattr(R, "_fetch_json", lambda url, timeout=12:
                        (payload, "") if "bioconda" in url else (None, ""))
    p = R.probe_conda("samtools")
    assert p["repo"] == "samtools/samtools"
    assert p["repo_field"] == "dev_url"


def test_dev_url_is_preferred_over_home(monkeypatch):
    """multiqc's home is seqera.io; its dev_url is the repo. `home` is a fallback, not a peer."""
    payload = {"versions": ["1.0"], "latest_version": "1.0", "summary": "x",
               "dev_url": "https://github.com/MultiQC/MultiQC", "home": "https://seqera.io/multiqc"}
    monkeypatch.setattr(R, "_fetch_json", lambda url, timeout=12:
                        (payload, "") if "bioconda" in url else (None, ""))
    assert R.probe_conda("multiqc")["repo"] == "MultiQC/MultiQC"


def test_a_recipe_with_no_project_link_reports_no_repo(monkeypatch):
    """bioconda's real `trinity` recipe carries neither dev_url nor home. Absent is a FACT:
    a repo we do not have must never be substituted from somewhere else."""
    payload = {"versions": ["2.15"], "latest_version": "2.15", "summary": "assembler"}
    monkeypatch.setattr(R, "_fetch_json", lambda url, timeout=12:
                        (payload, "") if "bioconda" in url else (None, ""))
    p = R.probe_conda("trinity")
    assert "repo" not in p


# ---------------------------------------------------------------------------
# repo_evidence — the winner's entry, and only the winner's
# ---------------------------------------------------------------------------
def test_a_repo_from_the_losing_tier_is_never_taken():
    """THE trinity BUG. conda WON, but the repo came from the losing pip candidate, and was
    handed to tiers that outrank conda."""
    ev = R.repo_evidence({"conda": _conda(),                       # won; names no repo
                          "pip": _pip(home="https://github.com/ethereum/trinity")})
    assert ev == {}, ev


def test_the_winning_conda_entry_anchors_its_own_repo():
    ev = R.repo_evidence({"conda": _conda(repo="MultiQC/MultiQC"),
                          "pip": _pip(home="https://github.com/someone/else")})
    assert ev["repo"] == "MultiQC/MultiQC"
    assert ev["source"] == "conda" and ev["anchored"] is True
    assert "dev_url" in ev["detail"]


def test_conda_anchors_on_provenance_not_on_bio_ness():
    """`uv` is a Rust-written Python package manager — not remotely bioinformatics — and is
    the live proof that the author_image tier fires at all. Gating the AUTHOR tiers on
    domain would shut them for every non-bio dependency the system legitimately installs."""
    ev = R.repo_evidence({"conda": _conda(channel="conda-forge", repo="astral-sh/uv",
                                          summary="An extremely fast Python package and "
                                                  "project manager, written in Rust.")})
    assert ev["anchored"] is True and ev["repo"] == "astral-sh/uv"


def test_a_repo_scraped_from_a_pip_entry_is_a_candidate_never_an_anchor():
    """THE dorado BUG. pip won, so its repo is the only candidate — an astronomy repo
    (Mucephie/DORADO) from an astronomy entry. It is recorded as a candidate but anchored
    nothing, so the author tiers (above conda) are never auto-run on it."""
    ev = R.repo_evidence({"pip": _pip(summary=_PYPI_DORADO,
                                      home="https://github.com/Mucephie/DORADO")})
    assert ev["repo"] == "Mucephie/DORADO"          # recorded as a candidate...
    assert ev["source"] == "pip"
    assert ev["anchored"] is False                  # ...but it vouches for nothing
    assert "candidate repo for the ride to confirm" in ev["detail"]


def test_a_scraped_pip_repo_is_a_candidate_even_with_a_bio_summary():
    """Phase 2 (2026-07-17): a repo SCRAPED from pip/cran metadata is never anchored on the
    strength of its summary — the 86-word domain list that used to do that is deleted, and
    identity is the ride's judgment now, not a regex over a blurb. multiqc's REAL
    authors-image path is anchored by its curated conda `dev_url` (see
    test_conda_anchors_on_provenance_not_on_bio_ness above), NOT by scraping PyPI. So a bare
    pip entry, bio-worded or not, comes back as a CANDIDATE for the ride to confirm."""
    ev = R.repo_evidence({"pip": _pip(summary=_PYPI_MULTIQC,
                                      home="https://github.com/MultiQC/MultiQC")})
    assert ev["repo"] == "MultiQC/MultiQC"
    assert ev["anchored"] is False
    assert "candidate repo for the ride to confirm" in ev["detail"]


def test_a_user_supplied_repo_is_authoritative_for_WHICH_repo_to_look_at():
    ev = R.repo_evidence({"pip": _pip(summary=_PYPI_DORADO,
                                      home="https://github.com/Mucephie/DORADO")},
                         github_repo="nanoporetech/dorado")
    assert ev["repo"] == "nanoporetech/dorado" and ev["source"] == "user"


# ---------------------------------------------------------------------------
# the gate consumes it — and NOT ASSESSED is a third state
# ---------------------------------------------------------------------------
def _stub(monkeypatch, **tiers):
    for fn, key in (("probe_conda", "conda"), ("probe_pypi", "pip"),
                    ("probe_cran", "cran"), ("probe_bioconductor", "bioconductor")):
        monkeypatch.setattr(R, fn, lambda n, t=12, _k=key: tiers.get(_k, {"available": False}))
    monkeypatch.setattr(R, "probe_github_search", lambda *a, **k: {"found": False, "candidates": []})


def test_the_gate_never_runs_on_an_unanchored_repo(monkeypatch):
    """RECORD the call; do NOT raise from the stub. resolve() wraps the gate in a broad
    `except Exception` (deliberately — a probe failure must not break registry routing), so
    an AssertionError raised inside the stub is SWALLOWED by the code under test and
    recorded as `authors_gate_error`. This test was written that way first and passed while
    the bug was live: the failure signal never escaped the try block."""
    _stub(monkeypatch, pip=_pip(summary=_PYPI_DORADO,
                                home="https://github.com/Mucephie/DORADO"))
    calls: list[str] = []
    monkeypatch.setattr(R, "probe_authors_sources",
                        lambda tool, owner="", repo="", **k: calls.append(f"{owner}/{repo}") or {})
    d = R.resolve("dorado")
    assert calls == [], f"the gate ran on an UNANCHORED squat repo: {calls}"
    assert "authors_gate_error" not in d["probed"]      # not an error either — a third state
    na = d["probed"]["authors_gate_not_assessed"]
    assert na["repo"] == "Mucephie/DORADO" and na["repo_source"] == "pip"


def test_not_assessed_is_disclosed_and_never_reads_as_no_author_image(monkeypatch):
    """'We did not look' and 'they publish nothing' are opposite claims. The gate's own
    docstring says there is no third state between fired and errored — there is, and this
    is it, so it must be named rather than left to read as a negative finding."""
    _stub(monkeypatch, pip=_pip(summary=_PYPI_DORADO,
                                home="https://github.com/Mucephie/DORADO"))
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: {})
    d = R.resolve("dorado")
    assert "AUTHORS-PATH NOT ASSESSED" in d["rationale"]
    assert "AUTHORS-PATH NOT ASSESSED" in d["install_call"]
    assert "no authoring image/recipe found" not in d["rationale"]


def test_the_gate_still_fires_for_a_conda_anchored_tool(monkeypatch):
    """THE REGRESSION GUARD. This gate has already been 100% dead once (a swallowed 401)
    for five commits with a green suite. multiqc / uv / bakta are its entire live
    population — a tightening that shuts it for them has killed it again, by a new route."""
    called = {}
    _stub(monkeypatch, conda=_conda(repo="MultiQC/MultiQC", summary="reports"),
          pip=_pip(summary=_PYPI_MULTIQC, home="https://github.com/MultiQC/MultiQC"))
    def gate(tool, owner="", repo="", **k):
        called["repo"] = f"{owner}/{repo}"
        return {"author_image": {"ref": "ghcr.io/multiqc/multiqc", "source": "ghcr"},
                "reconstruction_incomplete": False, "recommendation": "adopt it"}
    monkeypatch.setattr(R, "probe_authors_sources", gate)
    d = R.resolve("multiqc")
    assert called["repo"] == "MultiQC/MultiQC"
    assert d["chosen"] == "author_image"
    # Phase 2: no `confirmed` verdict — the FACT is that the repo is conda-anchored, which
    # is exactly why the gate was safe to run and the ride can trust the adoption.
    assert d["identity"]["repo"] == "MultiQC/MultiQC"
    assert d["identity"]["repo_anchored"] is True
    assert d["probed"]["author_image"]["repo_source"] == "conda"


# ---------------------------------------------------------------------------
# identity_facts REPORT the anchoring decision — they never re-make it, and there is
# no verdict to false-confirm anymore (Phase 2 — the word-list is deleted)
# ---------------------------------------------------------------------------
def test_identity_facts_mark_an_unanchored_scraped_repo_as_a_candidate():
    """Pre-Phase-2, `assess_identity('dorado','author_image',...)` stamped
    {confirmed: True, anchor: 'repo', evidence: ['Mucephie/DORADO']} — an astronomy repo
    presented as a confirmed identity anchor at the tier above conda. There is no verdict
    to stamp now: the resolver states `repo_anchored=False` (a candidate) and the ride
    confirms or asks. What actually keeps the astronomy image from being adopted is the
    UNANCHORED repo (the author tier is not auto-run on it) — tested at resolve() level."""
    out = R.identity_facts("dorado", "author_image",
                           {"author_image": {"repo": "Mucephie/DORADO", "repo_source": "pip",
                                             "repo_anchored": False}})
    assert out["repo"] == "Mucephie/DORADO"
    assert out["repo_source"] == "pip"
    assert out["repo_anchored"] is False
    assert "confirmed" not in out            # the verdict is gone


def test_identity_facts_mark_a_conda_anchored_repo_as_anchored():
    out = R.identity_facts("uv", "author_image",
                           {"author_image": {"repo": "astral-sh/uv", "repo_source": "conda",
                                             "repo_anchored": True,
                                             "repo_detail": "the conda-forge recipe's `dev_url`"}})
    assert out["repo"] == "astral-sh/uv" and out["repo_anchored"] is True
    assert out["repo_source"] == "conda"


def test_identity_facts_read_the_anchoring_decision_never_re_decide_it():
    """One truth, one definition, read at every use (Rule 4). identity_facts REPORTS the
    anchoring the gate already made (`repo_anchored`); it runs no anchoring rule of its own,
    so the second copy that used to disagree with the first is simply gone."""
    out = R.identity_facts("multiqc", "author_image",
                           {"author_image": {"repo": "MultiQC/MultiQC", "repo_source": "pip",
                                             "repo_anchored": True}})
    assert out["repo_anchored"] is True
