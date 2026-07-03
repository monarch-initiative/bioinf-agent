"""L15 — the REAL validated==shipped certification.

Every other build test mocks the container layer (`_sh`, `cb.validate_in_image`,
`cb.install`). Those prove the DECISION logic (a failed validation → broke; a
clean one → proven). This test proves the thing mocks can't: that an ACTUAL
Docker build produces an honest artifact — the bytes we validate ARE the bytes
that ship. It exercises the terminals only a real build reaches
(`container_build.started/frozen`, the pixi engine steps, and the real
`env_build.built` / `env_build.verified_in_image` / `container_build.
validated_in_image` running against a genuine image) and asserts the honesty
contract passes on the real record.

Provenance: writing this test SURFACED a real crash — pigz's banner probe emits
gzip magic (0x1f 0x8b) to stdout, and `_sh(text=True)` raised UnicodeDecodeError
and killed the build. No mocked test could have caught it. Fixed with
errors="replace" (regression-locked docker-free in test_invariants.py).

Tool: `pigz` — tiny conda-forge package, clean `pigz --version` evidence, small
closure so the solve+build stays fast (~50s warm). Opt-in via
`-m integration_docker`; skipped when no Docker daemon is present.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

pytestmark = pytest.mark.integration_docker


def _docker_up() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=15).returncode == 0
    except Exception:
        return False


def test_real_build_from_tools_is_honest_and_validated_in_image():
    """A genuine container-native build of `pigz` must come back BUILT +
    VALIDATED_IN_IMAGE + POLICY_CLEAN — the honesty contract passing on real
    bytes, not a mock. This is the literal validated==shipped guarantee, and it
    exercises the docker-only build terminals (started / frozen / pixi_* /
    real verified_in_image) that mocks can't reach."""
    if not _docker_up():
        pytest.skip("no Docker daemon — L15 real-build tier is opt-in")
    from agent.skills import env_freeze
    from agent.skills.env_honesty import check_build

    res = env_freeze.build_env_from_tools(name="bioinf_l15_realbuild", tools=["pigz"])

    # BUILT — a real image handle + content id resolved. (The BuildResult is a raw
    # result dict carrying `success`, not an outcome-tagged return.)
    assert res.get("success") is True, res
    assert (res.get("image") or "").strip(), res
    assert (res.get("image_digest") or "").startswith("sha256:"), res
    assert (res.get("content_digest") or "").startswith("sha256:"), res

    # VALIDATED_IN_IMAGE — pigz's evidence actually PASSED inside the shipped image.
    verifs = res.get("verifications") or []
    assert verifs, "a real build must record in-image verifications"
    pigz = [v for v in verifs if v.get("tool") == "pigz"]
    assert pigz and all(v.get("passed") for v in pigz), verifs

    # The honesty contract itself returns ZERO violations on the REAL record —
    # BUILT · VALIDATED_IN_IMAGE (non-cheat shape + passed) · POLICY_CLEAN. This
    # is the whole point: the record rendered from real bytes is self-consistent.
    assert res.get("honesty_violations") in (None, [], {}), res
    assert check_build(res) == [], f"real build must satisfy the honesty contract: {check_build(res)}"

    # Validation ran natively or under emulation, and is disclosed as such (I7
    # timings are authoritative only when native).
    assert res.get("validation_locus") in ("native", "emulated"), res
