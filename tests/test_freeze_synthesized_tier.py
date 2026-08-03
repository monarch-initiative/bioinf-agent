"""Freeze-path regression tests for the SYNTHESIZED tier (P2-C tier-breadth slice).

The synthesized tier is the UNIVERSAL RESIDUAL: for a tool with no per-tier generator,
the agent reads the tool's OWN build files and submits a command sequence, each tagged
'extracted' (verbatim from a named file) or 'agent_authored' (grounded in the repo's
prose). Unlike every other tier, the grid recipe is NOT a static install_method — the
generator DRIVES the real synth_fetch + validate_submission machinery so the shipped
provenance is the RUNTIME's own re-verification, never a hand-tag.

That distinction is the whole slice. A 2026-07-20 bake that hand-tagged provenance BUILT
the image but check_build correctly REFUSED it (PROVENANCE_CLEAN.untagged_command). The
recipe's `submission` is only the agent's CLAIM (what it would type after reading
synth_fetch); `_synth_install_method` re-fetches at the pinned commit and re-verifies every
command against those bytes — an 'extracted' command must occur verbatim in its origin_file
(sha256 stamped by the runtime, not the recipe), an 'agent_authored' command must ground.

Anchored to a REAL build: bwa 0.7.18 (lh3/bwa) — a ubiquitous C aligner, a small
self-contained `make` build (links only zlib). Two commands are EXTRACTED VERBATIM from
bwa's README "Getting started" block (the clone + `cd bwa; make`); two are grounded
AGENT_AUTHORED local verbs (a checkout PINNING the v0.7.18 commit for reproducibility + the
copy-to-PATH the README describes). FUNCTIONAL evidence RUNS `bwa index` on an inline ref
and asserts the FM-index (.bwt).

The tests split into: (A) the recipe shape (pure); (B) the honesty PROPERTY — the machinery
gates the submission, hermetic via a monkeypatched fetch, the strongest guarantee here.
"""
import hashlib
import importlib.util
from pathlib import Path

import pytest

from agent.skills.env_manager import EnvManager
from agent.skills import env_freeze
from agent.skills import env_honesty

ROOT = Path(__file__).resolve().parents[1]
COMMIT = "79b230de48c74156f9d3c26795a360fc5a2d5d3b"  # bwa v0.7.18


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ft():
    return _load("freeze_tiers")


def _probes():
    return _load("freeze_tier_probes")


def _synth_spec() -> dict:
    return _ft().tier("synthesized")["build"]["synth_spec"]


# A README that contains bwa's real "Getting started" block verbatim (the two extracted
# commands), so a monkeypatched fetch reproduces the runtime's ground truth WITHOUT network.
_REAL_README = (
    "## Getting started\n\n"
    "\tgit clone https://github.com/lh3/bwa.git\n"
    "\tcd bwa; make\n"
    "\t./bwa index ref.fa\n\n"
    "After you acquire the source code, simply use `make` to compile and copy the\n"
    "single executable `bwa` to the destination you want.\n"
)


def _fake_fetch(readme_text: str, commit: str) -> dict:
    """A synthetic synth_fetch result — one README.md with a runtime-computed sha256.
    Mirrors what EnvManager.fetch_build_source returns for a git repo, minus the clone."""
    return {"success": True, "commit": commit, "source_kind": "git",
            "files": [{"path": "README.md", "text": readme_text,
                       "sha256": hashlib.sha256(readme_text.encode()).hexdigest()}]}


def _patch_fetch(monkeypatch, readme_text: str, commit: str = COMMIT):
    monkeypatch.setattr(
        EnvManager, "fetch_build_source",
        staticmethod(lambda repo, ref, is_relevant=None: _fake_fetch(readme_text, commit)))


# ── #A the grid recipe shape (pure) ───────────────────────────────────────────

def test_grid_synthesized_is_a_wired_container_native_probe():
    """The synthesized row is a REAL probe (builder set + a synth_spec recipe), and the
    drift guard keeps the tier set == InstallMethod.type."""
    row = _ft().tier("synthesized")
    assert row["builder"] == "container_native"
    assert row["probe_tool"] == "bwa"
    assert row["build"]["primary_tools"] == ["bwa"]
    ss = row["build"]["synth_spec"]
    assert ss["tool"] == "bwa"
    assert set(ss) >= {"repo_url", "commit", "tool", "submission", "evidence"}


def test_grid_synthesized_pins_an_immutable_commit():
    """Reproducibility anchor: the recipe pins a full 40-hex commit. build_tier re-fetches
    at exactly this commit and refuses on mismatch (the source moved)."""
    ss = _synth_spec()
    assert len(ss["commit"]) == 40 and all(c in "0123456789abcdef" for c in ss["commit"])
    # the reproducibility PIN is threaded into the submission as an explicit checkout
    assert any(ss["commit"] in c["command"] and c["source"] == "agent_authored"
               for c in ss["submission"])


def test_grid_synthesized_prefers_verbatim_extraction_from_the_tools_own_files():
    """The tier's value: the agent lifts the tool's OWN documented build commands verbatim
    (source='extracted', naming the origin_file) rather than inventing them. The clone AND
    the build step are extractions from README.md; the authored ones are local verbs that
    make NO external reference (so they ground)."""
    from agent.skills import provenance as prov
    ss = _synth_spec()
    extracted = [c for c in ss["submission"] if c["source"] == "extracted"]
    authored = [c for c in ss["submission"] if c["source"] == "agent_authored"]
    assert len(extracted) >= 2 and all(c.get("origin_file") for c in extracted)
    assert any("git clone" in c["command"] for c in extracted)
    assert any("make" in c["command"] for c in extracted)
    # every authored command is a pure local verb — no URL/remote to ground against
    for c in authored:
        assert prov.external_refs(c["command"]) == [], \
            f"authored command makes an external ref that must be grounded: {c['command']}"


def test_grid_synthesized_evidence_is_functional_and_shape_clean():
    """FUNCTIONAL evidence (validated==ran): `bwa index` RUNS on inline data and the .bwt is
    asserted — NOT a presence default. It passes the anti-echo-cheat shape rule (leads with
    `cd`, references bwa as a real token) and classifies 'functional' (stronger than an
    import-only default, honestly disclosed as such)."""
    ss = _synth_spec()
    ev = ss["evidence"]
    assert env_honesty.evidence_shape_violation(ev, "bwa") is None
    assert env_honesty._references_tool(ev, "bwa") is True
    assert env_honesty.evidence_depth(ev, "bwa") == "functional"
    assert env_honesty.is_shallow_evidence(ev, "bwa") is False
    assert "bwa index" in ev and "test -s" in ev
    # a bare-echo of the tool name must still be rejected (the guard is live)
    assert env_honesty.evidence_shape_violation("echo bwa", "bwa") is not None


# ── #B the honesty PROPERTY: the machinery gates the submission, not the recipe ─

def test_synth_install_method_ships_runtime_verified_provenance(monkeypatch):
    """THE slice: `_synth_install_method` drives validate_submission against the runtime's
    OWN fetched bytes, so the shipped `commands` carry provenance the RUNTIME produced — an
    extracted command's origin_sha256 is the fetch's hash of README.md, not the recipe's
    word. The result is PROVENANCE_CLEAN (what the 2026-07-20 hand-tag failed)."""
    probes = _probes()
    _patch_fetch(monkeypatch, _REAL_README)
    im = probes._synth_install_method(_synth_spec())

    assert im["type"] == "synthesized" and im["tool"] == "bwa"
    assert im["commit_sha"] == COMMIT
    readme_sha = hashlib.sha256(_REAL_README.encode()).hexdigest()
    extracted = [c for c in im["commands"] if c["provenance"]["source"] == "extracted"]
    assert extracted, "no extracted command survived validation"
    for c in extracted:                      # sha256 stamped by the RUNTIME's fetch
        assert c["provenance"]["origin_sha256"] == readme_sha
        assert c["provenance"]["origin_file"] == "README.md"

    # Route it through the real freeze mapping and assert the Layer-1 firewall is clean.
    x = {"name": "bwa", "type": "synthesized", "install_method": im}
    spec = env_freeze._map_install(x)["spec"]
    assert env_honesty._clause_provenance({"longtail_steps": [spec]})[1] == []
    # the replayed RUN is the pinned, reproducible sequence
    assert f"git -C bwa checkout {COMMIT}" in spec["command"]
    assert spec["evidence"] == _synth_spec()["evidence"]


def test_synth_install_method_refuses_a_paraphrase(monkeypatch):
    """The anti-fake guard: an 'extracted' command that is NOT verbatim in the fetched file
    is a paraphrase, not an extraction. Here the fetched README omits the build line, so
    validate_submission rejects `cd bwa; make` and `_synth_install_method` RAISES — the
    recipe cannot smuggle an unverifiable command past the runtime's own bytes."""
    probes = _probes()
    readme_missing_build = "## Getting started\n\n\tgit clone https://github.com/lh3/bwa.git\n"
    _patch_fetch(monkeypatch, readme_missing_build)
    with pytest.raises(RuntimeError, match="provenance violation"):
        probes._synth_install_method(_synth_spec())


def test_synth_install_method_refuses_an_anchor_mismatch(monkeypatch):
    """The reproducibility anchor: if the re-fetch resolves a DIFFERENT commit than the
    recipe pinned (the source moved / the ref isn't immutable), the build is refused before
    any validation — the same guard synth_build makes."""
    probes = _probes()
    _patch_fetch(monkeypatch, _REAL_README, commit="deadbeef" * 5)  # != pinned COMMIT
    with pytest.raises(RuntimeError, match="anchor mismatch"):
        probes._synth_install_method(_synth_spec())


def test_build_tier_drives_the_synth_machinery_for_a_synth_spec(monkeypatch):
    """The generator dispatch: a synth_spec routes build_tier through _synth_install_method
    (real provenance) and hands build_env_image a draft whose install_method carries the
    runtime-verified commands — not the raw recipe submission. Hermetic: the actual docker
    build + honesty contract are faked; what's under test is the WIRING to the machinery."""
    probes = _probes()
    _patch_fetch(monkeypatch, _REAL_README)
    captured = {}

    def _fake_build(draft, *, name, primary_tools, platform, **kw):
        captured["draft"] = draft
        return {"success": True, "image": "img", "image_digest": "sha256:x",
                "content_digest": "sha256:c", "validation_locus": "emulated"}

    monkeypatch.setattr(env_freeze, "build_env_image", _fake_build)
    monkeypatch.setattr("agent.skills.env_honesty.check_build", lambda res: [])

    out = probes.build_tier(_ft().tier("synthesized"))
    assert out["ok"] is True and out["image_digest"] == "sha256:x"
    im = captured["draft"]["install_steps"][0]["installed_packages"][0]["install_method"]
    assert im["type"] == "synthesized"
    # every shipped command is provenance-tagged by the runtime (extracted|agent_authored)
    assert im["commands"] and all(
        c["provenance"]["source"] in ("extracted", "agent_authored") for c in im["commands"])
