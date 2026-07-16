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
    assert "git checkout v11.0.1" in md
