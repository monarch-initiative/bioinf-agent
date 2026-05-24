"""
EnvBuild — the core orchestrator (library, interface-agnostic).

Turns a set of tool requests into a content-addressed, machine-verified env IMAGE.
This is THE product; MCP / CLI / skill are faces on top (not here). It sequences:

    declare (conda/pip, one co-solve via the engine)
      + install (each long-tail tool via an install_commands generator)
      → freeze (emit Dockerfile + buildx for the ship platform)
      → VERIFY: re-run every tool's evidence INSIDE the shipped image.

The honesty gate in the container-native model is "validated == shipped": a build
is accepted only if every declared tool's evidence passes in the exact image we
ship. That re-anchors the old host-disk invariants (re-hash/clone-check/verify on
the host) onto the image itself — the bytes the user runs ARE the bytes verified.
Emits a BuildResult (image + image_digest + content_digest + per-tool evidence).

Pure helpers (content_digest, the build plan) are unit-testable; build()/verify()
drive a real container and are exercised by live verification.
"""

from __future__ import annotations

from typing import Any, Optional

from agent.skills import freeze as _freeze
from agent.skills.container_build import ContainerBuild, EnvEngine


class EnvBuild:
    def __init__(self, name: str, version: str = "", *, platform: str = "linux/amd64",
                 base: str = "debian:bookworm-slim", engine: Optional[EnvEngine] = None,
                 channels: Optional[list[str]] = None):
        self.name = name
        self.version = version
        self.platform = platform
        self.cb = ContainerBuild(base=base, platform=platform, engine=engine, channels=channels)
        self.conda_specs: list[str] = []
        self.tools: list[dict] = []           # long-tail generator specs
        self.verifications: list[dict] = []   # [{label, check, engine_coupled}]
        self.lock_text = ""

    # -- DECLARE the request (no container work yet) -----------------------
    def add_conda(self, specs: list[str], verify: list[tuple[str, str]]) -> "EnvBuild":
        """Add conda/pip specs (co-solved together) + how to verify each in the
        image: verify = [(label, tool_command)], run via the engine."""
        self.conda_specs += specs
        for label, cmd in verify:
            self.verifications.append({"label": label, "check": cmd, "engine_coupled": True})
        return self

    def add_tool(self, spec: dict) -> "EnvBuild":
        """Add a long-tail tool from an install_commands generator. Its `evidence`
        (with the right engine coupling) becomes the in-image verification."""
        self.tools.append(spec)
        self.verifications.append({"label": spec.get("purpose", "tool"),
                                   "check": spec["evidence"],
                                   "engine_coupled": spec.get("engine_coupled", False)})
        return self

    # -- BUILD in the container -------------------------------------------
    def build(self) -> dict[str, Any]:
        s = self.cb.start()
        if not s.get("success"):
            return {"success": False, "stage": "start", **s}
        if self.conda_specs:
            d = self.cb.declare(self.conda_specs)
            if not d.get("success"):
                return {"success": False, "stage": "declare", **d}
            # capture the lock for the content digest (what was actually GOT)
            for art in self.cb.engine.lock_artifacts():
                cat = self.cb.exec(f"cat {self.cb.workdir}/{art} 2>/dev/null")
                self.lock_text += cat.get("stdout", "")
        for spec in self.tools:
            r = self.cb.install(spec)
            if not r.get("success"):
                return {"success": False, "stage": "install", "tool": spec.get("purpose"), **r}
        return {"success": True}

    # -- FREEZE + the honesty gate ---------------------------------------
    def freeze(self) -> dict[str, Any]:
        return self.cb.freeze(self.name, self.version)

    def verify_in_image(self, image: str) -> dict[str, Any]:
        """Honesty gate: re-run EVERY tool's evidence in the shipped image. A build
        is only honest if validated==shipped."""
        finals = [(v["label"], self.cb.engine.run(v["check"]) if v["engine_coupled"] else v["check"])
                  for v in self.verifications]
        res = self.cb.validate_in_image(image, [c for _, c in finals])
        return {"success": res["success"],
                "results": {lbl: res["checks"].get(c, {}) for lbl, c in finals}}

    # -- the content address ---------------------------------------------
    def content_digest(self) -> str:
        """sha256 over what was actually GOT: the engine lock + the long-tail
        commands + platform + engine. Identical requests → identical digest."""
        parts = {
            "lock": self.lock_text,
            "longtail": sorted(s["command"] for s in self.cb.longtail),
            "platform": self.platform,
            "engine": self.cb.engine.name if self.conda_specs else "none",
        }
        return _freeze.compute_content_digest(parts)

    # -- run it all -------------------------------------------------------
    def run(self) -> dict[str, Any]:
        """Full core flow → a BuildResult. Refuses (success=False) unless every
        tool verifies IN the shipped image."""
        try:
            b = self.build()
            if not b.get("success"):
                return b
            fr = self.freeze()
            if not fr.get("success"):
                return {"success": False, "stage": "freeze", **fr}
            digest = self.cb.image_digest(fr["image"])
            v = self.verify_in_image(fr["image"])
            return {
                "success": v["success"],
                "name": self.name, "version": self.version, "platform": self.platform,
                "engine": self.cb.engine.name if self.conda_specs else "none",
                "image": fr["image"], "image_digest": digest,
                "content_digest": self.content_digest(),
                "conda_specs": list(self.conda_specs),
                "longtail_steps": [{"command": s["command"], "purpose": s["purpose"]}
                                   for s in self.cb.longtail],
                "validated_in_shipped_image": v["success"],
                "verifications": v["results"],
            }
        finally:
            self.cb.close()
