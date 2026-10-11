"""Shared stand-ins for tests of the image-based freeze routes (freeze_from_image,
build_env_from_authors_recipe): a cache that records registrations and a docker
stub that makes the honesty contract testable without a daemon.
"""
from __future__ import annotations

import json

from agent.skills import freeze_from_image as F


class FakeCache:
    """An EnvCache that remembers what freeze registered, and nothing else."""
    def __init__(self):
        self.registered = {}

    def register(self, key, record):
        self.registered[key] = record


def mock_docker(monkeypatch, *, evidence_rc=0, digest="sha256:" + "ab" * 32):
    """Stub every docker-facing seam freeze_from_image touches, so the honesty flow is
    testable without a daemon: the image is present with `digest`, evidence answers
    `evidence_rc` in the image and fails in the control image, the SBOM falls back to
    the importlib probe, and no registry is reached."""
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
