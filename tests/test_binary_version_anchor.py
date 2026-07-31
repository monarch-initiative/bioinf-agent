"""Binary-version anchor — a version-pinned binary must resolve to THAT version's
release asset, or refuse; it must NEVER ship the latest release's bytes under the
requested version's name.

WHY THIS FILE EXISTS. probe_github reads releases/LATEST, so a version pin is
invisible to the binary tier: `resolve("somalier","0.2.15",github_repo="brentp/
somalier",prefer="binary")` used to emit the v0.3.3 asset — fully green, sha256 +
SLSA + .sif, with nothing recording that the version was swapped. resolve() now
locates the PINNED release directly (`_release_for_version`) and drives both the
binary tier's availability and the emitted asset from it.

The design was adversarially red-teamed BEFORE implementation; the over-refusal
lens returned holds:false with four contract-level breaks (conda-miss pre-empts
binary; tag-format over-refusal; latest-only availability; pagination). Two lenses
(false-green, unchecked-scope) died on a usage limit, so their invariants are
encoded HERE as offline assertions instead:

  • FALSE-GREEN forbidden: a matcher over-match ('1'↔'1.2.3') or a wrong-release
    asset must never be emitted. (test_matcher_never_over_matches + the match tests
    assert the EXACT pinned asset.)
  • UNCHECKED never refuses, never reroutes, never substitutes: an error/incomplete
    release lookup keeps the pick and discloses — it does not manufacture a
    'version absent' refusal, does not fire the conda→binary reroute, and does not
    emit latest's bytes. (the UNCHECKED tests.)
  • NO SILENT TIER SWITCH: the reroute rests on a 200 + a real asset and is
    disclosed; a rate-limited lookup does not quietly change tier.

Registry/GitHub facts are stubbed (real shapes) so these run offline over the real
resolve() path.
"""
from __future__ import annotations

from agent.skills import resolver as R


def _boom(*a, **k):
    raise AssertionError("_release_for_version was called when it must be dormant")


def _wire(monkeypatch, *, conda=None, gh=None, relver=None, pip=None):
    """Stub registries + probe_github + _release_for_version; drive the REAL resolve().
    relver=None installs a raising stub so any UNEXPECTED version probe fails loudly."""
    monkeypatch.setattr(R, "probe_conda", lambda n, t=12: conda or {"available": False})
    monkeypatch.setattr(R, "probe_pypi", lambda n, t=12: pip or {"available": False})
    monkeypatch.setattr(R, "probe_cran", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_bioconductor", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_authors_sources", lambda *a, **k: {})
    monkeypatch.setattr(R, "probe_github", lambda repo, t=12: dict(gh or {"repo_exists": False}))
    # `_canon_repo` is a SECOND network call on the same page as probe_github, and
    # stubbing only the probe left it live: this file's docstring said "run offline"
    # while every test in it hit api.github.com. It stayed invisible because the tier
    # had nothing that refuses a socket (tests/conftest.py does now). The stub returns
    # what a repo that has NOT been renamed returns — identity + "ok" — which is the
    # case each test here is written about; a test that wants a 301 says so itself.
    monkeypatch.setattr(R, "_canon_repo",
                        lambda repo, t=12: ((repo or "").strip().strip("/").lower(), "ok"))
    monkeypatch.setattr(R, "_release_for_version",
                        (lambda *a, **k: relver) if relver is not None else _boom)


def _gh(*, has_assets=True, assets=None, repo_exists=True, probe_error=None):
    out = {"repo_exists": repo_exists, "has_release_assets": has_assets,
           "assets": assets if assets is not None else ["https://x/releases/download/v9/tool"],
           "is_fork": False, "parent": "", "upstream": "", "full_name": "", "default_branch": "main"}
    if probe_error:
        out["probe_error"] = probe_error
        out["repo_exists"] = False
    return out


def _conda(versions, *, latest=None, repo=""):
    d = {"available": True, "channel": "bioconda", "latest": latest or versions[-1],
         "summary": "packaged tool", "versions": list(versions)}
    if repo:
        d["repo"] = repo
        d["repo_source"] = "conda"
    return d


_ASSET_0215 = "https://github.com/brentp/somalier/releases/download/v0.2.15/somalier"


# ── 1. the headline: prefer=binary + version that ships a binary → pin THAT asset ──

def test_prefer_binary_version_match_emits_pinned_asset(monkeypatch):
    _wire(monkeypatch, gh=_gh(assets=["https://x/releases/download/v0.3.3/somalier"]),
          relver={"status": "match", "tag": "v0.2.15", "asset": _ASSET_0215,
                  "asset_name": "somalier"})
    d = R.resolve("somalier", version="0.2.15", github_repo="brentp/somalier", prefer="binary")
    assert d["chosen"] == "binary"
    assert d["install_call"] and _ASSET_0215 in d["install_call"]
    assert "v0.3.3" not in d["install_call"]        # NOT latest's bytes


# ── 2. Break 1: conda LACKS the pin but the repo ships it as a binary → reroute ──

def test_conda_version_miss_reroutes_to_binary(monkeypatch):
    _wire(monkeypatch, conda=_conda(["0.2.15", "0.2.17"], repo="brentp/somalier"),
          gh=_gh(),
          relver={"status": "match", "tag": "v0.2.16",
                  "asset": "https://x/releases/download/v0.2.16/somalier",
                  "asset_name": "somalier"})
    d = R.resolve("somalier", version="0.2.16", github_repo="brentp/somalier")   # no prefer
    assert d["chosen"] == "binary"
    assert d["binary_version_reroute"]["from"] == "conda"
    assert d["binary_version_reroute"]["resolved_tag"] == "v0.2.16"
    assert "v0.2.16/somalier" in d["install_call"]
    assert not d.get("version_absent")


# ── 3. conda lacks the pin AND the repo can't deliver it → refuse, disclose both ──

def test_conda_miss_and_binary_absent_refuses(monkeypatch):
    _wire(monkeypatch, conda=_conda(["1.0", "1.2"], repo="owner/tool"),
          gh=_gh(),
          relver={"status": "absent", "nearest": ["1.0", "1.2"], "tags": ["1.0", "1.2"]})
    d = R.resolve("tool", version="1.1", github_repo="owner/tool")
    assert d["chosen"] is None
    assert d["version_absent"]["requested"] == "1.1"
    assert d["version_absent"]["binary_checked"] is True
    assert "also checked" in d["rationale"]
    assert d["refusal_reason"] == "investigation_contradicted"


# ── 4. Break 3: latest has NO assets, but the PINNED older release ships a binary ──

def test_availability_comes_from_pinned_release_not_latest(monkeypatch):
    # has_release_assets=False (latest is source-only) — the OLD code marks binary
    # unavailable and prefer='binary' falls to synthesis. The anchor makes it available.
    _wire(monkeypatch, gh=_gh(has_assets=False, assets=[]),
          relver={"status": "match", "tag": "v1.0.0",
                  "asset": "https://x/releases/download/v1.0.0/tool-linux-amd64",
                  "asset_name": "tool-linux-amd64"})
    d = R.resolve("tool", version="1.0.0", github_repo="owner/tool", prefer="binary")
    assert d["chosen"] == "binary"
    assert d["probed"]["binary"]["available"] is True
    assert "v1.0.0/tool-linux-amd64" in d["install_call"]


# ── 5. version has a release but NO linux asset → binary unavailable, fall + disclose ──

def test_no_asset_pin_falls_to_synthesis_with_disclosure(monkeypatch):
    _wire(monkeypatch, gh=_gh(), relver={"status": "no_asset", "tag": "v2.0.0",
                                         "assets": ["tool-2.0.0.tar.gz"]})   # source tarball only
    d = R.resolve("tool", version="2.0.0", github_repo="owner/tool", prefer="binary")
    assert d["chosen"] == "synthesis"                 # binary unavailable → prefer ignored
    assert d["probed"]["binary"]["available"] is False
    assert "no linux/amd64 binary" in d["rationale"]


# ── 6. UNCHECKED + prefer=binary → keep binary, placeholder call, disclose (no substitute) ──

def test_unchecked_keeps_binary_and_emits_placeholder_not_latest(monkeypatch):
    _wire(monkeypatch, gh=_gh(assets=["https://x/releases/download/v9/tool"]),
          relver={"status": "error", "error": "rate_limited"})
    d = R.resolve("tool", version="3.3.3", github_repo="owner/tool", prefer="binary")
    assert d["chosen"] == "binary"                    # NOT switched away
    assert d["binary_version_unverified"]["status"] == "error"
    assert "UNVERIFIED" in d["install_call"]
    assert "/v9/tool" not in d["install_call"]        # latest's bytes NOT emitted as v3.3.3
    assert "UNCHECKED, not 'version absent'" in d["rationale"]


# ── 7. UNCHECKED must NOT fire the conda→binary reroute (the R2 seam) ──

def test_unchecked_does_not_reroute_off_a_conda_miss(monkeypatch):
    _wire(monkeypatch, conda=_conda(["1.0", "1.2"], repo="owner/tool"),
          gh=_gh(),
          relver={"status": "error", "error": "rate_limited"})
    d = R.resolve("tool", version="1.1", github_repo="owner/tool")   # conda lacks 1.1
    assert d.get("chosen") != "binary"                # an unchecked probe never reroutes
    assert not d.get("binary_version_reroute")
    # conda-miss still refuses (the version genuinely isn't on conda), never false-green to binary
    assert d["chosen"] is None
    assert d["version_absent"]["requested"] == "1.1"
    # HONESTY: the binary tier was UNCHECKED (error) — the refusal record must NOT render that
    # as a checked fact, and the refusal is investigation_INCOMPLETE (a pin-honoring tier went
    # unreached), never a settled contradiction. (red-team: binary_checked=True-when-unchecked.)
    assert d["version_absent"]["binary_checked"] is False
    assert d["refusal_reason"] == "investigation_incomplete"
    assert d.get("binary_version_unchecked")
    assert "UNCHECKED" in d["rationale"]


# ── 7b. UNCHECKED that falls out to synthesis (latest source-only) is DISCLOSED, not silent ──

def test_unchecked_fallthrough_to_synthesis_is_disclosed(monkeypatch):
    # latest ships no linux asset (binary unavailable-from-latest) AND the pinned-release lookup
    # is UNCHECKED → we fall to synthesis. The CHECKED-absent fallthrough is disclosed, so the
    # strictly-less-known UNCHECKED one must be too (the backwards-asymmetry red-team finding).
    _wire(monkeypatch, gh=_gh(has_assets=False, assets=[]),
          relver={"status": "error", "error": "rate_limited"})
    d = R.resolve("tool", version="1.0.0", github_repo="owner/tool", prefer="binary")
    assert d["chosen"] == "synthesis"                 # unchecked can only PROMOTE binary, not demote
    assert d.get("binary_version_unverified")         # surfaced machine-readably
    assert "UNCHECKED for" in d["rationale"]           # and disclosed, not silent


# ── 8. dormancy: no version → the anchor never runs; latest-derived pick, block inert ──

def test_no_version_is_dormant(monkeypatch):
    # relver=None installs the raising stub — proves _release_for_version is NOT called.
    _wire(monkeypatch, gh=_gh(assets=["https://x/releases/download/v9/tool"]))
    d = R.resolve("tool", github_repo="owner/tool", prefer="binary")
    assert d["chosen"] == "binary"
    assert "resolved_asset" not in d["probed"]["binary"]


# ── 9. _install_call never emits a sidecar as the binary URL (the assets[0] fix) ──

def test_install_call_skips_sidecar_asset(monkeypatch):
    _wire(monkeypatch, gh=_gh(assets=[
        "https://github.com/o/t/releases/download/v1/t.sha256",   # sidecar FIRST
        "https://github.com/o/t/releases/download/v1/t"]))
    d = R.resolve("t", github_repo="o/t", prefer="binary")        # no version → dormant anchor
    assert d["chosen"] == "binary"
    assert d["install_call"].count(".sha256") == 0
    assert "download/v1/t\"" in d["install_call"]


# ── 10. the matcher's over-match guards, in the suite (false-green lens, offline) ──

def test_matcher_never_over_matches():
    m = R._tag_matches
    # MUST match (real releases)
    for req, tag, tool in [
        ("0.2.15", "v0.2.15", "somalier"), ("1.10", "v1.10.0", "salmon"),
        ("2.15.1", "Trinity-v2.15.1", "trinity"), ("2.28", "v2.28", "minimap2"),
        ("15", "15-6f452a0", "mmseqs2"), ("15.6f452", "15-6f452", "mmseqs2"),
        ("1.2.3", "release-1.2.3", "toolx")]:
        assert m(req, tag, tool), (req, tag)
    # MUST NOT match (over-match = ships the wrong bytes green)
    for req, tag, tool in [
        ("1", "1.2.3", "toolx"), ("2", "v2.28", "minimap2"), ("0.2", "v0.2.15", "somalier"),
        ("0.3.3", "v0.2.15", "somalier"), ("15", "15.6", "mmseqs2"), ("1.2", "1.2.30", "toolx"),
        # Rule-5 separator over-match: 'X.Y-Z'/'X.Y_Z' is PEP440 X.Y.postZ, a DIFFERENT release
        # than 'X.Y.Z' — normalizing '-'/'_'→'.' must NOT forge a match rule 3 already refused
        # (the red-team's headline false-green; the offline suite missed the post-release form).
        ("1.2.3", "1.2-3", "tool"), ("1.2.3", "1.2_3", "tool"), ("2.28.1", "2.28-1", "t"),
        ("1.0", "1-0", "tool"),
        # Rule-4 build-hash over-match: a DOTTED all-decimal suffix is a minor version, not a
        # git short hash — '1.234567' is version 1.234567, not "version 1 + hash 234567".
        ("1", "1.234567", "toolx"), ("2", "2.100000", "toolx"), ("1", "1.202401", "toolx")]:
        assert not m(req, tag, tool), ("FALSE-GREEN", req, tag)
