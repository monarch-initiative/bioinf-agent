"""Vendor-CDN discovery — a release with no attached assets is not "these authors ship
no binary".

MEASURED (Phase D, 2026-08-02). `nanoporetech/dorado` at v2.1.1 carries **0 release
assets** and a 402-byte release body with **no URLs in it**, so the binary tier — whose
availability was `gh["has_release_assets"]` and nothing else — reported unavailable and
dorado fell through to a multi-hour CUDA source build. Its README names
`https://cdn.oxfordnanoportal.com/software/analysis/dorado-2.1.1-linux-x64.tar.gz`
(3.47 GB, HEAD 200). `install_release_binary` and `resolve_linux_asset` route B already
accepted a direct vendor URL: the PRIMITIVE was fine, the PROBE was github-shaped.

The hazard this file mostly guards is the one that makes the fix worse than the gap if
it is got wrong. A release asset is pinned to a TAG; a README lives on the DEFAULT
BRANCH and names whatever the authors last shipped. Serving a README URL for a pinned
version is the somalier 0.2.15 -> v0.3.3 failure class — latest's bytes wearing the
requested version's name, fully green.
"""

from __future__ import annotations

import pytest

from agent.skills import resolver as R


_CDN = "https://cdn.oxfordnanoportal.com/software/analysis"
_README = f"""
# dorado
Docs at https://software-docs.nanoporetech.com/dorado/latest/
 * [linux x64]({_CDN}/dorado-2.1.1-linux-x64.tar.gz)
 * [linux arm64]({_CDN}/dorado-2.1.1-linux-arm64.tar.gz)
 * [osx arm64]({_CDN}/dorado-2.1.1-osx-arm64.zip)
 * [win64]({_CDN}/dorado-2.1.1-win64.zip)
Source: https://github.com/nanoporetech/dorado/archive/refs/tags/v2.1.1.tar.gz
"""


def _readme(monkeypatch, text=_README, *, reachable=True, err=""):
    import base64
    monkeypatch.setattr(R, "_fetch_json", lambda url, t=12: (
        {"content": base64.b64encode(text.encode()).decode()}, "")
        if url.endswith("/readme") else (None, ""))
    monkeypatch.setattr(R, "_fetch_ok", lambda url, t=12: (reachable, err))


def test_the_platform_pick_comes_from_the_readme_not_a_second_rule(monkeypatch):
    """Four platform builds in one README; only the linux x64 one is ours. Selection is
    `_pick_platform_asset` — the SAME function the github-asset path uses — so a foreign
    os/arch is rejected by one rule, not two that can drift."""
    _readme(monkeypatch)
    out = R.probe_readme_assets("nanoporetech/dorado")
    assert out["found"] is True
    assert out["url"] == f"{_CDN}/dorado-2.1.1-linux-x64.tar.gz"
    assert out["version"] == "2.1.1"
    assert out["reachable"] is True


def test_github_source_tarballs_are_not_vendor_builds(monkeypatch):
    """The repo's own `/archive/refs/tags/...tar.gz` ends in a downloadable suffix and is
    SOURCE, not a build. A release asset would belong to the tier above; anything else on
    github.com is a doc or source link."""
    _readme(monkeypatch)
    out = R.probe_readme_assets("nanoporetech/dorado")
    assert not any("github.com" in c for c in out["candidates"]), out["candidates"]


def test_a_doc_link_is_never_mistaken_for_a_download(monkeypatch):
    _readme(monkeypatch, "See https://software-docs.nanoporetech.com/dorado/latest/ for docs.")
    out = R.probe_readme_assets("nanoporetech/dorado")
    assert out["found"] is False and out["candidates"] == []


def test_a_readme_we_could_not_fetch_is_unchecked_never_absent(monkeypatch):
    """The whole thesis of test_probe_honesty, applied to a new probe."""
    monkeypatch.setattr(R, "_fetch_json", lambda url, t=12: (None, "rate_limited"))
    out = R.probe_readme_assets("nanoporetech/dorado")
    assert out["found"] is False and out["probe_error"] == "rate_limited"


def test_a_url_the_vendor_has_moved_is_a_hard_no(monkeypatch):
    """A 404 from the CDN is the host ANSWERING. Advertising a dead multi-GB link is
    worse than falling through to source."""
    _readme(monkeypatch, reachable=False)
    assert R.probe_readme_assets("nanoporetech/dorado")["found"] is False


def test_an_unreachable_check_still_offers_the_url_but_says_it_is_unverified(monkeypatch):
    """`reachable=None` is the third state: we could not ask. That must not delete a URL
    the authors plainly published — it must be offered and labelled."""
    _readme(monkeypatch, reachable=False, err="unreachable: timed out")
    out = R.probe_readme_assets("nanoporetech/dorado")
    assert out["found"] is True and out["reachable"] is None


# ── the pin guard: the reason this fix could be worse than the gap ──────────────

def _resolve(monkeypatch, *, version="", assets=(), tag="v2.1.1", relstatus="no_asset",
             vendor=True):
    """Stub at `probe_github`, because that is where the README lookup now lives.

    It was briefly a second probe called from resolve(), and 29 hermetic tests promptly
    reached api.github.com — the conftest network guard saying the seam was in the wrong
    place. Folding it in means every existing `probe_github` stub in this repo covers it,
    which is the property that made the churn go away."""
    for fn in ("probe_conda", "probe_pypi", "probe_cran", "probe_bioconductor"):
        monkeypatch.setattr(R, fn, lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: {})
    gh = {"repo_exists": True, "has_release_assets": bool(assets), "assets": list(assets),
          "is_fork": False, "parent": "", "upstream": "", "full_name": "nanoporetech/dorado",
          "default_branch": "master"}
    if vendor and not assets:
        gh |= {"vendor_asset": f"{_CDN}/dorado-2.1.1-linux-x64.tar.gz",
               "vendor_asset_version": "2.1.1", "vendor_asset_reachable": True,
               "asset_source": "readme"}
    monkeypatch.setattr(R, "probe_github", lambda repo, t=12: dict(gh))
    monkeypatch.setattr(R, "_canon_repo", lambda repo, t=12: (repo.lower(), "present"))
    monkeypatch.setattr(R, "_release_for_version",
                        lambda repo, v, tool, timeout=12: {
                            "status": relstatus, "tag": tag,
                            "asset": f"https://github.com/{repo}/releases/download/{tag}/x.tar.gz"})
    return R.resolve("dorado", github_repo="nanoporetech/dorado", version=version)


def test_probe_github_asks_the_readme_only_when_the_release_has_nothing(monkeypatch):
    """THE SEAM. One stub point for callers, and the better-anchored answer short-circuits
    the extra request rather than merely losing the ranking afterwards."""
    asked: list[str] = []
    monkeypatch.setattr(R, "probe_readme_assets",
                        lambda repo, t=12, *a, **k: asked.append(repo) or {"found": False})

    def _gh(has_assets):
        rel = {"assets": ([{"browser_download_url": "https://x/y-linux-x64.tar.gz"}]
                          if has_assets else [])}
        monkeypatch.setattr(R, "_fetch_json", lambda url, t=12: (
            (rel, "") if url.endswith("/releases/latest") else ({"full_name": "o/r"}, "")))
        return R.probe_github("o/r")

    _gh(True)
    assert asked == [], "a release WITH assets must not trigger a README fetch"
    _gh(False)
    assert asked == ["o/r"], "a release with no assets must fall back to the README"


def test_no_pin_reaches_the_binary_tier_instead_of_a_source_build(monkeypatch):
    """THE FINDING. Before: chosen='synthesis' (a multi-hour CUDA build) for a tool whose
    authors ship a prebuilt linux tarball."""
    _readme(monkeypatch)
    d = _resolve(monkeypatch)
    assert d["chosen"] == "binary"
    assert d["probed"]["binary"]["asset_source"] == "readme"
    assert d["install_call"].splitlines()[-1].endswith(
        f'url="{_CDN}/dorado-2.1.1-linux-x64.tar.gz", sha256="<published>")')


def test_a_matching_pin_is_honored(monkeypatch):
    """`no_asset` means the TAG EXISTS and github just carries no bytes for it — exactly
    the vendor shape. This shared one branch with `absent` and the collapse made the
    fallback unreachable for any pinned version."""
    _readme(monkeypatch)
    d = _resolve(monkeypatch, version="2.1.1")
    assert d["chosen"] == "binary" and d["probed"]["binary"]["available"] is True


def test_a_pin_the_readme_contradicts_is_REFUSED_not_quietly_served(monkeypatch):
    """THE HAZARD. The README says 2.1.1; the user asked for 2.0.0. Serving that URL
    would ship latest's bytes under the requested version's name."""
    _readme(monkeypatch)
    d = _resolve(monkeypatch, version="2.0.0")
    assert d["chosen"] != "binary"
    assert d["probed"]["binary"]["available"] is False
    assert "DOES NOT MATCH THE PIN" in d["install_call"]


def test_a_tag_that_does_not_exist_is_not_rescued_by_a_vendor_url(monkeypatch):
    """`absent` is a different fact from `no_asset`: there IS no such release, so no
    README build can stand in for it however the versions line up."""
    _readme(monkeypatch)
    d = _resolve(monkeypatch, version="2.1.1", relstatus="absent")
    assert d["probed"]["binary"]["available"] is False


def test_a_release_asset_outranks_the_readme_and_costs_no_extra_request(monkeypatch):
    """The better-anchored answer wins, and the common path must not pay a README fetch."""
    seen: list[str] = []
    monkeypatch.setattr(R, "probe_readme_assets",
                        lambda repo, *a, **k: seen.append(repo) or {"found": False})
    d = _resolve(monkeypatch, relstatus="match",
                 assets=["https://github.com/nanoporetech/dorado/releases/download/"
                         "v2.1.1/dorado-linux-x64.tar.gz"])
    assert seen == [], "the README was fetched even though the release had assets"
    assert d["chosen"] == "binary"
    assert d["probed"]["binary"].get("asset_source") is None


def test_the_weaker_anchor_is_disclosed_in_both_fields(monkeypatch):
    """A branch-tracked URL is a real answer AND a weaker one. `install_release_binary`
    sha256-pins whatever bytes it is given — WHICH bytes is the part only this note can
    tell the caller."""
    _readme(monkeypatch)
    d = _resolve(monkeypatch)
    for field in ("rationale", "install_call"):
        assert "VENDOR-CDN ASSET, NOT A RELEASE ASSET" in d[field], field
    assert "not stable across releases" in d["install_call"] or \
           "do not assume this URL is stable" in d["install_call"]
