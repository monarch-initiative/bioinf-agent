"""L15 — REAL validated==shipped for the AUTHORS'-IMAGE path (freeze_from_image).

The sibling test certifies the container-native BUILD path on real bytes (pigz, pure
conda). This certifies the OTHER executor — freeze_from_image, the authors'-own-image /
authors'-Dockerfile path — on real bytes: a genuine `docker build` of a small NON-conda
image, then a real freeze_from_image over it. The unit tests mock every docker call
(they prove the honesty DECISION logic); this proves the thing mocks can't — that the
same contract (BUILT · VALIDATED_IN_IMAGE · POLICY_CLEAN) passes when the evidence
command ACTUALLY runs the tool inside a real image, and that the SBOM/deliverables render
from the true record.

Self-contained: builds its own image from an inline Dockerfile (no registry pull, no
network), so it's reliable in CI. The image is debian-slim (bash present, as every real
authors' image has — Talos's own base is python:slim-bullseye; NO conda prefix), so it
also exercises freeze_from_image's non-conda system (apt) SBOM path. Opt-in via
`-m integration_docker`; skipped when no Docker daemon is present.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

pytestmark = pytest.mark.integration_docker

_TAG = "bioinf-l15-ffi:1"
_DOCKERFILE = (
    "FROM debian:bookworm-slim\n"
    "RUN printf '#!/bin/sh\\necho \"mytool 1.0 ok\"\\n' > /usr/local/bin/mytool "
    "&& chmod +x /usr/local/bin/mytool\n"
)


def _docker_up() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode == 0
    except Exception:
        return False


class _Cache:
    def __init__(self):
        self.registered = {}

    def register(self, key, record):
        self.registered[key] = record


def test_real_freeze_from_image_is_honest_and_validated_in_image(tmp_path):
    """A real freeze_from_image over a genuinely-built non-conda image must come back
    proven with the honesty contract satisfied on the REAL record: the tool's evidence
    RAN in-image (not a mock), and all four deliverables rendered."""
    if not _docker_up():
        pytest.skip("no Docker daemon — L15 real-build tier is opt-in")
    from agent.skills import freeze_from_image as F
    from agent.skills.env_honesty import check_build

    build = subprocess.run(["docker", "build", "--platform", "linux/amd64", "-t", _TAG, "-"],
                           input=_DOCKERFILE, text=True, capture_output=True, timeout=600)
    if build.returncode != 0:
        pytest.skip(f"docker build unavailable: {build.stderr[-200:]}")
    try:
        cache = _Cache()
        out = F.freeze_from_image(
            image=_TAG, name="l15_ffi", version="1.0",
            tools=[{"name": "mytool", "evidence": "mytool"}],   # evidence that RUNS the tool
            build_method="authors-dockerfile",
            dockerfile_source={"repo": "local", "commit": "test"},
            env_cache=cache, reports_dir=tmp_path, pull_if_absent=False)

        # proven, and the env registered by digest
        assert out["outcome"] == "proven", out
        assert out["image_digest"].startswith("sha256:"), out
        assert cache.registered, "a passing image must register"
        rec = list(cache.registered.values())[0]

        # VALIDATED_IN_IMAGE — the evidence actually PASSED in the real image
        vs = rec["verifications"]
        assert vs and all(v["passed"] for v in vs), vs
        assert any(v["tool"] == "mytool" for v in vs)

        # the honesty contract returns ZERO violations on the REAL record
        assert check_build(rec) == [], f"real freeze_from_image must satisfy the contract: {check_build(rec)}"

        # non-conda image → system SBOM captured (apk layer), not a conda closure
        assert isinstance(rec.get("system_packages"), list)

        # all four deliverables rendered from the verified record
        for f in ("l15_ffi.ENV.html", "l15_ffi.attestation.json",
                  "l15_ffi.recipe.yaml", "l15_ffi.recipe.md"):
            assert (tmp_path / f).is_file(), f"missing deliverable {f}"
    finally:
        subprocess.run(["docker", "rmi", "-f", _TAG], capture_output=True)


def test_real_freeze_from_image_refuses_non_running_evidence(tmp_path):
    """The contract must REFUSE on real bytes when the evidence doesn't actually run the
    tool — the guard a shallow reconstruction would slip past. Uses an evidence command
    that references a tool NOT present in the image (fails in-image → refused)."""
    if not _docker_up():
        pytest.skip("no Docker daemon — L15 real-build tier is opt-in")
    from agent.skills import freeze_from_image as F

    build = subprocess.run(["docker", "build", "--platform", "linux/amd64", "-t", _TAG, "-"],
                           input=_DOCKERFILE, text=True, capture_output=True, timeout=600)
    if build.returncode != 0:
        pytest.skip(f"docker build unavailable: {build.stderr[-200:]}")
    try:
        cache = _Cache()
        out = F.freeze_from_image(
            image=_TAG, name="l15_ffi_bad", version="1.0",
            tools=[{"name": "notinstalled", "evidence": "notinstalled --version"}],
            build_method="authors-dockerfile", env_cache=cache, reports_dir=tmp_path,
            pull_if_absent=False)
        assert out["outcome"] == "refused", out
        assert out["code"] == "freeze_from_image.honesty_violation"
        assert not cache.registered, "a tool that doesn't run in-image must NOT register"
    finally:
        subprocess.run(["docker", "rmi", "-f", _TAG], capture_output=True)
