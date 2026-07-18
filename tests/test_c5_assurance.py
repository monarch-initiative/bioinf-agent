"""C5 — per-tool SHIP assurance across ALL tiers (not just binary).

The honesty hole C5 closes: before this, only the `binary` tier attached a
`provenance={verified, assurance}`, so the ENV report rendered nothing for every
other tier → it IMPLIED UNIFORM TRUST. A `source` tool cloned from a floating
branch (which DRIFTS) looked identical to a digest-pinned one. C5 makes each tier
disclose HOW its shipped tool is anchored, with `verified=True` reserved for an
IMMUTABLE anchor a rebuild reproduces.

These tests make the DISCLOSURE itself machine-verified:
  1. `_replay_assurance` — the honest per-tier (assurance, verified) matrix.
  2. Every assurance the system can emit renders a badge (no generic fallback),
     and verified↔badge-class stay consistent (verified ⇒ 'ok', else 'na').
  3. `_map_install` attaches provenance for EVERY tier (the wiring), with
     verified True only for genuinely immutable anchors.
  4. `_shipped_binary_entry` copies assurance into the freeze record for any tier.
"""
from __future__ import annotations

import pytest

from agent.skills.env_freeze import _replay_assurance, _is_commit_sha, _map_install
from agent.skills.env_report_html import _ASSURANCE_BADGE, _assurance_badge
from agent.mcp_tools.freeze_tools import _shipped_binary_entry

_C = "a" * 40   # a full 40-hex commit


# ── 1. the honest per-tier matrix ──────────────────────────────────────────
@pytest.mark.parametrize("tier,im,expected", [
    # source: immutable commit → verified; movable ref → tofu; nothing → unpinned
    ("source",      {"commit_sha": _C},                 ("commit_pinned", True)),
    ("source",      {"ref": "v1.2.3"},                  ("ref_pinned_tofu", False)),
    ("source",      {"ref": "main"},                    ("ref_pinned_tofu", False)),
    ("source",      {},                                 ("unpinned", False)),
    # synthesized: gated + replayed @ commit
    ("synthesized", {"commit_sha": _C},                 ("commit_pinned", True)),
    ("synthesized", {},                                 ("unpinned", False)),
    # cargo/go: immutable registry version compiled in-image → verified; latest drifts
    ("cargo",       {"version": "1.0.0"},               ("built_pinned", True)),
    ("cargo",       {"version": "latest"},              ("built_unpinned", False)),
    ("cargo",       {},                                 ("built_unpinned", False)),
    ("go",          {"version": "v1.2.3"},              ("built_pinned", True)),
    ("go",          {"version": "latest"},              ("built_unpinned", False)),
    # perl / r: TOFU (no immutable content anchor we reproduce)
    ("perl",        {},                                 ("cpan_tofu", False)),
    ("r_install",   {"source": "github:o/r", "commit_sha": _C}, ("commit_pinned", True)),
    ("r_install",   {"source": "cran"},                 ("repo_tofu", False)),
    ("r_install",   {"source": "github:o/r", "ref": "main"},    ("repo_tofu", False)),
    # flag-bearing pip: literal command, not lock-representable
    ("pip",         {},                                 ("command_pinned", False)),
])
def test_replay_assurance_matrix(tier, im, expected):
    assert _replay_assurance(tier, im) == expected


def test_verified_only_for_immutable_anchors():
    """The load-bearing honesty rule: verified=True ONLY where a rebuild reproduces
    the bytes from an immutable anchor. A floating branch must NEVER be verified."""
    assert _replay_assurance("source", {"ref": "main"})[1] is False
    assert _replay_assurance("source", {})[1] is False
    assert _replay_assurance("cargo", {"version": "latest"})[1] is False
    assert _replay_assurance("perl", {})[1] is False
    assert _replay_assurance("source", {"commit_sha": _C})[1] is True


def test_is_commit_sha():
    assert _is_commit_sha(_C) is True
    assert _is_commit_sha(_C.upper()) is True
    assert _is_commit_sha("main") is False
    assert _is_commit_sha("v1.2.3") is False
    assert _is_commit_sha("a" * 39) is False      # too short
    assert _is_commit_sha("g" * 40) is False      # non-hex
    assert _is_commit_sha("") is False


# ── 2. every emitted assurance renders honestly ────────────────────────────
# The full set the system can emit: binary + jar inline (env_freeze) + helper.
_EMITTED = {
    "authenticated", "pinned_tofu", "unanchored_cross_platform", "unanchored",  # binary/jar
    "commit_pinned", "ref_pinned_tofu", "unpinned",                             # source/synth/r
    "built_pinned", "built_unpinned",                                           # cargo/go
    "cpan_tofu", "repo_tofu", "command_pinned",                                 # perl/r/pip
}
_VERIFIED_ASSURANCES = {"authenticated", "commit_pinned", "built_pinned", "lock_pinned"}


@pytest.mark.parametrize("a", sorted(_EMITTED))
def test_every_emitted_assurance_has_a_badge(a):
    """No emitted assurance may fall through to the generic '⚠ {a}' fallback — the
    report must name each level explicitly."""
    assert a in _ASSURANCE_BADGE
    cls, txt = _ASSURANCE_BADGE[a]
    # class must match the verified/unverified split: verified→ok, else→na.
    assert cls == ("ok" if a in _VERIFIED_ASSURANCES else "na"), (a, cls)
    assert txt.strip()


def test_assurance_badge_pill_and_empty():
    assert "checksum-verified" in _assurance_badge({"assurance": "authenticated"})
    assert "drifts" in _assurance_badge({"assurance": "unpinned"})
    assert _assurance_badge({}) == ""            # no assurance → no pill (unchanged)
    assert _assurance_badge({"assurance": ""}) == ""


# ── 3. _map_install attaches provenance for every tier (the wiring) ─────────
def _source_install(ref_field: dict) -> dict:
    im = {"source": "https://github.com/o/r", "build_command": "make", "bin_path": "t"}
    im.update(ref_field)
    return {"name": "t", "type": "source", "install_method": im}


def test_map_install_source_commit_is_verified():
    m = _map_install(_source_install({"commit_sha": _C}))
    prov = m["spec"]["provenance"]
    assert prov["tier"] == "source"
    assert prov["assurance"] == "commit_pinned"
    assert prov["verified"] is True


def test_map_install_source_floating_branch_is_disclosed_unverified():
    """The exact hole C5 closes: a floating-branch source tool must ship DISCLOSED
    as unverified, not silently trusted."""
    m = _map_install(_source_install({}))               # no commit, no ref
    prov = m["spec"]["provenance"]
    assert prov["assurance"] == "unpinned"
    assert prov["verified"] is False


def test_map_install_perl_discloses_cpan_tofu():
    m = _map_install({"name": "M::N", "type": "perl",
                      "install_method": {"module": "M::N", "distribution": "M-N"}})
    assert m["spec"]["provenance"] == {"tier": "perl", "verified": False,
                                       "assurance": "cpan_tofu"}


# ── 4. the freeze record copies assurance for any tier ─────────────────────
def test_shipped_binary_entry_carries_assurance_for_non_binary_tier():
    step = {"tool": "mytool", "purpose": "install mytool", "command": "git clone ... && make",
            "provenance": {"tier": "source", "verified": True, "assurance": "commit_pinned"}}
    e = _shipped_binary_entry(step)
    assert e["tier"] == "source"
    assert e["verified"] is True
    assert e["assurance"] == "commit_pinned"
    # the tool is NAMED, and separately from both the prose and the shell line — the
    # three used to share two keys with drifting meanings
    assert e["tool"] == "mytool"
    assert e["install_command"] == "git clone ... && make"


def test_shipped_binary_entry_without_provenance_discloses_absence():
    """A step with nothing to disclose still produces the FULL declared shape, with
    each absent fact stated as None rather than omitted. Omission is what let four
    readers each invent a different default for a missing key."""
    e = _shipped_binary_entry({"tool": "seqtk", "purpose": "p", "command": "c"})
    assert e == {"tool": "seqtk", "version": None, "provenance": "p",
                 "install_command": "c", "tier": None, "verified": None,
                 "assurance": None}


def test_shipped_binary_entry_refuses_a_step_with_no_tool_name():
    """REINTRODUCE-THE-BUG. `tool` is what every reader keys on. A step that lost it
    used to yield a record labelled with prose (or the literal string "tool"), and the
    ENV report rendered `<b>tool</b>` four times for the authors'-image env. An
    unnameable binary must fail at the producer, not render as a blank cell."""
    import pytest as _pytest
    from pydantic import ValidationError
    with _pytest.raises(ValidationError, match="tool"):
        _shipped_binary_entry({"purpose": "p", "command": "c"})
