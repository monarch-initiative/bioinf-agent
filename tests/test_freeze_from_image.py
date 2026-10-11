"""
Tests for freeze_from_image — the authors-image / authors-Dockerfile freeze executor.

The docker calls are monkeypatched so the HONESTY flow is testable without a daemon:
the point under test is that the same contract (BUILT / VALIDATED_IN_IMAGE / POLICY_CLEAN)
gates registration, that a tool whose evidence doesn't RUN (fails, or is an echo/print
cheat) is REFUSED, and that all four deliverables render from the verified record.
"""

from __future__ import annotations

import json

from agent.skills import freeze_from_image as F


class _Cache:
    def __init__(self):
        self.registered = {}

    def register(self, key, record):
        self.registered[key] = record


def _mock_docker(monkeypatch, *, evidence_rc=0, digest="sha256:" + "ab" * 32):
    monkeypatch.setattr(F, "_image_present", lambda image: True)
    monkeypatch.setattr(F, "_image_digest", lambda image: digest)
    def _run(image, platform, command, timeout=300, maxlen=400):
        # THE CONTROL IMAGE MUST NOT CARRY THE TOOL. `_evidence_discriminates` re-runs
        # each passing evidence in a stock Debian and refuses if it passes there too. A
        # stub that returns rc=0 for EVERY image models a world where the evidence proves
        # nothing — which is exactly the state the check exists to catch, so it fired.
        if image == F._CONTROL_IMAGE:
            return {"rc": 127, "out": "command not found"}
        # importlib SBOM probe returns a JSON list; everything else is the evidence
        if "importlib.metadata" in command:
            return {"rc": 0, "out": json.dumps(["talos==11.0.0"])}
        return {"rc": evidence_rc, "out": "ran"}
    monkeypatch.setattr(F, "_run_in_image", _run)
    # The pulled image is a real single-arch image of the platform requested. Without
    # this the `docker image inspect` behind locus.image_arch finds nothing, freeze
    # records no `image_arch`, and BUILT.platform correctly reads UNOBSERVED — turning
    # every happy-path assertion here from `proven` into `degraded`. Patched on the
    # locus module rather than on F because freeze_from_image imports it inside the
    # function, so there is no F._locus attribute to replace.
    import agent.skills.locus as LOC
    monkeypatch.setattr(LOC, "image_arch",
                        lambda ref: {"resolved": True, "arch": "amd64"})
    # SBOM-from-image best-effort → force the importlib fallback path
    import agent.skills.container_build as CB
    monkeypatch.setattr(CB.ContainerBuild, "conda_sbom_from_image", staticmethod(lambda *a, **k: []))
    monkeypatch.setattr(CB.ContainerBuild, "apt_sbom_from_image", staticmethod(lambda *a, **k: []))
    # The image carries no source label unless a test says so, and the identity probes
    # reach no registry: the freeze under test must not depend on the network or on which
    # images this machine's daemon happens to hold.
    monkeypatch.setattr(F, "_image_source_label", lambda image: "")
    import agent.skills.resolver as R
    monkeypatch.setattr(R, "probe_pypi", lambda name, timeout=12: {"available": False})
    monkeypatch.setattr(R, "probe_conda", lambda name, timeout=12: {"available": False})


def test_adopt_image_happy_path_registers_and_renders(tmp_path, monkeypatch):
    _mock_docker(monkeypatch)
    cache = _Cache()
    out = F.freeze_from_image(
        image="ghcr.io/org/talos@sha256:abc", name="talos_authors", version="11.0.0",
        tools=[{"name": "talos", "evidence": "python -m talos --help"}],
        build_method="adopt-image", env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] == "proven", out
    assert out["build_method"] == "adopt-image"
    assert cache.registered, "env should be registered in the cache"
    # all four deliverables written
    for f in ("talos_authors.ENV.html", "talos_authors.attestation.json",
              "talos_authors.recipe.yaml", "talos_authors.recipe.md"):
        assert (tmp_path / f).is_file(), f"missing deliverable {f}"
    # the human recipe records the adopt path
    md = (tmp_path / "talos_authors.recipe.md").read_text()
    assert "adopt" in md.lower()
    # evidence DEPTH is disclosed per verification + a soft advisory (not a refusal):
    # `--help` proves presence, not a functional run → flagged shallow.
    assert out["verifications"][0]["depth"] == "help"
    assert "talos" in out["shallow_evidence"]
    assert "shallow evidence" in out["evidence_advisory"]


def test_failing_evidence_is_refused_by_honesty_contract(tmp_path, monkeypatch):
    _mock_docker(monkeypatch, evidence_rc=1)   # the tool does NOT run in-image
    cache = _Cache()
    out = F.freeze_from_image(
        image="img@sha256:abc", name="x", tools=[{"name": "talos", "evidence": "talos --run"}],
        env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] == "refused"
    assert out["code"] == "freeze_from_image.honesty_violation"
    assert not cache.registered, "a failing-evidence image must NOT be registered"


def test_echo_cheat_evidence_is_refused(tmp_path, monkeypatch):
    _mock_docker(monkeypatch, evidence_rc=0)   # exits 0 but doesn't reference the tool
    cache = _Cache()
    out = F.freeze_from_image(
        image="img@sha256:abc", name="x", tools=[{"name": "talos", "evidence": "echo hello"}],
        env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] == "refused", out
    assert out["code"] == "freeze_from_image.honesty_violation"
    kinds = {v["invariant"] for v in out["honesty_violations"]}
    assert any("evidence_shape" in k for k in kinds), kinds


def test_no_tools_refused(tmp_path):
    out = F.freeze_from_image(image="img", name="x", tools=[],
                             env_cache=_Cache(), env_dir=tmp_path)
    assert out["outcome"] == "refused" and out["code"] == "freeze_from_image.no_tools"


def test_sbom_capture_failure_is_recorded_not_silently_empty(tmp_path, monkeypatch):
    """A failed SBOM probe must not render as "this image has no dependencies".

    The capture used to sit under a bare `except Exception: pass`, so a probe failure
    left resolved_packages == [] — which the ENV report renders as "0 along for the ride"
    and the attestation carries as an empty package list. Absence of data presented as
    data, which is the failure this project exists to prevent.
    """
    _mock_docker(monkeypatch)
    import agent.skills.container_build as CB
    def _boom(*a, **k):
        raise RuntimeError("docker exec failed")
    monkeypatch.setattr(CB.ContainerBuild, "conda_sbom_from_image", staticmethod(_boom))
    monkeypatch.setattr(CB.ContainerBuild, "apt_sbom_from_image", staticmethod(_boom))
    # and make the importlib fallback fail too, so the SBOM is genuinely empty
    monkeypatch.setattr(
        F, "_run_in_image",
        lambda image, platform, command, timeout=300, maxlen=400:
            {"rc": 127, "out": "not found"} if image == F._CONTROL_IMAGE
            else {"rc": 0, "out": "ran"})
    cache = _Cache()
    out = F.freeze_from_image(
        image="img@sha256:abc", name="t", version="1",
        tools=[{"name": "talos", "evidence": "talos --help"}],
        build_method="adopt-image", env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] == "proven", out          # not a refusal — disclosure, not a gate
    rec = cache.registered[out["request_key"]]
    assert rec["resolved_packages"] == []
    assert "sbom_error" in rec, "an empty SBOM caused by a probe failure must say so"
    assert "docker exec failed" in rec["sbom_error"]


def test_gated_image_without_licenses_is_refused_by_i13(tmp_path, monkeypatch):
    """I13 must fire on THIS path too — it is the one path that hands the EnvCache record
    straight to check_build.

    Regression for audit 2026-07-16: freeze_record emitted `gated` while
    env_honesty._check_license reads `license_gated`, so a gated artifact declaring NO
    licenses registered clean here — and then rendered a POLICY_CLEAN badge, wrote an
    attestation, and became shippable. Every other freeze path built a separate
    contract-input dict and so translated the name by hand; this one didn't.
    """
    _mock_docker(monkeypatch)
    cache = _Cache()
    out = F.freeze_from_image(
        image="cellranger:8.0.0", name="cellranger", version="8.0.0",
        tools=[{"name": "cellranger", "evidence": "cellranger --version"}],
        build_method="adopt-image", gated=True, licenses=[],
        env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] == "refused", out
    assert any(v["invariant"] == "I13.gated_license_recorded"
               for v in out["honesty_violations"]), out["honesty_violations"]
    assert not cache.registered, "a gated artifact with no licenses[] must NOT register"


def test_gated_image_with_licenses_registers_and_is_not_redistributable(tmp_path, monkeypatch):
    """The other half of I13: declaring the terms is what makes a gated artifact shippable
    (as a non-redistributable one). Guards against 'fixed' meaning 'now refuses everything'."""
    _mock_docker(monkeypatch)
    cache = _Cache()
    out = F.freeze_from_image(
        image="cellranger:8.0.0", name="cellranger", version="8.0.0",
        tools=[{"name": "cellranger", "evidence": "cellranger --version"}],
        build_method="adopt-image", gated=True, licenses=["10x Genomics EULA"],
        env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] == "proven", out
    rec = cache.registered[out["request_key"]]
    from agent.models.core_data import record_is_gated
    assert record_is_gated(rec) is True
    assert rec["redistributable"] is False
    assert rec["licenses"] == ["10x Genomics EULA"]


def test_authors_dockerfile_records_pinned_source(tmp_path, monkeypatch):
    _mock_docker(monkeypatch)
    cache = _Cache()
    out = F.freeze_from_image(
        image="talos:11.0.0", name="talos_authors", version="11.0.0",
        tools=[{"name": "talos", "evidence": "python -m talos --help"}],
        build_method="authors-dockerfile",
        dockerfile_source={"repo": "https://github.com/populationgenomics/talos",
                           "commit": "c5a8f07", "tag": "v11.0.1"},
        env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] == "proven"
    rec = cache.registered[out["request_key"]]
    assert rec["build_method"] == "authors-dockerfile"
    assert rec["dockerfile_source"]["commit"] == "c5a8f07"
    md = (tmp_path / "talos_authors.recipe.md").read_text()
    # Pin to the COMMIT and name the tag alongside. `git checkout v11.0.1` is not a pin —
    # the repo owner can move that tag tomorrow — it only reads like one.
    assert "git checkout c5a8f07" in md
    assert "v11.0.1" in md


# --- shipped-binary self-reported version capture (adopt path) --------------------
# The SBOM cannot see a source-built / release binary the authors baked in, so its only
# observable version is what the binary prints about ITSELF. `_parse_self_report` extracts
# that, and its two guards make the htslib-under-bcftools lie structurally impossible.

import pytest


@pytest.mark.parametrize("tool,out,expected", [
    # THE war story: the fork prints its own commit on line 1, htslib's version on line 2.
    # First-line-only means line 2 is never read, so 1.23.1 can NEVER be returned here.
    ("bcftools", "bcftools 9cef4057\nUsing htslib 1.23.1\nCopyright (C) 2025", "9cef4057"),
    ("samtools", "samtools 1.21\nUsing htslib 1.21", "1.21"),
    ("echtvar", "echtvar 0.2.2", "0.2.2"),
    ("tabix", "tabix (htslib) 1.23.1", "1.23.1"),           # skips the '(htslib)' token
    ("mytool", "mytool version 1.2.3", "1.2.3"),            # digit-requirement skips 'version'
    ("foo", "foo v2.0.1", "2.0.1"),                          # strips a leading v
    ("bcftools", "/usr/local/bin/bcftools 9cef4057", "9cef4057"),  # basename match
    ("bcftools", "Usage: bcftools [options]", None),        # not the tool's own line
    ("bcftools", "Using htslib 1.23.1", None),              # a dependency's line — rejected
    ("samtools", "samtools\nUsing htslib 1.21", None),      # no version token on line 1
    ("bwa", "", None),                                       # empty → unrecorded, not a guess
])
def test_parse_self_report(tool, out, expected):
    assert F._parse_self_report(tool, out) == expected


def test_parse_self_report_never_returns_a_dependency_version():
    # The load-bearing invariant, stated as its own test: for the real Talos bcftools fork
    # banner, the returned token is the fork's commit and is NEVER htslib's 1.23.1.
    got = F._parse_self_report("bcftools", "bcftools 9cef4057\nUsing htslib 1.23.1")
    assert got == "9cef4057"
    assert got != "1.23.1"


def test_self_reported_version_none_on_nonzero_exit(monkeypatch):
    monkeypatch.setattr(F, "_run_in_image",
                        lambda *a, **k: {"rc": 1, "out": "command not found"})
    assert F._self_reported_version("img", "linux/amd64", "bcftools") is None


def test_freeze_from_image_captures_fork_self_report(tmp_path, monkeypatch):
    # End-to-end: freeze_from_image populates shipped_binaries[].version from the binary's
    # own `--version`, so the adopted fork ships with `9cef4057`, not None and not htslib.
    monkeypatch.setattr(F, "_image_present", lambda image: True)
    monkeypatch.setattr(F, "_image_digest", lambda image: "sha256:" + "cd" * 32)
    # Same locus stub _mock_docker installs, and for the same reason — but this test
    # builds its own stubs rather than calling it, so it did not get one. That made the
    # `proven` assertion depend on AMBIENT MACHINE STATE: `talos-authors:11.0.0` happens
    # to exist on the development laptop, so `docker image inspect` behind
    # locus.image_arch resolved, BUILT.platform read CHECKED, and the test passed. On a
    # runner with no Docker and no such image it reads UNOBSERVED — which is correct,
    # and correctly downgrades the outcome to `degraded`. Green here, red in CI, and the
    # difference was a leftover image nobody declared.
    import agent.skills.locus as LOC
    monkeypatch.setattr(LOC, "image_arch",
                        lambda ref: {"resolved": True, "arch": "amd64"})
    import agent.skills.container_build as CB
    monkeypatch.setattr(CB.ContainerBuild, "conda_sbom_from_image", staticmethod(lambda *a, **k: []))
    monkeypatch.setattr(CB.ContainerBuild, "apt_sbom_from_image", staticmethod(lambda *a, **k: []))

    def _run(image, platform, command, timeout=300, maxlen=400):
        # THE CONTROL IMAGE MUST NOT CARRY THE TOOL. `_evidence_discriminates` re-runs
        # each passing evidence in a stock Debian and refuses if it passes there too. A
        # stub that returns rc=0 for EVERY image models a world where the evidence proves
        # nothing — which is exactly the state the check exists to catch, so it fired.
        if image == F._CONTROL_IMAGE:
            return {"rc": 127, "out": "command not found"}
        if "importlib.metadata" in command:
            return {"rc": 0, "out": json.dumps([])}
        if command.startswith("bcftools --version"):
            return {"rc": 0, "out": "bcftools 9cef4057\nUsing htslib 1.23.1"}
        if command.startswith("echtvar --version"):
            return {"rc": 0, "out": "echtvar 0.2.2"}
        return {"rc": 0, "out": "ran"}   # the evidence commands
    monkeypatch.setattr(F, "_run_in_image", _run)

    cache = _Cache()
    out = F.freeze_from_image(
        image="talos-authors:11.0.0", name="talos_selfrep", version="11.0.0",
        tools=[{"name": "bcftools", "evidence": "bcftools --version"},
               {"name": "echtvar", "evidence": "echtvar --version"}],
        build_method="authors-dockerfile",
        dockerfile_source={"repo": "https://github.com/populationgenomics/talos",
                           "commit": "c5a8f07", "tag": "v11.0.1"},
        env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] == "proven"
    rec = cache.registered[out["request_key"]]
    sb = {s["tool"]: s["version"] for s in rec["shipped_binaries"]}
    assert sb["bcftools"] == "9cef4057"      # the fork's own identity, captured
    assert sb["bcftools"] != "1.23.1"        # NOT the dependency htslib scraped from line 2
    assert sb["echtvar"] == "0.2.2"


# ── _run_in_image: entrypoint-robust in-image execution (real bug: diamond) ────

def test_run_in_image_falls_back_to_entrypoint_override(monkeypatch):
    """A tool image with ENTRYPOINT ["tool"] (diamond, many biocontainers) eats `bash -c cmd`
    as the tool's own args, so the natural invocation fails; _run_in_image retries with
    --entrypoint bash — which runs the command directly AND mirrors how `apptainer exec`
    invokes it on HPC — and takes that success."""
    calls = []

    def fake_sh(argv, timeout=300):
        calls.append(argv)
        if "--entrypoint" in argv:                       # override runs the command
            return {"rc": 0, "out": "diamond version 2.2.4", "err": ""}
        return {"rc": 1, "out": "", "err": "Invalid command: bash"}   # entrypoint ate it

    monkeypatch.setattr(F, "_sh", fake_sh)
    r = F._run_in_image("img", "linux/amd64", "diamond version")
    assert r["rc"] == 0 and "diamond version 2.2.4" in r["out"]
    assert len(calls) == 2 and "--entrypoint" in calls[1]   # natural FIRST, override SECOND


def test_run_in_image_natural_success_never_triggers_override(monkeypatch):
    """Zero regression: when the natural `bash -c` already works (no entrypoint, or an
    env-ACTIVATING entrypoint that runs our shell), its success is kept verbatim and the
    override is never attempted — so an activation entrypoint is preserved."""
    calls = []

    def fake_sh(argv, timeout=300):
        calls.append(argv)
        return {"rc": 0, "out": "ok", "err": ""}

    monkeypatch.setattr(F, "_sh", fake_sh)
    r = F._run_in_image("img", "linux/amd64", "tool --version")
    assert r["rc"] == 0 and len(calls) == 1 and "--entrypoint" not in calls[0]


def test_run_in_image_double_failure_returns_the_natural_error(monkeypatch):
    """When BOTH attempts fail the command genuinely does not run — rc stays non-zero and the
    natural (canonical) attempt's output is returned, not the override's."""
    def fake_sh(argv, timeout=300):
        if "--entrypoint" in argv:
            return {"rc": 3, "out": "", "err": "override-err"}
        return {"rc": 2, "out": "", "err": "natural-err"}

    monkeypatch.setattr(F, "_sh", fake_sh)
    r = F._run_in_image("img", "linux/amd64", "missing --version")
    assert r["rc"] == 2 and r["out"] == "natural-err"


# ---------------------------------------------------------------------------
# An ADOPT record must be pinned to what SOMEONE ELSE can pull
# ---------------------------------------------------------------------------
#
# `_image_digest` is `docker image inspect --format {{.Id}}` — the daemon's LOCAL content
# id — and it fed BOTH `image_digest` (right: BUILT asks whether the image resolves in this
# daemon) and `content_digest` (wrong: `verify_env_recipe`'s adopt branch compares
# content_digest against `registry_manifest_digest(image)`).
#
# Measured 2026-08-07 on quay.io/biocontainers/miniprot: the manifest digest is
# sha256:2eb53fea… and the config-blob digest `.Id` returns is sha256:65a4f971…. Two
# different values. It shipped green only because THIS machine runs the containerd
# snapshotter, where `.Id` happens to equal the manifest digest; on a classic overlay2
# daemon — the common case, and what a colleague or CI has — the recorded anchor is a
# string nobody can pull, and verify_env_recipe returns `broke /
# freeze.recipe_not_reproduced` for a recipe that is entirely correct.
#
# container_build.py:209-216 documents this exact confusion as already fixed, and the
# adopt_image block in the same function already used registry_manifest_digest. The fix
# had landed on the reader and on the sibling field, never on this producer.

def _mock_registry_digest(monkeypatch, manifest_digest):
    import agent.skills.container_build as CB
    monkeypatch.setattr(CB, "registry_manifest_digest", lambda ref: manifest_digest)


def test_an_adopt_record_anchors_on_the_registry_manifest_not_the_local_id(tmp_path, monkeypatch):
    local_id = "sha256:" + "65" * 32          # what `docker image inspect .Id` returns
    manifest = "sha256:" + "2e" * 32          # what anyone else can pull
    _mock_docker(monkeypatch, digest=local_id)
    _mock_registry_digest(monkeypatch, manifest)
    cache = _Cache()
    out = F.freeze_from_image(
        image="quay.io/biocontainers/miniprot:0.13", name="miniprot_bc", version="0.13",
        tools=[{"name": "miniprot", "evidence": "miniprot --version"}],
        build_method="adopt-image", env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] in ("proven", "degraded"), out

    # the PULLABLE address is the reproducibility anchor…
    assert out["content_digest"] == manifest
    # …while the LOCAL handle stays local, because that is what BUILT resolves.
    assert out["image_digest"] == local_id

    import yaml
    recipe = yaml.safe_load((tmp_path / "miniprot_bc.recipe.yaml").read_text())
    assert recipe["content_digest"] == manifest, (
        "the recipe's content_digest is what verify_env_recipe compares against "
        "registry_manifest_digest — a local .Id there cannot be reproduced by anyone else")


def test_an_unpullable_adopt_image_keeps_the_local_id_and_says_it_is_unpinnable(tmp_path, monkeypatch):
    """No registry digest means the image was never pulled from a registry. Say so — do not
    invent a pin — and do not lose the local handle BUILT needs."""
    local_id = "sha256:" + "65" * 32
    _mock_docker(monkeypatch, digest=local_id)
    _mock_registry_digest(monkeypatch, "")
    cache = _Cache()
    out = F.freeze_from_image(
        image="local-only:latest", name="localish", version="1",
        tools=[{"name": "miniprot", "evidence": "miniprot --version"}],
        build_method="adopt-image", env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] in ("proven", "degraded"), out
    assert out["content_digest"] == local_id
    rec = list(cache.registered.values())[0]
    assert rec.get("image_by_digest") in (None, "")
    assert "cannot be pinned" in rec["adopt_pin_error"]


def test_the_cached_record_carries_the_pin_the_recipe_carries(tmp_path, monkeypatch):
    """`env_cache.register` fired BEFORE `image_by_digest` was assigned, so every cached
    adopt-image record held `image_by_digest: None` while the recipe beside it held a
    correct `…@sha256:…`. `attestation.py:152-153` reads the cached key, so the provenance
    document lost the pin. Measured: all three adopt-image entries in the corpus."""
    _mock_docker(monkeypatch, digest="sha256:" + "65" * 32)
    _mock_registry_digest(monkeypatch, "sha256:" + "2e" * 32)
    cache = _Cache()
    F.freeze_from_image(
        image="quay.io/biocontainers/miniprot:0.13", name="miniprot_bc", version="0.13",
        tools=[{"name": "miniprot", "evidence": "miniprot --version"}],
        build_method="adopt-image", env_cache=cache, env_dir=tmp_path)
    rec = list(cache.registered.values())[0]
    assert rec["image_by_digest"] == "quay.io/biocontainers/miniprot@sha256:" + "2e" * 32


# ---------------------------------------------------------------------------
# Provenance is OBSERVED, never claimed
# ---------------------------------------------------------------------------
#
# `build_method` and `dockerfile_source` are observations: only the executor that built
# the image may write them. The MCP surface does not accept them, an image handed in by
# reference is adopted, and a LOCAL image (no registry digest) is recorded as a build this
# record did not observe — in the record, the recipe, the ENV report and the attestation.

import asyncio
from pathlib import Path

import yaml


def _mcp_tool(name):
    from agent.mcp_server import mcp
    return asyncio.run(mcp.get_tool(name))


def test_the_mcp_freeze_from_image_accepts_no_build_method_or_dockerfile_source():
    """The parameters a caller could use to CLAIM a provenance are gone from the surface,
    and the description says what happens to an image built outside the record."""
    tool = _mcp_tool("freeze_from_image")
    props = tool.parameters["properties"]
    assert "build_method" not in props, "build_method is an observation, not an input"
    assert "dockerfile_source" not in props, "dockerfile_source is an observation, not an input"
    assert {"image", "tools", "name", "version", "platform", "gated", "licenses"} <= set(props)
    desc = " ".join((tool.description or "").split())   # the served text keeps its line wraps
    assert "adopted with its build unobserved" in desc
    assert "build_env_from_authors_recipe(patches=" in desc


def test_the_mcp_authors_recipe_tool_publishes_patches_and_explains_them():
    tool = _mcp_tool("build_env_from_authors_recipe")
    props = tool.parameters["properties"]
    assert props["patches"]["type"] == "array"
    assert props["patches"]["default"] == []
    desc = " ".join((tool.description or "").split())
    for needle in ("`patches`", "find", "replace", "reason",
                   "authors_recipe.patch_no_match", "authors_recipe.patch_ambiguous",
                   "never claims an unmodified Dockerfile"):
        assert needle in desc, f"the patches paragraph does not say {needle!r}"


def _adopt_local(tmp_path, monkeypatch, image="talos-amd64:v12.2.0"):
    """A tag that is present in the daemon and carries no registry manifest digest —
    the shape of an image the agent built by hand and then handed in."""
    _mock_docker(monkeypatch, digest="sha256:" + "65" * 32)
    _mock_registry_digest(monkeypatch, "")
    cache = _Cache()
    out = F.freeze_from_image(
        image=image, name="talos", version="12.2.0",
        tools=[{"name": "talos", "evidence": "talos --help"}],
        env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] in ("proven", "degraded"), out
    return out, cache.registered[out["request_key"]]


def test_a_local_image_is_adopted_with_its_build_unobserved(tmp_path, monkeypatch):
    out, rec = _adopt_local(tmp_path, monkeypatch)
    assert out["build_method"] == "adopt-image" and out["image_origin"] == "local"
    assert rec["build_method"] == "adopt-image"
    assert rec["image_origin"] == "local"
    assert "dockerfile_source" not in rec, "nothing observed a build, so none is recorded"


def test_a_local_image_recipe_and_report_say_the_build_was_not_observed(tmp_path, monkeypatch):
    _adopt_local(tmp_path, monkeypatch)
    recipe = yaml.safe_load((tmp_path / "talos.recipe.yaml").read_text())
    assert recipe["image_origin"] == "local"
    assert recipe["build_method"] == "adopt" and recipe["dockerfile_source"] == {}
    md = (tmp_path / "talos.recipe.md").read_text()
    assert "local — a local image whose build this record did not observe" in md
    assert "A local image whose build this record did not observe" in md
    assert "docker pull talos-amd64" not in md
    html = (tmp_path / "talos.ENV.html").read_text()
    assert "a local image whose build this record did not observe" in html
    assert "apptainer pull docker://talos-amd64" not in html, (
        "a pull line for a local tag works on exactly one machine")
    assert "nothing in this record saw how the image was built" in html
    att = json.loads((tmp_path / "talos.attestation.json").read_text())
    assert att["predicate"]["buildDefinition"]["internalParameters"]["image_origin"] == "local"
    assert "authors_recipe" not in att["predicate"]["buildDefinition"]["externalParameters"]


def test_an_image_already_local_with_a_registry_digest_is_origin_registry(tmp_path, monkeypatch):
    _mock_docker(monkeypatch, digest="sha256:" + "65" * 32)
    _mock_registry_digest(monkeypatch, "sha256:" + "2e" * 32)
    cache = _Cache()
    out = F.freeze_from_image(
        image="quay.io/biocontainers/miniprot:0.13", name="miniprot_bc", version="0.13",
        tools=[{"name": "miniprot", "evidence": "miniprot --version"}],
        env_cache=cache, env_dir=tmp_path)
    assert out["image_origin"] == "registry"
    rec = list(cache.registered.values())[0]
    assert rec["image_origin"] == "registry"
    html = (tmp_path / "miniprot_bc.ENV.html").read_text()
    assert "pulled from a registry" in html and "did not observe" not in html
    md = (tmp_path / "miniprot_bc.recipe.md").read_text()
    assert "registry — pulled from a registry" in md


def test_an_image_pulled_by_this_call_is_origin_registry(tmp_path, monkeypatch):
    """The image is absent, the call pulls it: that pull IS the observation of where it
    came from, even when the daemon then reports no repo digest for it."""
    _mock_docker(monkeypatch, digest="sha256:" + "65" * 32)
    _mock_registry_digest(monkeypatch, "")
    pulled = {"done": False}
    monkeypatch.setattr(F, "_image_present",
                        lambda image: image == F._CONTROL_IMAGE or pulled["done"])

    def fake_sh(argv, timeout=300, **kw):
        if argv[:2] == ["docker", "pull"]:
            pulled["done"] = True
            return {"rc": 0, "out": "", "err": ""}
        return {"rc": 1, "out": "", "err": "not mocked"}
    monkeypatch.setattr(F, "_sh", fake_sh)
    cache = _Cache()
    out = F.freeze_from_image(
        image="quay.io/x/y:1", name="y", version="1",
        tools=[{"name": "y", "evidence": "y --version"}],
        env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] in ("proven", "degraded"), out
    assert pulled["done"] is True
    assert out["image_origin"] == "registry"


# ---------------------------------------------------------------------------
# patches= on the authors' path — the honest way to fix an authors' recipe
# ---------------------------------------------------------------------------
#
# The clone and the build are faked: `git clone` materialises a Dockerfile into the
# checkout, `docker buildx build` succeeds, and the freeze half runs under _mock_docker.
# What is under test is the step between them — apply, record, refuse — and that every
# deliverable carries the edits.

_DOCKERFILE = ("FROM debian:bookworm-slim\n"
               "RUN apt-get update && apt-get install -y curl\n"
               "RUN pip install uv\n")


def _mock_authors_build(monkeypatch, dockerfile=_DOCKERFILE, build_rc=0,
                        recipe_path="docker/Dockerfile"):
    calls: list[list[str]] = []

    def fake_sh(argv, timeout=300, **kw):
        calls.append(list(argv))
        if argv[:2] == ["git", "clone"]:
            target = Path(argv[-1]) / recipe_path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(dockerfile)
            return {"rc": 0, "out": "", "err": ""}
        if argv[:2] == ["git", "-C"] and "rev-parse" in argv:
            return {"rc": 0, "out": "56b47ee" + "0" * 33 + "\n", "err": ""}
        if argv[:3] == ["docker", "buildx", "build"]:
            # the build reads the Dockerfile as it is at this moment — remember it
            df = argv[argv.index("-f") + 1]
            calls.append(["<built>", Path(df).read_text()])
            return {"rc": build_rc, "out": "",
                    "err": "" if build_rc == 0 else "error: git: command not found"}
        return {"rc": 1, "out": "", "err": "not mocked"}
    monkeypatch.setattr(F, "_sh", fake_sh)
    return calls


_GIT_PATCH = {"file": "docker/Dockerfile",
              "find": "apt-get install -y curl",
              "replace": "apt-get install -y curl git",
              "reason": "uv clones a git dependency from the lock file; the authors' image lacks git"}


def _build(tmp_path, monkeypatch, patches, dockerfile=_DOCKERFILE):
    _mock_docker(monkeypatch)
    calls = _mock_authors_build(monkeypatch, dockerfile=dockerfile)
    cache = _Cache()
    out = F.build_from_authors_recipe(
        repo="populationgenomics/talos", ref="v12.2.0", recipe="docker/Dockerfile",
        name="talos", version="12.2.0",
        tools=[{"name": "talos", "evidence": "talos --help"}],
        patches=patches, env_cache=cache, env_dir=tmp_path)
    return out, cache, calls


def test_a_patch_is_applied_before_the_build_and_recorded_with_it(tmp_path, monkeypatch):
    out, cache, calls = _build(tmp_path, monkeypatch, [_GIT_PATCH])
    assert out["outcome"] == "proven", out
    assert out["build_method"] == "authors-dockerfile" and out["image_origin"] == "built"
    built = [c[1] for c in calls if c[0] == "<built>"]
    assert built and "apt-get install -y curl git" in built[0], "docker built the UNPATCHED file"
    rec = cache.registered[out["request_key"]]
    ds = rec["dockerfile_source"]
    assert ds["repo"] == "https://github.com/populationgenomics/talos"
    assert ds["commit"].startswith("56b47ee") and ds["tag"] == "v12.2.0"
    assert "apt-get install -y curl git" in ds["dockerfile"], "the Dockerfile AS BUILT is recorded"
    assert len(ds["patches"]) == 1
    p = ds["patches"][0]
    assert {k: p[k] for k in ("file", "find", "replace", "reason")} == _GIT_PATCH
    assert len(p["sha256_before"]) == 64 and len(p["sha256_after"]) == 64
    assert p["sha256_before"] != p["sha256_after"]
    import hashlib
    assert p["sha256_before"] == hashlib.sha256(_DOCKERFILE.encode()).hexdigest()
    assert p["sha256_after"] == hashlib.sha256(ds["dockerfile"].encode()).hexdigest()


def test_the_recorded_patches_reach_every_deliverable(tmp_path, monkeypatch):
    out, cache, _ = _build(tmp_path, monkeypatch, [_GIT_PATCH])
    assert out["outcome"] == "proven", out
    recipe = yaml.safe_load((tmp_path / "talos.recipe.yaml").read_text())
    assert recipe["image_origin"] == "built"
    assert recipe["dockerfile_source"]["patches"][0]["find"] == _GIT_PATCH["find"]
    assert recipe["dockerfile_source"]["patches"][0]["reason"] == _GIT_PATCH["reason"]
    md = (tmp_path / "talos.recipe.md").read_text()
    assert "### Patches applied to the authors' source (1)" in md
    assert "`docker/Dockerfile`** · uv clones a git dependency" in md
    assert "--- find\napt-get install -y curl\n+++ replace\napt-get install -y curl git" in md
    assert "the 1 recorded patch above would have to be re-applied to the pinned source" in md
    html = (tmp_path / "talos.ENV.html").read_text()
    assert "Built from the authors&#x27; own Dockerfile" in html or "Built from the authors' own Dockerfile" in html
    assert "1 patch applied" in html
    assert "uv clones a git dependency from the lock file" in html
    assert "apt-get install -y curl git" in html
    assert "built under this record" in html
    att = json.loads((tmp_path / "talos.attestation.json").read_text())
    bd = att["predicate"]["buildDefinition"]
    src = bd["externalParameters"]["authors_recipe"]
    assert src["repo"] == "https://github.com/populationgenomics/talos"
    assert src["patches"] == [{**_GIT_PATCH,
                               "sha256_before": recipe["dockerfile_source"]["patches"][0]["sha256_before"],
                               "sha256_after": recipe["dockerfile_source"]["patches"][0]["sha256_after"],
                               "authored_by": "agent"}]
    assert bd["internalParameters"]["image_origin"] == "built"


def test_an_unpatched_authors_build_records_an_empty_patch_list(tmp_path, monkeypatch):
    """No patches is a statement — "built as published" — and the record makes it."""
    out, cache, calls = _build(tmp_path, monkeypatch, [])
    assert out["outcome"] == "proven", out
    rec = cache.registered[out["request_key"]]
    assert rec["dockerfile_source"]["patches"] == []
    assert rec["dockerfile_source"]["dockerfile"] == _DOCKERFILE
    md = (tmp_path / "talos.recipe.md").read_text()
    assert "Patches applied" not in md and "re-applied" not in md
    html = (tmp_path / "talos.ENV.html").read_text()
    assert "No patches: the Dockerfile at the pinned commit was built as published" in html
    att = json.loads((tmp_path / "talos.attestation.json").read_text())
    assert "patches" not in att["predicate"]["buildDefinition"]["externalParameters"]["authors_recipe"]


def test_a_patch_whose_text_is_absent_is_refused_before_anything_is_built(tmp_path, monkeypatch):
    bad = dict(_GIT_PATCH, find="RUN pip install nothing-of-the-sort")
    out, cache, calls = _build(tmp_path, monkeypatch, [bad])
    assert out["outcome"] == "refused" and out["code"] == "authors_recipe.patch_no_match"
    assert "docker/Dockerfile" in out["error"]
    assert "RUN pip install nothing-of-the-sort" in out["error"]
    assert out["file"] == "docker/Dockerfile" and out["patch_index"] == 0
    assert not any(c[:3] == ["docker", "buildx", "build"] for c in calls), "it built anyway"
    assert cache.registered == {}


def test_a_no_match_refusal_quotes_at_most_80_characters_of_the_find_text(tmp_path, monkeypatch):
    long_find = "Z" * 200
    out, _, _ = _build(tmp_path, monkeypatch, [dict(_GIT_PATCH, find=long_find)])
    assert out["code"] == "authors_recipe.patch_no_match"
    assert "Z" * 80 + "…" in out["error"]
    assert "Z" * 81 not in out["error"]


def test_a_missing_file_is_a_no_match_that_names_the_file(tmp_path, monkeypatch):
    out, _, calls = _build(tmp_path, monkeypatch, [dict(_GIT_PATCH, file="docker/Dockerfile.gpu")])
    assert out["code"] == "authors_recipe.patch_no_match"
    assert "docker/Dockerfile.gpu" in out["error"] and "does not exist" in out["error"]
    assert not any(c[:3] == ["docker", "buildx", "build"] for c in calls)


def test_a_patch_that_matches_twice_is_refused_as_ambiguous(tmp_path, monkeypatch):
    twice = "FROM debian\nRUN apt-get update\nRUN apt-get update\n"
    out, cache, calls = _build(tmp_path, monkeypatch,
                               [dict(_GIT_PATCH, find="apt-get update", replace="apt-get update -q")],
                               dockerfile=twice)
    assert out["outcome"] == "refused" and out["code"] == "authors_recipe.patch_ambiguous"
    assert out["occurrences"] == 2 and "occurs 2 times" in out["error"]
    assert "docker/Dockerfile" in out["error"]
    assert not any(c[:3] == ["docker", "buildx", "build"] for c in calls)
    assert cache.registered == {}


@pytest.mark.parametrize("broken,missing", [
    ({"file": "docker/Dockerfile", "find": "curl", "replace": "curl git"}, "`reason`"),
    ({"find": "curl", "replace": "curl git", "reason": "r"}, "`file`"),
    ({"file": "docker/Dockerfile", "replace": "x", "reason": "r"}, "`find`"),
    ({"file": "docker/Dockerfile", "find": "curl", "replace": None, "reason": "r"}, "`replace`"),
])
def test_a_malformed_patch_is_refused_naming_the_missing_field(tmp_path, monkeypatch, broken, missing):
    out, cache, calls = _build(tmp_path, monkeypatch, [broken])
    assert out["outcome"] == "refused" and out["code"] == "authors_recipe.patch_malformed"
    assert missing in out["error"]
    assert not any(c[:3] == ["docker", "buildx", "build"] for c in calls)


def test_a_patch_may_not_reach_outside_the_checkout(tmp_path, monkeypatch):
    out, _, _ = _build(tmp_path, monkeypatch,
                       [dict(_GIT_PATCH, file="../../../../etc/hosts", find="localhost")])
    assert out["code"] == "authors_recipe.patch_malformed"
    assert "not a path inside the checkout" in out["error"]


def test_a_non_dict_patch_is_refused_as_malformed(tmp_path, monkeypatch):
    out, _, _ = _build(tmp_path, monkeypatch, ["sed -i s/curl/curl git/ Dockerfile"])
    assert out["code"] == "authors_recipe.patch_malformed"


def test_patches_apply_in_order_and_each_sees_the_previous_result(tmp_path, monkeypatch):
    second = {"file": "docker/Dockerfile", "find": "curl git", "replace": "curl git ca-certificates",
              "reason": "git over https needs the CA bundle"}
    out, cache, calls = _build(tmp_path, monkeypatch, [_GIT_PATCH, second])
    assert out["outcome"] == "proven", out
    ds = cache.registered[out["request_key"]]["dockerfile_source"]
    assert [p["reason"] for p in ds["patches"]] == [_GIT_PATCH["reason"], second["reason"]]
    assert ds["patches"][0]["sha256_after"] == ds["patches"][1]["sha256_before"]
    assert "apt-get install -y curl git ca-certificates" in ds["dockerfile"]
    md = (tmp_path / "talos.recipe.md").read_text()
    assert "Patches applied to the authors' source (2)" in md
    assert "the 2 recorded patches above would have to be re-applied" in md


def test_a_patched_build_that_still_fails_is_the_same_honest_build_failure(tmp_path, monkeypatch):
    _mock_docker(monkeypatch)
    _mock_authors_build(monkeypatch, build_rc=1)
    out = F.build_from_authors_recipe(
        repo="populationgenomics/talos", ref="v12.2.0", recipe="docker/Dockerfile",
        name="talos", tools=[{"name": "talos", "evidence": "talos --help"}],
        patches=[_GIT_PATCH], env_cache=_Cache(), env_dir=tmp_path)
    assert out["outcome"] == "broke" and out["code"] == "authors_recipe.build_failed"


# ---------------------------------------------------------------------------
# verify_env_recipe knows about the patches and about a local adopt image
# ---------------------------------------------------------------------------

def test_verify_env_recipe_names_the_patches_a_rebuild_must_reapply(tmp_path):
    from agent.mcp_tools import freeze_tools as FT
    recipe = {"name": "talos", "build_method": "authors-dockerfile",
              "content_digest": "sha256:" + "7f" * 32,
              "dockerfile_source": {"repo": "https://github.com/populationgenomics/talos",
                                    "commit": "c" * 40, "recipe_path": "docker/Dockerfile",
                                    "patches": [dict(_GIT_PATCH, sha256_before="1" * 64,
                                                     sha256_after="2" * 64)]}}
    p = tmp_path / "talos.recipe.yaml"
    p.write_text(yaml.safe_dump(recipe))
    out = FT.verify_env_recipe(str(p))
    assert out["outcome"] == "refused" and out["code"] == "freeze.recipe_verify_unavailable"
    assert out["success"] is False and out["patches_recorded"] == 1
    assert "patches=<the 1 recorded patch>" in out["proves"]
    assert "docker/Dockerfile" in out["proves"]
    assert "would have to be re-applied to the pinned source" in out["proves"]


def test_verify_env_recipe_without_patches_does_not_mention_them(tmp_path):
    from agent.mcp_tools import freeze_tools as FT
    recipe = {"name": "talos", "build_method": "authors-dockerfile",
              "content_digest": "sha256:" + "7f" * 32,
              "dockerfile_source": {"repo": "https://github.com/populationgenomics/talos",
                                    "commit": "c" * 40, "patches": []}}
    p = tmp_path / "talos.recipe.yaml"
    p.write_text(yaml.safe_dump(recipe))
    out = FT.verify_env_recipe(str(p))
    assert out["code"] == "freeze.recipe_verify_unavailable" and out["patches_recorded"] == 0
    assert "patch" not in out["proves"]


def test_verify_env_recipe_explains_a_local_adopt_image_has_nothing_to_re_pull(tmp_path):
    from agent.mcp_tools import freeze_tools as FT
    recipe = {"name": "talos", "build_method": "adopt", "adopt_image": "",
              "image_origin": "local", "content_digest": "sha256:" + "65" * 32}
    p = tmp_path / "talos.recipe.yaml"
    p.write_text(yaml.safe_dump(recipe))
    out = FT.verify_env_recipe(str(p))
    assert out["outcome"] == "refused" and out["code"] == "freeze.recipe_adopt_no_image"
    assert out["image_origin"] == "local"
    assert "did not observe" in out["error"] and "build_env_from_authors_recipe" in out["error"]


# ---------------------------------------------------------------------------
# Identity by name is not identity — the known repo reaches capture()
# ---------------------------------------------------------------------------

_PYPI_OTHER_TALOS = {"available": True,
                     "summary": "Reproducible parameter sweeps for Keras, TensorFlow and PyTorch",
                     "home_page": "https://github.com/autonomio/talos",
                     "project_urls": {}, "package_url": "https://pypi.org/project/talos/"}


def _pypi_says(monkeypatch, probe):
    import agent.skills.resolver as R
    monkeypatch.setattr(R, "probe_pypi", lambda name, timeout=12: probe)


def test_the_authors_repo_turns_a_same_named_pypi_hit_into_a_collision(tmp_path, monkeypatch):
    """THE h_talos case: the SBOM says `talos` (pypi), PyPI's `talos` is a Keras sweep
    library, and the record KNOWS it was built from populationgenomics/talos. The other
    project's words must not describe this env — anywhere."""
    _mock_docker(monkeypatch)
    _pypi_says(monkeypatch, _PYPI_OTHER_TALOS)
    cache = _Cache()
    out = F.freeze_from_image(
        image="talos:12.2.0", name="talos", version="12.2.0",
        tools=[{"name": "talos", "evidence": "talos --help"}],
        build_method="authors-dockerfile",
        dockerfile_source={"repo": "https://github.com/populationgenomics/talos",
                           "commit": "c" * 40, "tag": "v12.2.0", "patches": []},
        env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] == "proven", out
    idn = cache.registered[out["request_key"]]["tool_identities"][0]
    assert idn["tool"] == "talos" and idn["package"] == "talos" and idn["source"] == "pypi"
    assert idn["self_description"] is None
    assert idn["collision"] == {"registry": "pypi",
                                "points_at": "https://github.com/autonomio/talos",
                                "known_repo": "https://github.com/populationgenomics/talos"}
    assert "same-named pypi package `talos` exists elsewhere" in idn["note"]
    html = (tmp_path / "talos.ENV.html").read_text()
    assert "name collision" in html and "autonomio/talos" in html
    assert "Reproducible parameter sweeps" not in html
    assert "describes it as" not in html
    md = (tmp_path / "talos.recipe.md").read_text()
    assert "name collision" in md and "Reproducible parameter sweeps" not in md
    att = json.loads((tmp_path / "talos.attestation.json").read_text())
    ti = att["predicate"]["buildDefinition"]["internalParameters"]["tool_identities"][0]
    assert ti["collision"]["registry"] == "pypi" and ti["self_description"] is None


def test_an_adopted_image_source_label_is_the_known_repo(tmp_path, monkeypatch):
    """No Dockerfile source on the adopt path — but the image names its own repository in
    `org.opencontainers.image.source`, and that observation anchors identity the same way."""
    _mock_docker(monkeypatch)
    _mock_registry_digest(monkeypatch, "")
    monkeypatch.setattr(F, "_image_source_label",
                        lambda image: "https://github.com/populationgenomics/talos")
    _pypi_says(monkeypatch, _PYPI_OTHER_TALOS)
    cache = _Cache()
    out = F.freeze_from_image(
        image="talos-amd64:v12.2.0", name="talos", version="12.2.0",
        tools=[{"name": "talos", "evidence": "talos --help"}],
        env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] in ("proven", "degraded"), out
    rec = cache.registered[out["request_key"]]
    assert rec["image_source_label"] == "https://github.com/populationgenomics/talos"
    idn = rec["tool_identities"][0]
    assert idn["self_description"] is None
    assert idn["collision"]["points_at"] == "https://github.com/autonomio/talos"


def test_a_registry_hit_anchored_to_the_known_repo_keeps_its_description(tmp_path, monkeypatch):
    _mock_docker(monkeypatch)
    _pypi_says(monkeypatch, dict(_PYPI_OTHER_TALOS,
                                 summary="Rare-disease variant prioritisation",
                                 home_page="https://github.com/populationgenomics/talos"))
    cache = _Cache()
    out = F.freeze_from_image(
        image="talos:12.2.0", name="talos", version="12.2.0",
        tools=[{"name": "talos", "evidence": "talos --help"}],
        build_method="authors-dockerfile",
        dockerfile_source={"repo": "https://github.com/populationgenomics/talos",
                           "commit": "c" * 40, "patches": []},
        env_cache=cache, env_dir=tmp_path)
    idn = cache.registered[out["request_key"]]["tool_identities"][0]
    assert idn["self_description"] == "Rare-disease variant prioritisation"
    assert idn["collision"] is None and idn["note"] is None


def test_without_a_known_repo_the_sbom_tie_is_the_anchor_as_before(tmp_path, monkeypatch):
    """No Dockerfile source, no label: nothing to check against, so the install-tied
    registry summary is disclosed as it always was — labelled unverified by the renders."""
    _mock_docker(monkeypatch)
    _mock_registry_digest(monkeypatch, "")
    _pypi_says(monkeypatch, _PYPI_OTHER_TALOS)
    cache = _Cache()
    out = F.freeze_from_image(
        image="talos-amd64:v12.2.0", name="talos", version="12.2.0",
        tools=[{"name": "talos", "evidence": "talos --help"}],
        env_cache=cache, env_dir=tmp_path)
    rec = cache.registered[out["request_key"]]
    assert "image_source_label" not in rec
    idn = rec["tool_identities"][0]
    assert idn["self_description"] == _PYPI_OTHER_TALOS["summary"]
    assert idn["collision"] is None


def test_the_known_repo_is_tied_to_the_primary_tool_only(tmp_path, monkeypatch):
    """The repo builds talos; the bcftools fork baked beside it has its own home. A
    registry `bcftools` pointing at samtools/bcftools is not a collision with
    populationgenomics/talos — the check must not spill onto the other tools."""
    monkeypatch.setattr(F, "_image_present", lambda image: True)
    monkeypatch.setattr(F, "_image_digest", lambda image: "sha256:" + "cd" * 32)
    monkeypatch.setattr(F, "_image_source_label", lambda image: "")
    import agent.skills.locus as LOC
    monkeypatch.setattr(LOC, "image_arch", lambda ref: {"resolved": True, "arch": "amd64"})
    import agent.skills.container_build as CB
    monkeypatch.setattr(CB.ContainerBuild, "conda_sbom_from_image", staticmethod(lambda *a, **k: []))
    monkeypatch.setattr(CB.ContainerBuild, "apt_sbom_from_image", staticmethod(lambda *a, **k: []))

    def _run(image, platform, command, timeout=300, maxlen=400):
        if image == F._CONTROL_IMAGE:
            return {"rc": 127, "out": "command not found"}
        if "importlib.metadata" in command:
            return {"rc": 0, "out": json.dumps(["talos==12.2.0", "bcftools==1.23"])}
        return {"rc": 0, "out": "ran"}
    monkeypatch.setattr(F, "_run_in_image", _run)
    import agent.skills.resolver as R
    monkeypatch.setattr(R, "probe_conda", lambda name, timeout=12: {"available": False})
    monkeypatch.setattr(R, "probe_pypi", lambda name, timeout=12: {
        "available": True, "summary": f"PYPI::{name}",
        "home_page": f"https://github.com/elsewhere/{name}", "project_urls": {}})
    cache = _Cache()
    out = F.freeze_from_image(
        image="talos:12.2.0", name="talos", version="12.2.0",
        tools=[{"name": "talos", "evidence": "talos --help"},
               {"name": "bcftools", "evidence": "bcftools --version"}],
        build_method="authors-dockerfile",
        dockerfile_source={"repo": "https://github.com/populationgenomics/talos",
                           "commit": "c" * 40, "patches": []},
        env_cache=cache, env_dir=tmp_path)
    assert out["outcome"] == "proven", out
    ids = {i["tool"]: i for i in cache.registered[out["request_key"]]["tool_identities"]}
    assert ids["talos"]["collision"] is not None and ids["talos"]["self_description"] is None
    assert ids["bcftools"]["collision"] is None
    assert ids["bcftools"]["self_description"] == "PYPI::bcftools"
