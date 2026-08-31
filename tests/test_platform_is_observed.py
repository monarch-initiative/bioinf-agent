"""`platform` must be a fact about the artifact, not the request repeated back.

A freeze record states its architecture three times — `platform`, `validation_locus`,
and (for a content-addressed artifact) `image_digest`. Until BUILT.platform, not one of
those was an observation:

  * `platform` is the caller's `platform=` argument, echoed onto the record.
  * `validation_locus` is `detect_locus(platform)` — the REQUEST compared against the
    local daemon's arch. It answers "would a build for what you asked for be emulated
    here", which is a different question from "what is this image".
  * `image_digest` is whatever the producer got, and for a manifest index that is a
    digest naming a MENU of per-arch images rather than any image's bytes.

Measured on a real arm64 request (G3 phase 6). `resolve_biocontainer` takes no platform
argument at all, so an arm64 adopt binds bioconda's amd64-only image. The honesty
contract did refuse — it was never a false green — but it refused for the wrong reason:
"the tool is not provably present/runnable in what we ship", when samtools is present and
perfectly fine and the only thing wrong is that no arm64 build of that image exists. The
true cause sat truncated inside a verification's `out` field. A refusal that misnames its
cause sends the next agent to rebuild a tool that was never broken.

The fix is one comparison, and it is only meaningful because the two sides have separate
provenance: freeze READS `.Architecture` off the shipped image into `image_arch`, and the
contract holds that against `platform`. (Re-deriving the arch inside the contract would
have been the I5 laundering shape — a value compared against itself — as well as putting
a `docker image inspect` per env inside `list_installed_pipelines`.)
"""
from __future__ import annotations

import pytest

from agent.skills import env_honesty as eh
from agent.skills import freeze as fz
from agent.skills import locus


def _rec(**over):
    """A record that passes the rest of the Layer-1 contract, so any violation the
    tests see is the one under test."""
    r = {"image": "img:1", "image_digest": "sha256:" + "a" * 64,
         "platform": "linux-64", "image_arch": "amd64",
         "verifications": [{"label": "t", "tool": "t", "check": "t --version",
                            "rc": 0, "passed": True}]}
    r.update(over)
    return r


def _platform_violations(rec) -> list[str]:
    return [v["invariant"] for v in eh.evaluate_build(rec).violations
            if v["invariant"].startswith("BUILT.platform")]


def _platform_clause(rec):
    got = [c for c in eh.evaluate_build(rec).coverage if c.clause == "BUILT.platform"]
    assert got, "the BUILT.platform clause reported no coverage at all"
    return got[0]


# --- the arch token, read from every spelling the system writes --------------------

@pytest.mark.parametrize("spelling,arch", [
    ("linux-64", "amd64"), ("linux/amd64", "amd64"),   # conda form vs docker form:
    ("linux-aarch64", "arm64"), ("linux/arm64", "arm64"),  # the corpus contains BOTH
    ("linux/arm64/v8", "arm64"), ("osx-arm64", "arm64"), ("darwin/amd64", "amd64"),
    ("AMD64", "amd64"),
])
def test_every_platform_spelling_the_system_can_write_reads_to_an_arch(spelling, arch):
    """`target_arch` was `platform.split("/")[-1]`, which handled only the docker form.
    'linux-aarch64' has no slash, so it parsed to ITSELF — harmless while the sole
    consumer compared it to a daemon arch on amd64, and wrong the instant anything
    compared it to an arch read off an image."""
    assert locus.target_arch(spelling) == arch


@pytest.mark.parametrize("junk", ["banana", "", "linux", None])
def test_an_unrecognised_platform_reads_as_unknown_not_as_amd64(junk):
    """The old default was "amd64" — the commonest answer. A comparison that cannot
    distinguish "this is amd64" from "I cannot read this string" silently PASSES for
    every unparseable platform, which is the failure mode the whole clause exists to
    catch."""
    assert locus.target_arch(junk) == ""


# --- the clause --------------------------------------------------------------------

def test_an_image_of_the_architecture_it_claims_passes():
    assert _platform_violations(_rec(platform="linux-aarch64", image_arch="arm64")) == []
    assert _platform_clause(_rec()).status == eh.CHECKED


def test_the_two_spellings_of_one_architecture_are_not_a_mismatch():
    """Every BUILD record in the corpus carries the conda form and every ADOPT record
    the docker form. Reading them as different architectures would fail all of them."""
    assert _platform_violations(_rec(platform="linux-64", image_arch="amd64")) == []
    assert _platform_violations(_rec(platform="linux/amd64", image_arch="amd64")) == []


def test_an_arm64_request_that_bound_an_amd64_image_is_refused():
    """THE measured case. Asked for linux-aarch64, adopt bound bioconda's amd64-only
    biocontainer, and the record said arm64 over amd64 bytes."""
    rec = _rec(platform="linux-aarch64", image_arch="amd64")
    assert _platform_violations(rec) == ["BUILT.platform_matches"]


def test_the_refusal_names_the_cause_and_the_way_out():
    """The defect was never a missing refusal — it was a refusal that misnamed itself,
    so the reader went looking for a broken tool. THE GATE IS THE GUIDE: the message
    has to say what is actually wrong AND what to do instead."""
    msg = [v["message"] for v in eh.evaluate_build(
        _rec(platform="linux-aarch64", image_arch="amd64")).violations
        if v["invariant"] == "BUILT.platform_matches"][0]
    assert "arm64" in msg and "amd64" in msg, "the message must name BOTH architectures"
    assert "bioconda" in msg, "name why an arm64 adopt binds amd64 bytes"
    assert "container-native" in msg, "name the path that DOES produce a genuine arm64 image"


def test_a_manifest_index_digest_is_refused_because_it_names_no_bytes():
    """`image_arch: ""` from an image that inspects fine is not "we could not tell" —
    it means the digest resolves to a manifest list / OCI index. `multiqc` in the real
    corpus pins one: a single digest serving an amd64 child and an arm64 child, recorded
    as `platform: linux/amd64`. Two machines pulling it ship different binaries, which
    is the one thing a content-addressed artifact may not do."""
    rec = _rec(platform="linux/amd64", image_arch="")
    assert _platform_violations(rec) == ["BUILT.platform_pinned"]
    msg = [v["message"] for v in eh.evaluate_build(rec).violations][0]
    assert "child digest" in msg, "say what to record instead of the index digest"


def test_a_platform_string_the_system_cannot_read_is_refused_not_waved_through():
    """The other half of dropping the "amd64" default: an unreadable platform means the
    claim cannot be checked, and 'cannot be checked' must not resolve to 'fine'."""
    assert _platform_violations(_rec(platform="banana", image_arch="amd64")) \
        == ["BUILT.platform_matches"]


# --- three states, not two ---------------------------------------------------------

def test_a_record_written_before_the_observation_is_unobserved_not_failed():
    """All 16 records in the real corpus predate `image_arch`. Failing them would be a
    claim about their architecture that nothing supports — the same overreach in the
    other direction. They report UNOBSERVED, surface through `assurance_unproven[]` in
    the inventory, and are re-earned by a re-freeze, never a backfill."""
    rec = _rec()
    del rec["image_arch"]
    assert _platform_violations(rec) == []
    clause = _platform_clause(rec)
    assert clause.status == eh.UNOBSERVED
    assert clause.establishes == eh.ASSURANCE, (
        "this clause PROVES something about the artifact, so an unobserved one means we "
        "proved less — it must reach assurance_unproven[], not the disclosure bucket")
    assert "request repeated back" in clause.detail


def test_absent_and_empty_image_arch_are_different_facts():
    """The producer must be able to say "nothing looked" (absent) distinctly from "it
    looked, and the digest names no architecture" (empty). Collapsing them would turn
    the multiqc finding into silence."""
    absent = _rec()
    del absent["image_arch"]
    assert _platform_clause(absent).status == eh.UNOBSERVED
    assert _platform_clause(_rec(image_arch="")).status == eh.CHECKED


# --- the producer ------------------------------------------------------------------

def test_freeze_record_omits_the_key_entirely_when_nothing_was_observed():
    """`None` must not land as a VALUE. An `image_arch: null` on disk is a third
    spelling of the same state and the reader would have to know all three."""
    assert "image_arch" not in fz.freeze_record(
        request_key="k", content_digest="sha256:c", mode="build", image="i",
        image_digest="sha256:d", platform="linux-64", gated=False)


def test_freeze_record_keeps_an_observed_empty_string():
    """The manifest-index observation is an empty string and it is REAL — it must
    survive onto the record rather than being tidied away as falsy."""
    rec = fz.freeze_record(
        request_key="k", content_digest="sha256:c", mode="build", image="i",
        image_digest="sha256:d", platform="linux-64", gated=False, image_arch="")
    assert rec["image_arch"] == ""


def test_the_observation_reads_the_image_not_the_request():
    """locus.image_arch must distinguish its two empty answers, because the contract
    routes them to opposite outcomes (UNOBSERVED vs a violation)."""
    assert locus.image_arch("") == {"resolved": False, "arch": ""}


@pytest.mark.live
def test_the_observation_against_real_images():
    """The three shapes, on bytes rather than fixtures — this is where the amd64-only
    biocontainer and the multi-arch index were actually found."""
    assert locus.image_arch("no-such-image-here:nope")["resolved"] is False
    bc = locus.image_arch("quay.io/biocontainers/samtools:1.21--h50ea8bc_0")
    # Since F3 this pulls when the image is not local, so `resolved` no longer
    # depends on what this machine had cached — which is the whole point of the
    # change. The guard stays because a pull can still fail (offline, registry
    # down), and that is a real "we failed to look" rather than a cold cache.
    if bc["resolved"]:
        assert bc["arch"] == "amd64", "bioconda publishes linux-64 biocontainers only"


# ---------------------------------------------------------------------------
# F3 — the verdict that depended on the docker cache, not on the artifact.
#
# `image_arch` is `docker image inspect`, which reads only the LOCAL daemon. On
# the adopt path the call site sits ahead of the SBOM read that pulls, so a
# freshly-adopted biocontainer recorded `resolved: False` on a cold daemon and
# `checked` on a warm one — the same env, two answers, decided by what the laptop
# happened to have. Measured on the S1 fastp digest: `resolved: false` before the
# freeze, `{"resolved": true, "arch": "amd64"}` minutes later, image unchanged.
#
# It matters more than a flaky disclosure because the clause exists to stop
# `platform` being the caller's request echoed back, and the adopt path is
# precisely the one that can bind an architecture nobody asked for
# (`resolve_biocontainer` cannot express a platform). The observation was absent
# from the path that needed it, for a reason that was ordering, not impossibility.
# ---------------------------------------------------------------------------

def test_a_missing_image_is_pulled_before_the_arch_is_called_unobserved(monkeypatch):
    from agent.skills import locus

    calls = []

    def fake_sh(args, timeout=30):
        calls.append(args)
        if args[:3] == ["docker", "image", "inspect"]:
            # cold on the first look, warm after the pull
            pulled = any(a[:2] == ["docker", "pull"] for a in calls)
            return ({"rc": 0, "out": "amd64", "err": ""} if pulled
                    else {"rc": 1, "out": "", "err": "No such image"})
        return {"rc": 0, "out": "", "err": ""}

    monkeypatch.setattr(locus, "_sh", fake_sh)

    assert locus.image_arch("quay.io/biocontainers/fastp@sha256:abc") == {
        "resolved": True, "arch": "amd64"}
    assert any(a[:2] == ["docker", "pull"] for a in calls), calls


def test_a_warm_image_is_not_pulled_again(monkeypatch):
    from agent.skills import locus
    calls = []

    def fake_sh(args, timeout=30):
        calls.append(args)
        return {"rc": 0, "out": "arm64", "err": ""}

    monkeypatch.setattr(locus, "_sh", fake_sh)
    assert locus.image_arch("x:1")["arch"] == "arm64"
    assert not any(a[:2] == ["docker", "pull"] for a in calls), (
        "a locally-present image must not pay for a pull")


def test_an_image_that_cannot_be_fetched_is_still_unobserved(monkeypatch):
    """`resolved: False` keeps its meaning — we failed to look. It now means the
    image could neither be inspected nor fetched, which is a real absence rather
    than a cold cache."""
    from agent.skills import locus
    monkeypatch.setattr(locus, "_sh",
                        lambda args, timeout=30: {"rc": 1, "out": "", "err": "nope"})
    assert locus.image_arch("ghost:1") == {"resolved": False, "arch": ""}


def test_an_empty_ref_never_reaches_docker(monkeypatch):
    from agent.skills import locus
    called = []
    monkeypatch.setattr(locus, "_sh",
                        lambda args, timeout=30: (called.append(args), {"rc": 0, "out": "", "err": ""})[1])
    assert locus.image_arch("") == {"resolved": False, "arch": ""}
    assert called == []
