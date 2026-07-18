"""Fork-anchor / declared-repo reconciliation — a user-named repo must decide identity.

WHY THIS FILE EXISTS. A user who passes `github_repo=` is asserting authoritative intent
("build THIS project"). Before this anchor, a same-name conda package silently won even
when it builds a DIFFERENT repo: naming the `populationgenomics/bcftools` csq-fork shipped
upstream `samtools/bcftools`, dropping the exact fork changes that were the point (this
project's own Talos war story). resolve() now reconciles the named repo against the repo
the registry would build, split by the fork edge:

  • a DIVERGENT FORK of the packaged tool  → reroute to the fork's own build (synthesis)
  • a DISTINCT same-name lineage (no fork)  → REFUSE and ask (investigation_contradicted)
  • CONVERGENCE (same repo after 301-follow) → inert, conda wins as today

The load-bearing property, and the one the adversarial red-team broke every earlier draft
on: the reconciliation stands on live GitHub calls sharing the resolver's 60/hr quota, so
an UNCHECKED probe (rate-limit/timeout) must degrade to keep-conda-and-DISCLOSE — NEVER a
refuse and NEVER a reroute. Rendering "we could not look" as "the repos conflict" is the
Rule-2 lie the `(payload,error)` seam exists to prevent. Each test names what to break.

Registry/GitHub facts are stubbed (real shapes), so these run offline over the real
resolve() path; the live corpus rows (`fork-is-the-point`, `true-mismatch-...`) are the
end-to-end proof and carry their own rate-limit preconditions.
"""
from __future__ import annotations

from agent.skills import resolver as R


def _wire(monkeypatch, *, conda=None, gh=None, canon=None, diverges=None,
          pip=None, cran=None):
    """Stub registries + the three GitHub-backed probes; drive the REAL resolve()."""
    monkeypatch.setattr(R, "probe_conda", lambda n, t=12: conda or {"available": False})
    monkeypatch.setattr(R, "probe_pypi", lambda n, t=12: pip or {"available": False})
    monkeypatch.setattr(R, "probe_cran", lambda n, t=12: cran or {"available": False})
    monkeypatch.setattr(R, "probe_bioconductor", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: {})   # no author tier
    monkeypatch.setattr(R, "probe_github", lambda repo, t=12: dict(gh or {"repo_exists": False}))
    monkeypatch.setattr(R, "_canon_repo", lambda repo, t=12: canon or (repo.lower(), "ok"))
    monkeypatch.setattr(R, "_fork_diverges",
                        lambda p, f, d, t=12: diverges if diverges is not None else (None, "unstubbed"))


def _conda(repo, latest="1.21"):
    return {"available": True, "channel": "bioconda", "latest": latest,
            "summary": "packaged tool", "repo": repo, "repo_source": "conda"}


def _gh(full_name, *, is_fork=False, parent="", upstream="", default_branch="main",
        repo_exists=True, has_release_assets=False, probe_error=None):
    out = {"repo_exists": repo_exists, "has_release_assets": has_release_assets, "assets": [],
           "is_fork": is_fork, "parent": parent, "upstream": upstream,
           "full_name": full_name, "default_branch": default_branch}
    if probe_error:
        out["probe_error"] = probe_error
        out["repo_exists"] = False
    return out


# ── 1. divergent fork of the packaged tool → reroute (disqualify registry tiers) ──

def test_divergent_fork_reroutes_off_conda(monkeypatch):
    """The bcftools bite: user names the csq-fork; it diverges from samtools/bcftools, so
    conda is disqualified and we route to the fork's own build. Break by returning the
    fork's divergence as (False,...) and watch conda win, shipping the parent."""
    _wire(monkeypatch, conda=_conda("samtools/bcftools"),
          gh=_gh("populationgenomics/bcftools", is_fork=True, parent="samtools/bcftools",
                 default_branch="develop"),
          canon=("samtools/bcftools", "ok"), diverges=(True, ""))
    d = R.resolve("bcftools", github_repo="populationgenomics/bcftools")
    assert d["chosen"] not in ("conda", "pip", "cran", "bioconductor")
    assert d["chosen"] in ("synthesis", "source", "binary")
    assert d["fork_divergence"]["status"] == "diverged"
    assert d["probed"]["conda"].get("fork_divergence_disqualified") is True


def test_transitive_fork_uses_upstream_root(monkeypatch):
    """A fork-of-a-fork has an immediate parent that isn't the packaged tool; GitHub's
    `source` (root) is. We fall back to `upstream` when `parent` is empty. Break by
    dropping the `or gh.get('upstream')` and watch a transitive fork refuse instead."""
    _wire(monkeypatch, conda=_conda("samtools/bcftools"),
          gh=_gh("deep/fork-bcftools", is_fork=True, parent="", upstream="samtools/bcftools"),
          canon=("samtools/bcftools", "ok"), diverges=(True, ""))
    d = R.resolve("bcftools", github_repo="deep/fork-bcftools")
    assert d["chosen"] in ("synthesis", "source", "binary")
    assert d.get("fork_divergence")


# ── 2. distinct same-name lineage (not a fork) → refuse + ask ─────────────────────

def test_distinct_lineage_refuses_contradicted(monkeypatch):
    """pyranges1: a v1 rewrite, NOT a GitHub fork of the packaged pyranges0. Two live
    lineages that don't reconcile → refuse. Break by returning is_fork=True and watch it
    silently reroute a genuine contradiction into a synthesis build."""
    _wire(monkeypatch, conda=_conda("pyranges/pyranges0"),
          gh=_gh("pyranges/pyranges1", is_fork=False),
          canon=("pyranges/pyranges0", "ok"))
    d = R.resolve("pyranges", github_repo="pyranges/pyranges1")
    assert d["chosen"] is None
    assert d["declared_repo_contradiction"]["user_repo"] == "pyranges/pyranges1"
    assert d["refusal_reason"] == "investigation_contradicted"


# ── 3. convergence (same repo after a 301-follow) → inert, conda wins ─────────────

def test_canon_convergence_is_inert(monkeypatch):
    """User pastes the canonical name; conda's stale dev_url (endrebak/pyranges) canon-
    follows to the same repo → converge → conda proceeds untouched. Break the 301-follow
    (raw-lower) and this becomes a false contradiction — the razor's own safe side."""
    _wire(monkeypatch, conda=_conda("endrebak/pyranges"),
          gh=_gh("pyranges/pyranges0", is_fork=False),
          canon=("pyranges/pyranges0", "ok"))   # endrebak/pyranges 301→ pyranges/pyranges0
    d = R.resolve("pyranges", github_repo="pyranges/pyranges0")
    assert d["chosen"] == "conda"
    assert not d.get("declared_repo_contradiction")


def test_same_repo_named_is_inert(monkeypatch):
    """samtools + github_repo=samtools/samtools (== conda dev_url) → conda, no drama."""
    _wire(monkeypatch, conda=_conda("samtools/samtools"),
          gh=_gh("samtools/samtools", is_fork=False),
          canon=("samtools/samtools", "ok"))
    d = R.resolve("samtools", github_repo="samtools/samtools")
    assert d["chosen"] == "conda"


# ── 4. UNCHECKED never refuses / never reroutes — the red-team headline ───────────

def test_unchecked_canon_keeps_conda_and_discloses(monkeypatch):
    """A canon() that could not resolve R_conda (rate-limit) must NOT manufacture a
    contradiction. Keep conda, disclose. Break by treating cc_status 'error' as a mismatch
    and watch a rate limit refuse a valid install (the Rule-2 violation)."""
    _wire(monkeypatch, conda=_conda("pyranges/pyranges0"),
          gh=_gh("pyranges/pyranges1", is_fork=False),
          canon=("pyranges/pyranges0", "error"))
    d = R.resolve("pyranges", github_repo="pyranges/pyranges1")
    assert d["chosen"] == "conda"
    assert not d.get("declared_repo_contradiction")
    assert "could not be verified" in d["rationale"]


def test_unchecked_fork_divergence_keeps_conda_and_discloses(monkeypatch):
    """A fork whose divergence probe rate-limited must NOT reroute (that would strand the
    user off conda on an unverified guess). Keep conda, disclose. Break by treating
    (None, err) as diverged."""
    _wire(monkeypatch, conda=_conda("samtools/bcftools"),
          gh=_gh("populationgenomics/bcftools", is_fork=True, parent="samtools/bcftools"),
          canon=("samtools/bcftools", "ok"), diverges=(None, "rate_limited"))
    d = R.resolve("bcftools", github_repo="populationgenomics/bcftools")
    assert d["chosen"] == "conda"
    assert not d.get("fork_divergence")
    assert "could not be verified" in d["rationale"]


def test_pristine_fork_keeps_conda_silently(monkeypatch):
    """A fork identical-to/behind its parent adds nothing → conda's build is equivalent →
    keep conda, no reroute, no noise."""
    _wire(monkeypatch, conda=_conda("samtools/bcftools"),
          gh=_gh("someone/bcftools", is_fork=True, parent="samtools/bcftools"),
          canon=("samtools/bcftools", "ok"), diverges=(False, ""))
    d = R.resolve("bcftools", github_repo="someone/bcftools")
    assert d["chosen"] == "conda"
    assert not d.get("fork_divergence")


def test_user_repo_probe_error_skips_reconciliation(monkeypatch):
    """If probe_github on the user's repo itself never answered, the whole block is skipped
    (fail closed to today's behavior) — no reconciliation on an unchecked user repo."""
    _wire(monkeypatch, conda=_conda("samtools/bcftools"),
          gh=_gh("populationgenomics/bcftools", probe_error="rate_limited"))
    d = R.resolve("bcftools", github_repo="populationgenomics/bcftools")
    assert d["chosen"] == "conda"
    assert not d.get("fork_divergence") and not d.get("declared_repo_contradiction")


def test_conda_declares_no_repo_still_reroutes_a_divergent_fork(monkeypatch):
    """Decoupled from conda-declares-a-repo (red-team fix): a confirmed divergent fork is a
    hard pin even when the conda recipe names no repo at all (bedtools/trinity class)."""
    conda_no_repo = {"available": True, "channel": "bioconda", "latest": "1.0",
                     "summary": "packaged tool"}   # no 'repo' key
    _wire(monkeypatch, conda=conda_no_repo,
          gh=_gh("someone/toolx", is_fork=True, parent="upstream/toolx"),
          diverges=(True, ""))
    d = R.resolve("toolx", github_repo="someone/toolx")
    assert d["chosen"] in ("synthesis", "source", "binary")
    assert d.get("fork_divergence")


# ── 5. no github_repo → the whole anchor is dormant ───────────────────────────────

def test_bare_name_never_arms_the_anchor(monkeypatch):
    """No github_repo: reconciliation must not fire — bare bcftools stays conda."""
    _wire(monkeypatch, conda=_conda("samtools/bcftools"))
    d = R.resolve("bcftools")
    assert d["chosen"] == "conda"
    assert not d.get("fork_divergence") and not d.get("declared_repo_contradiction")
