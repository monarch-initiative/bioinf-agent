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
