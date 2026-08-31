"""Probe honesty — ABSENT and UNCHECKED are different facts and must not share a value.

Every tier's `available: False` is built on one return value at the bottom of the stack.
That value used to be `None` for BOTH "the registry answered: no such package" and "we
never got an answer", because `_get_json` caught `(URLError, HTTPError, OSError,
ValueError)` and returned None for all of them.

That collapse silently rewrote the routing decision. Measured on the live resolver before
the fix: with a single transient 403 on api.anaconda.org — and NOTHING about the tool
changed — `resolve('samtools')` stopped returning `conda` + a pinned
`install_conda_packages(samtools=1.21)` and instead returned `binary`, recorded
`conda: {available: False}` on disk, and announced:

    "AUTO-DISCOVERED + adopted samtools/samtools (1928*, exact-name) — no registry hit,
     but a dominant exact-name repo; proceeding without a human."

There WAS a registry hit. We could not reach it. Every clause of that sentence was false,
stated with full confidence, and the whole tier ladder slid a rung on a network blip —
producing a different build method, a different recipe, and a different image, all
reported as intentional.

These tests stub at the urlopen I/O SEAM and drive the REAL probe + resolve path, rather
than replacing the probes with doubles. Replacing the function is how the call-signature
drift of audit 2026-07-16 stayed invisible behind nine green tests.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from agent.skills import resolver as R


def _http_error(url: str, code: int, *, rate_limited: bool = False) -> urllib.error.HTTPError:
    hdrs = {"X-RateLimit-Remaining": "0"} if rate_limited else {}
    return urllib.error.HTTPError(url, code, "boom", hdrs, None)


def _seal(monkeypatch, failing_host: str, *, code: int = 403, rate_limited: bool = True):
    """Make exactly one host fail; every other request raises so a test can never
    silently reach the real network."""
    def fake(req, *a, **k):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if failing_host in url:
            raise _http_error(url, code, rate_limited=rate_limited)
        raise AssertionError(f"unstubbed network call to {url}")
    monkeypatch.setattr(urllib.request, "urlopen", fake)


# ---------------------------------------------------------------------------
# the seam itself
# ---------------------------------------------------------------------------
def test_a_404_is_a_checked_absence_and_carries_no_error(monkeypatch):
    """The registry ANSWERED. 'No such package' is a fact about the tool."""
    _seal(monkeypatch, "example.test", code=404, rate_limited=False)
    data, err = R._fetch_json("https://example.test/x")
    assert data is None
    assert err == ""          # empty error == the probe reached a conclusion


def test_a_rate_limit_is_unchecked_and_names_itself(monkeypatch):
    """403-with-quota-exhausted says nothing about the tool, and must not pretend to."""
    _seal(monkeypatch, "example.test", code=403, rate_limited=True)
    data, err = R._fetch_json("https://example.test/x")
    assert data is None
    assert err == "rate_limited"


def test_a_real_403_is_not_mistaken_for_a_rate_limit(monkeypatch):
    """GitHub returns 403 for BOTH an exhausted quota and a genuine denial. They mean
    opposite things ('come back later' vs 'this is private'), so they must not merge."""
    _seal(monkeypatch, "example.test", code=403, rate_limited=False)
    _, err = R._fetch_json("https://example.test/x")
    assert err == "HTTP 403 (forbidden)"


def test_an_unreachable_host_is_unchecked(monkeypatch):
    def fake(req, *a, **k):
        raise urllib.error.URLError("no route to host")
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    data, err = R._fetch_json("https://example.test/x")
    assert data is None
    assert "unreachable" in err


# ---------------------------------------------------------------------------
# the probes carry it through
# ---------------------------------------------------------------------------
def test_conda_probe_reports_an_unreachable_channel_rather_than_absence(monkeypatch):
    _seal(monkeypatch, "anaconda.org")
    probe = R.probe_conda("samtools")
    assert probe["available"] is False          # fail CLOSED — unchecked never means yes
    assert "rate_limited" in probe["probe_error"]


def test_conda_probe_stays_silent_on_a_genuine_miss(monkeypatch):
    """A clean 404 from both channels is a finding. It must NOT be decorated with an
    error, or every honest miss would read as a broken probe and the signal would rot."""
    _seal(monkeypatch, "anaconda.org", code=404, rate_limited=False)
    probe = R.probe_conda("definitely-not-a-package")
    assert probe == {"available": False}


def test_github_repo_probe_does_not_report_a_rate_limit_as_a_missing_repo(monkeypatch):
    """binary, source AND synthesis all rest on this one call, so a 403 here silently
    deletes three tiers from the ladder."""
    _seal(monkeypatch, "api.github.com")
    probe = R.probe_github("nanoporetech/dorado")
    assert probe["repo_exists"] is False        # fail closed
    assert probe["probe_error"] == "rate_limited"


def test_github_search_does_not_report_a_rate_limit_as_no_candidates(monkeypatch):
    """Search is the tightest quota on the API (10/min unauthenticated) and it is the
    INVESTIGATION's own probe: a caller refusing on an empty result would be reporting a
    rate limit as a finding about the world."""
    _seal(monkeypatch, "api.github.com")
    out = R.probe_github_search("dorado")
    assert out["found"] is False
    assert out["candidates"] == []
    assert out["probe_error"] == "rate_limited"


def test_a_github_token_is_sent_when_the_environment_offers_one(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
    assert R._headers("https://api.github.com/x")["Authorization"] == "Bearer ghp_secret"
    # ...and never leaks to a non-github host
    assert "Authorization" not in R._headers("https://pypi.org/pypi/x/json")


def test_no_token_is_not_an_error(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    assert "Authorization" not in R._headers("https://api.github.com/x")


# ---------------------------------------------------------------------------
# the decision CONSUMES it — a recorded error nobody reads is the bug, not the fix
# ---------------------------------------------------------------------------
def _stub_probes(monkeypatch, **tiers):
    for fn, key in (("probe_conda", "conda"), ("probe_pypi", "pip"),
                    ("probe_cran", "cran"), ("probe_bioconductor", "bioconductor")):
        monkeypatch.setattr(R, fn, lambda n, t=12, _k=key: tiers.get(_k, {"available": False}))
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: {})


def test_an_unreachable_registry_does_not_become_a_dead_end(monkeypatch):
    """THE REGRESSION. Discovery's whole warrant is 'the registries dead-ended'. An
    unreachable registry is not a dead end — we did not investigate, we failed to ask.
    Auto-adopting a repo on that basis is reaching for the nuclear option first."""
    _stub_probes(monkeypatch, conda={"available": False, "probe_error": "rate_limited"})
    monkeypatch.setattr(R, "probe_github_search",
                        lambda *a, **k: pytest.fail("discovery fired on an UNCHECKED dead-end"))
    d = R.resolve("samtools")
    assert d["chosen"] is None
    assert d["install_call"] is None
    assert d["unchecked_tiers"] == {"conda": "rate_limited"}
    # and it must not read as a refusal: a refusal is a conclusion, and we reached none
    assert "NOT A REFUSAL" in d["rationale"]


def test_a_genuine_dead_end_still_reaches_discovery(monkeypatch):
    """The guard above must not disarm discovery for the case it exists to serve
    (GAPIT3 lives at jiabowang/GAPIT3 and on no registry)."""
    _stub_probes(monkeypatch)          # every tier: a clean, checked miss
    called = []
    monkeypatch.setattr(R, "probe_github_search",
                        lambda *a, **k: called.append(1) or {"found": False, "candidates": []})
    R.resolve("gapit3")
    assert called, "a CHECKED dead-end must still be investigated"


def test_an_unchecked_higher_tier_is_disclosed_in_the_install_call(monkeypatch):
    """A warning parked in a sibling key is a warning that gets skipped — install_call is
    the field an agent copies and runs."""
    _stub_probes(monkeypatch,
                 conda={"available": False, "probe_error": "rate_limited"},
                 pip={"available": True, "latest": "1.0", "summary": "fast vcf parsing with htslib"})
    d = R.resolve("cyvcf2")
    assert d["chosen"] == "pip"
    assert d["unchecked_tiers"] == {"conda": "rate_limited"}
    assert "UNCHECKED TIER(S) RANKED ABOVE THIS PICK" in d["install_call"]
    assert "GITHUB_TOKEN" not in d["install_call"]      # not a github quota; don't misadvise


def test_an_unchecked_tier_BELOW_the_pick_is_not_noise(monkeypatch):
    """A tier that could not have won anyway is not worth a warning. Alarm fatigue is how
    a real warning gets skipped, so the disclosure must stay scarce enough to be read."""
    _stub_probes(monkeypatch,
                 conda={"available": True, "channel": "bioconda", "latest": "1.21",
                        "summary": "Tools for dealing with SAM, BAM and CRAM files"},
                 cran={"available": False, "probe_error": "rate_limited"})
    d = R.resolve("samtools")
    assert d["chosen"] == "conda"
    assert "UNCHECKED" not in (d["install_call"] or "")
    assert d["unchecked_tiers"] == {"cran": "rate_limited"}   # recorded, just not shouted


def test_a_refusal_states_its_absent_fields_rather_than_omitting_them(monkeypatch):
    """`None` is a value a producer must STATE (the ShippedBinary rule, one layer over).
    An absent key cannot be told apart from a producer that forgot, and every disclosure
    below hangs off reading these."""
    _stub_probes(monkeypatch)
    monkeypatch.setattr(R, "probe_github_search", lambda *a, **k: {"found": False, "candidates": []})
    d = R.resolve("nope-not-a-tool")
    assert d["chosen"] is None
    assert "install_call" in d and d["install_call"] is None
    assert "identity" in d and d["identity"] is None


def test_a_broken_authors_gate_still_reaches_a_caller_who_got_REFUSED(monkeypatch):
    """The gate's own comment claims 'either reported as fired or reported as errored;
    there is no third state'. That was false for every refusal: the note was attached only
    to install_call, which does not exist when chosen is None — so the caller was told the
    registries dead-ended while the gate had crashed. A refusal is the outcome where 'we
    could not check the authors' path' matters MOST, and it was the one that dropped it."""
    _stub_probes(monkeypatch)                       # every registry: a clean, checked miss
    monkeypatch.setattr(R, "probe_github", lambda r, t=12: {"repo_exists": False,
                                                            "has_release_assets": False,
                                                            "assets": []})
    def boom(*a, **k):
        raise TypeError("simulated call-signature drift")
    monkeypatch.setattr(R, "probe_authors_sources", boom)
    d = R.resolve("thing", github_repo="acme/thing")
    assert d["chosen"] is None                      # a REFUSAL — the case that dropped it
    assert d["install_call"] is None
    assert "AUTHORS-PATH GATE FAILED" in d["rationale"]
    assert "simulated call-signature drift" in d["rationale"]


def test_a_failed_discovery_probe_is_not_reported_as_an_unfindable_tool(monkeypatch):
    _stub_probes(monkeypatch)
    monkeypatch.setattr(R, "probe_github_search",
                        lambda *a, **k: {"found": False, "candidates": [],
                                         "probe_error": "rate_limited"})
    d = R.resolve("gapit3")
    assert d["chosen"] is None
    assert d["discovery_error"] == "rate_limited"
    assert "we established nothing" in d["rationale"]
    assert "GITHUB_TOKEN" in d["rationale"]      # the actionable next step, not just blame


# ---------------------------------------------------------------------------
# refusal_reason — the machine-readable WHY behind an ask (feedback-earn-the-refusal)
#
# `chosen: None` alone cannot tell an EARNED refusal from a LAZY one — a refusal that
# never investigated satisfies it perfectly. So a refusal now names its reason from the
# structured signals the resolve() body already recorded. These drive the REAL resolve
# path (stubbed only at the probe seam) so the classifier is tested against the decision
# the code actually builds, not a hand-made dict. The load-bearing distinction is
# UNREACHABLE (investigation_incomplete) vs ABSENT (investigation_empty): the same
# "we failed to look" ≠ "there is nothing" that the whole probe-honesty file exists for.
# ---------------------------------------------------------------------------
def test_refusal_reason_is_empty_when_every_tier_answered_and_found_nothing(monkeypatch):
    """The one genuine dead end: reachable tiers all missed AND discovery reached and
    found nothing. Only here may we claim 'we looked and there is nothing'."""
    _stub_probes(monkeypatch)          # every registry: a clean, CHECKED miss
    monkeypatch.setattr(R, "probe_github_search",
                        lambda *a, **k: {"found": False, "candidates": []})
    d = R.resolve("nope-not-a-tool")
    assert d["chosen"] is None
    assert d["refusal_reason"] == "investigation_empty"


def test_refusal_reason_is_incomplete_when_a_higher_tier_was_unreachable(monkeypatch):
    """An unreachable probe is not a probe that said no. Calling that 'empty' is the exact
    lie this file exists to prevent, so it gets its own reason — never investigation_empty."""
    _stub_probes(monkeypatch, conda={"available": False, "probe_error": "rate_limited"})
    # discovery is skipped on an UNCHECKED dead-end, so no need to stub probe_github_search
    d = R.resolve("samtools")
    assert d["chosen"] is None
    assert d["refusal_reason"] == "investigation_incomplete"


def test_refusal_reason_is_incomplete_when_the_discovery_probe_itself_failed(monkeypatch):
    """The github fallback search failing is also an incomplete investigation — 'found
    nothing' is unavailable as a claim when the search never answered."""
    _stub_probes(monkeypatch)
    monkeypatch.setattr(R, "probe_github_search",
                        lambda *a, **k: {"found": False, "candidates": [],
                                         "probe_error": "rate_limited"})
    d = R.resolve("gapit3")
    assert d["chosen"] is None
    assert d["refusal_reason"] == "investigation_incomplete"


def test_refusal_reason_is_contradicted_when_a_same_name_hit_is_disqualified(monkeypatch):
    """github_repo is authoritative intent. A same-name PyPI hit whose metadata does not
    reference it is positive CONTRARY evidence — stronger than a bare empty."""
    _stub_probes(monkeypatch,
                 pip={"available": True, "latest": "1.0",
                      "summary": "an unrelated same-name package"})
    monkeypatch.setattr(R, "probe_github",
                        lambda r, t=12: {"repo_exists": False,
                                         "has_release_assets": False, "assets": []})
    d = R.resolve("thing", github_repo="acme/thing")
    assert d["chosen"] is None
    assert d["cross_namespace_collisions"], "the collision guard must have fired"
    assert d["refusal_reason"] == "investigation_contradicted"


def test_refusal_reason_is_needs_user_input_when_discovery_found_only_weak_candidates(monkeypatch):
    """Discovery surfaced candidate repos but none dominant enough to auto-adopt (the
    same-name-repo-is-still-a-guess risk). We refuse to guess and ask the user to confirm."""
    _stub_probes(monkeypatch)
    monkeypatch.setattr(R, "probe_github_search",
                        lambda *a, **k: {"found": True,
                                         "candidates": [{"repo": "stranger/thinglike",
                                                         "stars": 3,
                                                         "exact_name_match": False}]})
    d = R.resolve("gapit3")
    assert d["chosen"] is None
    assert d["discovered_repos"], "discovery must have surfaced candidates"
    assert d["refusal_reason"] == "needs_user_input"


def test_refusal_reason_unreachable_outranks_contradicted(monkeypatch):
    """Precedence: a contradiction found on one tier does not make an UNREACHED tier
    known-absent — the answer is still unsettled, so incomplete wins."""
    _stub_probes(monkeypatch,
                 conda={"available": False, "probe_error": "rate_limited"},
                 pip={"available": True, "latest": "1.0",
                      "summary": "an unrelated same-name package"})
    monkeypatch.setattr(R, "probe_github",
                        lambda r, t=12: {"repo_exists": False,
                                         "has_release_assets": False, "assets": []})
    d = R.resolve("thing", github_repo="acme/thing")
    assert d["chosen"] is None
    assert d["unchecked_tiers"] and d["cross_namespace_collisions"]
    assert d["refusal_reason"] == "investigation_incomplete"


def test_a_non_refusal_carries_no_refusal_reason(monkeypatch):
    """refusal_reason is stated (None) on a successful pick, never omitted — the same
    STATE-the-absence rule the identity/install_call fields follow one block over."""
    _stub_probes(monkeypatch,
                 conda={"available": True, "channel": "bioconda", "latest": "1.21",
                        "summary": "Tools for dealing with SAM, BAM and CRAM files"})
    d = R.resolve("samtools")
    assert d["chosen"] == "conda"
    assert "refusal_reason" in d and d["refusal_reason"] is None


def test_a_pypi_cran_name_collision_refuses_instead_of_tipping_to_pip(monkeypatch):
    """A bare name on BOTH PyPI and CRAN is two different packages (PyPI `ape`
    build-system ≠ CRAN `ape` phylogenetics). With no language hint and no conda pick,
    tipping to pip is a confident wrong-ecosystem guess — refuse and ASK."""
    _stub_probes(monkeypatch,
                 pip={"available": True, "latest": "0.7", "summary": "a python build tool"},
                 cran={"available": True, "latest": "5.7", "summary": "phylogenetics in R"})
    d = R.resolve("ape")
    assert d["chosen"] is None            # NOT pip — the collision is unresolved
    assert d["ambiguous"] is True
    assert d["refusal_reason"] == "needs_user_input"
    assert d["install_call"] is None      # no wrong-ecosystem call handed to the caller
    # both options stay visible so the caller knows what to disambiguate between
    assert "language" in d["rationale"]


def test_a_language_hint_dissolves_the_collision_and_picks(monkeypatch):
    """The ask is cheap and specific; supplying it must PROCEED, not keep refusing."""
    _stub_probes(monkeypatch,
                 pip={"available": True, "latest": "0.7", "summary": "a python build tool"},
                 cran={"available": True, "latest": "5.7", "summary": "phylogenetics in R"})
    d = R.resolve("ape", language="python")
    assert d["chosen"] == "pip"
    assert d["ambiguous"] is False
    assert d["refusal_reason"] is None


def test_conda_disambiguates_a_pypi_cran_collision_in_context(monkeypatch):
    """conda winning is itself the disambiguator — the bioinformatics-channel package is
    the in-context answer, so an ambiguous name that resolved to conda PROCEEDS."""
    _stub_probes(monkeypatch,
                 conda={"available": True, "channel": "bioconda", "latest": "5.7",
                        "summary": "Analyses of Phylogenetics and Evolution"},
                 pip={"available": True, "latest": "0.7", "summary": "a python build tool"},
                 cran={"available": True, "latest": "5.7", "summary": "phylogenetics in R"})
    d = R.resolve("ape")
    assert d["chosen"] == "conda"         # conda is the pick; the pip/cran collision is moot
    assert d["refusal_reason"] is None


# ---------------------------------------------------------------------------
# version existence — a requested version the chosen tier does not carry must not ship as
# a byte-identical unsolvable pin (samtools=9.99 looks exactly like a valid samtools=1.21).
# ---------------------------------------------------------------------------
def test_a_requested_version_absent_from_the_chosen_tier_refuses(monkeypatch):
    _stub_probes(monkeypatch,
                 conda={"available": True, "channel": "bioconda", "latest": "1.21",
                        "versions": ["1.17", "1.19", "1.20", "1.21"],
                        "summary": "SAM/BAM tools"})
    d = R.resolve("samtools", version="9.99")
    assert d["chosen"] is None
    assert d["refusal_reason"] == "investigation_contradicted"
    assert d["install_call"] is None                       # no byte-identical unsolvable pin
    assert d["version_absent"]["requested"] == "9.99"
    assert d["version_absent"]["nearest"]                  # nearest real versions surfaced
    assert "VERSION NOT FOUND" in d["rationale"]


def test_a_real_requested_version_still_resolves(monkeypatch):
    """THE safety property: a version that DOES exist must never be refused, and the pin
    must survive into the install_call unchanged."""
    _stub_probes(monkeypatch,
                 conda={"available": True, "channel": "bioconda", "latest": "1.21",
                        "versions": ["1.17", "1.19", "1.20", "1.21"],
                        "summary": "SAM/BAM tools"})
    d = R.resolve("samtools", version="1.20")
    assert d["chosen"] == "conda"
    assert d["refusal_reason"] is None
    assert "samtools=1.20" in d["install_call"]


def test_version_existence_normalises_pep440(monkeypatch):
    """A request for 1.0 matches a stored 1.0.0 (PyPI normalises) — not a false refusal."""
    _stub_probes(monkeypatch,
                 pip={"available": True, "latest": "1.0.0",
                      "versions": ["0.9.0", "1.0.0"], "summary": "x"})
    d = R.resolve("somepkg", version="1.0")
    assert d["chosen"] == "pip"
    assert d["refusal_reason"] is None


def test_version_existence_normalises_conda_underscore_revision(monkeypatch):
    """conda encodes an R revision separator as '_' — R's `5.8-1` ships on conda-forge as
    `5.8_1`, which PEP440 CANNOT parse, so a request for `5.8.1` was falsely refused as
    version-absent even though that release exists (the ape regression). The three forms of
    one release must unify."""
    _stub_probes(monkeypatch,
                 conda={"available": True, "channel": "conda-forge", "latest": "5.8",
                        "versions": ["5.7", "5.7_1", "5.8", "5.8_1"], "summary": "phylo in R"})
    d = R.resolve("ape", version="5.8.1")
    assert d["chosen"] == "conda"
    assert d["refusal_reason"] is None
    assert "5.8.1" in d["install_call"]
    # and it must NOT become a false-positive escape hatch: a genuinely absent version still
    # refuses (5.9 is not 5.8_1).
    assert R._version_present("5.9", ["5.7", "5.8", "5.8_1"]) is False


def test_a_tier_without_a_version_list_is_not_second_guessed(monkeypatch):
    """cran surfaces no version list, so a version request there is left ALONE — absence of
    the list is 'we don't know', never 'the version is missing'."""
    _stub_probes(monkeypatch,
                 cran={"available": True, "latest": "5.7", "summary": "phylo in R"})
    d = R.resolve("someRpkg", version="3.0")
    assert d["chosen"] == "cran"
    assert d["refusal_reason"] is None
    assert "version_absent" not in d


def test_probe_pypi_excludes_fully_yanked_versions(monkeypatch):
    """A version whose files are ALL yanked is not installable, so it must not count as
    present (anndata 0.12.15 was yanked 'released against wrong branch')."""
    payload = {"info": {"version": "1.1"}, "releases": {
        "1.0":   [{"yanked": False}],
        "1.0.5": [{"yanked": True}, {"yanked": True}],   # fully yanked → absent
        "1.1":   [{"yanked": False}],
        "1.2":   [],                                     # no files → absent
    }}
    monkeypatch.setattr(R, "_fetch_json", lambda url, t=12: (payload, ""))
    out = R.probe_pypi("anything")
    assert out["available"] is True
    assert set(out["versions"]) == {"1.0", "1.1"}


# ---------------------------------------------------------------------------
# BIOCONDUCTOR — the field's core R stats tools (DESeq2, edgeR, limma) live ONLY on bioconda
# under a `bioconductor-{name}` prefix: never the bare name, and not on CRAN. Without a
# bioconductor-aware conda probe, a bare `deseq2` misses every registry and slides to github
# synthesis, `edger` resolves to a same-name junk PyPI package ("Redirect Microsoft Edge to
# your preferred browser") — a clean-green install of the WRONG tool — and `limma` refuses as
# an unresolved pip/cran collision. The fold routes the hit INTO the conda tier so it wins at
# conda's rank AND disambiguates in context. These drive the REAL resolve() path, stubbed only
# at the probe functions with a NAME-AWARE conda so bare-vs-`bioconductor-` is distinguishable.
# ---------------------------------------------------------------------------
def _bioc_stubs(monkeypatch, *, on_bioconda=frozenset(), bare_conda=None,
                pip=None, cran=None, bioc_error=None):
    """Name-aware probe stubs for the bioconductor→conda fold. Bare `<name>` conda MISSES
    (unless `bare_conda` given); `bioconductor-<name>` HITS iff <name> in `on_bioconda`, a
    clean 404 miss otherwise — or an ERROR when `bioc_error` is set (the transient-blip case)."""
    def fake_conda(name, t=12):
        low = name.lower()
        if low.startswith("bioconductor-"):
            if bioc_error is not None:
                return {"available": False, "probe_error": bioc_error}
            if low[len("bioconductor-"):] in on_bioconda:
                return {"available": True, "channel": "bioconda", "latest": "1.42.0",
                        "versions": ["1.40.0", "1.42.0"],
                        "summary": f"Bioconductor package {low}"}
            return {"available": False}
        return dict(bare_conda) if bare_conda else {"available": False}
    monkeypatch.setattr(R, "probe_conda", fake_conda)
    monkeypatch.setattr(R, "probe_pypi", lambda n, t=12: dict(pip) if pip else {"available": False})
    monkeypatch.setattr(R, "probe_cran", lambda n, t=12: dict(cran) if cran else {"available": False})
    monkeypatch.setattr(R, "probe_bioconductor", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: {})
    # A conda hit is a settled ROUTING decision — the DISCOVERY branch (which re-enters
    # resolve with the found repo and can change the pick) must never fire on it, and the
    # assertion for that is now `discovered_repos`/`auto_discovered` being absent, checked
    # per test. This stub used to `pytest.fail` on the search itself, which conflated the
    # two things `name_corroboration` (2026-08-07) separated: WHETHER we look, and whether
    # what we find may move the pick. The corroboration search runs on a hit deliberately —
    # a registry entry proves the NAME is taken, not that it is the tool — and it can only
    # add a disclosure. It answers "nothing else owns this name" here so these tests stay
    # about the bioconductor fold.
    monkeypatch.setattr(R, "probe_github_search",
                        lambda *a, **k: {"found": False, "candidates": []})


def test_deseq2_routes_to_the_bioconda_bioconductor_package(monkeypatch):
    """The headline case: a bare `deseq2` must resolve to the curated bioconda package, not
    slide to a github-source synthesis of the devel branch."""
    _bioc_stubs(monkeypatch, on_bioconda={"deseq2"})
    d = R.resolve("deseq2")
    assert d["chosen"] == "conda"
    assert d["probed"]["conda"]["bioc_spec"] == "bioconductor-deseq2"
    assert '"spec": "bioconductor-deseq2=1.42.0"' in d["install_call"]
    assert '"channel": "bioconda"' in d["install_call"]
    assert d["refusal_reason"] is None
    # A CONDA HIT SETTLES THE ROUTING. The github name search still runs (it can only
    # disclose), but the DISCOVERY branch — the one that re-enters resolve with a found
    # repo and can move the pick — must not fire on a tier that answered.
    assert "discovered_repos" not in d and not d.get("auto_discovered")
    # NAME-MAPPING HONESTY: the caller typed `deseq2`, the call installs `bioconductor-deseq2`
    # — the rationale must say so up front, never a silent substitution.
    assert d["rationale"].startswith("BIOCONDUCTOR:")
    assert "bioconductor-deseq2" in d["rationale"]
    # deseq2 is NOT on CRAN (no cran stub) — the "not on CRAN" clause is TRUE here and present.
    assert "not on CRAN" in d["rationale"]


def test_edger_beats_a_same_name_junk_pypi_package(monkeypatch):
    """The wrong-tool-green trap: PyPI `edger` is a browser-redirect utility. bioconda's
    `bioconductor-edger` must win over it — conda outranks pip."""
    _bioc_stubs(monkeypatch, on_bioconda={"edger"},
                pip={"available": True, "latest": "0.1.3",
                     "summary": "Redirect Microsoft Edge to your preferred browser"})
    d = R.resolve("edger")
    assert d["chosen"] == "conda"                       # NOT pip — the wrong tool
    assert "bioconductor-edger" in d["install_call"]
    assert "install_pip_package" not in d["install_call"]


def test_limma_resolves_instead_of_refusing_as_ambiguous(monkeypatch):
    """bare `limma` is a pip∧cran collision (a junk PyPI ESP8266 controller vs the real CRAN
    mirror) → today a needs_user_input refusal. A `bioconductor-limma` conda hit disambiguates
    in context, exactly as conda does for the `ape` collision."""
    _bioc_stubs(monkeypatch, on_bioconda={"limma"},
                pip={"available": True, "latest": "0.2.5", "summary": "an ESP8266 controller"},
                cran={"available": True, "latest": "2.10.7",
                      "summary": "Linear Models for Microarray Data"})
    d = R.resolve("limma")
    assert d["chosen"] == "conda"
    assert "bioconductor-limma" in d["install_call"]
    assert d["refusal_reason"] is None                  # NOT needs_user_input
    # a name we RESOLVED must not carry a "pass a hint to disambiguate" instruction for a pick
    # already made — that gated note is the report-honesty lie this exists to kill.
    assert "disambiguate" not in d["rationale"]
    assert d["rationale"].startswith("BIOCONDUCTOR:")
    # limma IS on CRAN (an archived mirror) — the note must NOT hardcode "not on CRAN" and
    # contradict the `cran` it lists as a lower-priority alternative on the same line.
    assert "not on CRAN" not in d["rationale"]


def test_a_non_bioconductor_miss_stays_a_clean_dead_end(monkeypatch):
    """The fold must be a CHECKED probe, not a guess: a tool on neither bare conda nor
    `bioconductor-*` gains no phantom conda hit (bioconductor-samtools 404s), and still
    reaches discovery like any other genuine dead-end."""
    _bioc_stubs(monkeypatch, on_bioconda=frozenset())   # nothing on bioconda
    reached = []
    monkeypatch.setattr(R, "probe_github_search",
                        lambda *a, **k: reached.append(1) or {"found": False, "candidates": []})
    d = R.resolve("not-a-bioc-tool")
    assert d["probed"]["conda"]["available"] is False
    assert d["chosen"] is None
    assert reached, "a CHECKED dead-end must still be investigated"


def test_language_python_hint_suppresses_the_bioconductor_fallback(monkeypatch):
    """An explicit `language='python'` means the caller wants the PyPI package — do NOT
    rescue it into a bioconda R package behind their back."""
    _bioc_stubs(monkeypatch, on_bioconda={"deseq2"},
                pip={"available": True, "latest": "1.0", "summary": "a python package"})
    d = R.resolve("deseq2", language="python")
    assert d["chosen"] == "pip"
    assert "bioconductor" not in (d["install_call"] or "")


def test_a_transient_bioconductor_probe_error_is_disclosed_not_swallowed(monkeypatch):
    """ABSENT ≠ UNCHECKED. When the `bioconductor-{name}` probe ERRORS (a transient anaconda
    403, not 'answered no'), bioconda is NOT cleanly ruled out — the conda tier must land in
    unchecked_tiers and the disclosure must reach the install_call, never silently ship the
    same-name junk pip package. Measured live: one blip on this exact probe routed `edger` to
    install_pip_package(edger) — the wrong tool, green."""
    _bioc_stubs(monkeypatch, bioc_error="rate_limited",
                pip={"available": True, "latest": "0.1.3",
                     "summary": "Redirect Microsoft Edge to your preferred browser"})
    d = R.resolve("edger")
    assert d["chosen"] == "pip"                          # pip is the only AVAILABLE tier...
    assert d["unchecked_tiers"] == {"conda": "rate_limited"}   # ...but conda wasn't ruled out
    assert "UNCHECKED TIER(S) RANKED ABOVE THIS PICK" in d["install_call"]


def test_language_r_hint_also_reaches_the_bioconductor_fold(monkeypatch):
    """The `language='r'` branch probes conda as `r-{name}` (misses for a Bioconductor tool),
    so the fold must apply there too — a hinted `deseq2` still gets the bioconda package, not
    the github r_github fallback."""
    _bioc_stubs(monkeypatch, on_bioconda={"deseq2"})
    d = R.resolve("deseq2", language="r")
    assert d["chosen"] == "conda"
    assert "bioconductor-deseq2" in d["install_call"]


def test_a_forced_non_conda_pick_does_not_advertise_the_conda_biocontainer(monkeypatch):
    """The fold makes availability['conda'] available even when conda is NOT the pick (e.g.
    prefer='pip'). The conda biocontainer must then NOT be surfaced — else the decision
    advertises a `bioconductor-edger` adopt_call beside an `install_pip_package(edger)` (PyPI's
    edger is a browser-redirect utility): one decision naming two different tools."""
    _bioc_stubs(monkeypatch, on_bioconda={"edger"},
                pip={"available": True, "latest": "0.1.3",
                     "summary": "Redirect Microsoft Edge to your preferred browser"})
    # a biocontainer DOES exist for bioconductor-edger — the guard is `chosen`, not existence.
    monkeypatch.setattr(R, "_resolve_biocontainer",
                        lambda specs, timeout=12: {
                            "found": True, "image": "quay.io/biocontainers/bioconductor-edger:x",
                            "image_by_digest": "quay.io/biocontainers/bioconductor-edger@sha256:dead",
                            "digest": "sha256:dead"})
    d = R.resolve("edger", prefer="pip")
    assert d["chosen"] == "pip"
    assert d["pullable_image"]["found"] is False        # NOT the bioconductor-edger image
    assert "BioContainer" not in d["rationale"]
    # control: without the prefer override, conda IS the pick and DOES surface its biocontainer.
    d2 = R.resolve("edger")
    assert d2["chosen"] == "conda"
    assert d2["pullable_image"]["found"] is True
    assert d2["pullable_image"]["source"] == "biocontainer"
