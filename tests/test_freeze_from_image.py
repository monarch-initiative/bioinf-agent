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
        # importlib SBOM probe returns a JSON list; everything else is the evidence
        if "importlib.metadata" in command:
            return {"rc": 0, "out": json.dumps(["talos==11.0.0"])}
        return {"rc": evidence_rc, "out": "ran"}
    monkeypatch.setattr(F, "_run_in_image", _run)
    # SBOM-from-image best-effort → force the importlib fallback path
    import agent.skills.container_build as CB
    monkeypatch.setattr(CB.ContainerBuild, "conda_sbom_from_image", staticmethod(lambda *a, **k: []))
    monkeypatch.setattr(CB.ContainerBuild, "apt_sbom_from_image", staticmethod(lambda *a, **k: []))


def test_adopt_image_happy_path_registers_and_renders(tmp_path, monkeypatch):
    _mock_docker(monkeypatch)
    cache = _Cache()
    out = F.freeze_from_image(
        image="ghcr.io/org/talos@sha256:abc", name="talos_authors", version="11.0.0",
        tools=[{"name": "talos", "evidence": "python -m talos --help"}],
        build_method="adopt-image", env_cache=cache, reports_dir=tmp_path)
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
        env_cache=cache, reports_dir=tmp_path)
    assert out["outcome"] == "refused"
    assert out["code"] == "freeze_from_image.honesty_violation"
    assert not cache.registered, "a failing-evidence image must NOT be registered"


def test_echo_cheat_evidence_is_refused(tmp_path, monkeypatch):
    _mock_docker(monkeypatch, evidence_rc=0)   # exits 0 but doesn't reference the tool
    cache = _Cache()
    out = F.freeze_from_image(
        image="img@sha256:abc", name="x", tools=[{"name": "talos", "evidence": "echo hello"}],
        env_cache=cache, reports_dir=tmp_path)
    assert out["outcome"] == "refused", out
    assert out["code"] == "freeze_from_image.honesty_violation"
    kinds = {v["invariant"] for v in out["honesty_violations"]}
    assert any("evidence_shape" in k for k in kinds), kinds


def test_no_tools_refused(tmp_path):
    out = F.freeze_from_image(image="img", name="x", tools=[],
                             env_cache=_Cache(), reports_dir=tmp_path)
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
    monkeypatch.setattr(F, "_run_in_image",
                        lambda image, platform, command, timeout=300, maxlen=400: {"rc": 0, "out": "ran"})
    cache = _Cache()
    out = F.freeze_from_image(
        image="img@sha256:abc", name="t", version="1",
        tools=[{"name": "talos", "evidence": "talos --help"}],
        build_method="adopt-image", env_cache=cache, reports_dir=tmp_path)
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
        env_cache=cache, reports_dir=tmp_path)
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
        env_cache=cache, reports_dir=tmp_path)
    assert out["outcome"] == "proven", out
    rec = cache.registered[out["request_key"]]
    from agent.skills.freeze import record_is_gated
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
        env_cache=cache, reports_dir=tmp_path)
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
    import agent.skills.container_build as CB
    monkeypatch.setattr(CB.ContainerBuild, "conda_sbom_from_image", staticmethod(lambda *a, **k: []))
    monkeypatch.setattr(CB.ContainerBuild, "apt_sbom_from_image", staticmethod(lambda *a, **k: []))

    def _run(image, platform, command, timeout=300, maxlen=400):
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
        env_cache=cache, reports_dir=tmp_path)
    assert out["outcome"] == "proven"
    rec = cache.registered[out["request_key"]]
    sb = {s["tool"]: s["version"] for s in rec["shipped_binaries"]}
    assert sb["bcftools"] == "9cef4057"      # the fork's own identity, captured
    assert sb["bcftools"] != "1.23.1"        # NOT the dependency htslib scraped from line 2
    assert sb["echtvar"] == "0.2.2"
