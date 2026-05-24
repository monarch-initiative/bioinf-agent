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

from agent.skills import env_honesty as _honesty
from agent.skills import freeze as _freeze
from agent.skills import locus as _locus
from agent.skills.container_build import BASE_IMAGE, ContainerBuild, EnvEngine


class EnvBuild:
    def __init__(self, name: str, version: str = "", *, platform: str = "linux/amd64",
                 base: str = BASE_IMAGE, engine: Optional[EnvEngine] = None,
                 channels: Optional[list[str]] = None,
                 accelerator: Optional[dict] = None, license_gated: bool = False,
                 licenses: Optional[list[str]] = None, redistributable: bool = True):
        self.name = name
        self.version = version
        self.platform = platform
        self.cb = ContainerBuild(base=base, platform=platform, engine=engine, channels=channels)
        self.conda_specs: list[str] = []
        self.pip_specs: list[str] = []        # PyPI specs (engine --pypi, into the lock)
        self.tools: list[dict] = []           # long-tail generator specs
        self.verifications: list[dict] = []   # [{label, tool, check, engine_coupled}]
        self.lock_text = ""
        # POLICY_CLEAN inputs (I12/I13) — env-level claims the contract guards.
        self.accelerator = accelerator
        self.license_gated = license_gated
        self.licenses = licenses or []
        self.redistributable = redistributable

    # -- DECLARE the request (no container work yet) -----------------------
    def add_conda(self, specs: list[str], verify: list[tuple[str, str]]) -> "EnvBuild":
        """Add conda/pip specs (co-solved together) + how to verify each in the
        image: verify = [(label, tool_command)], run via the engine. The label is
        the tool token the contract's shape rule anchors on."""
        self.conda_specs += specs
        for label, cmd in verify:
            self.verifications.append({"label": label, "tool": label, "check": cmd,
                                       "engine_coupled": True})
        return self

    def add_pip(self, specs: list[str], verify: list[tuple[str, str]]) -> "EnvBuild":
        """Add PyPI specs (engine --pypi → into the lock, materializes with the conda
        layer) + how to verify each in the image. Verifications are engine-coupled
        (pip CLIs / `python -c import` run in the engine env, not the base PATH)."""
        self.pip_specs += specs
        for label, cmd in verify:
            self.verifications.append({"label": label, "tool": label, "check": cmd,
                                       "engine_coupled": True})
        return self

    def add_tool(self, spec: dict) -> "EnvBuild":
        """Add a long-tail tool from an install_commands generator. Its `evidence`
        (with the right engine coupling) becomes the in-image verification; its
        `tool` token anchors the anti-echo-cheat shape rule."""
        self.tools.append(spec)
        self.verifications.append({"label": spec.get("purpose", "tool"),
                                   "tool": spec.get("tool", ""),
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
        if self.pip_specs:
            dp = self.cb.declare_pypi(self.pip_specs)
            if not dp.get("success"):
                return {"success": False, "stage": "declare_pypi", **dp}
        if self.conda_specs or self.pip_specs:
            # capture the lock AFTER both layers — the content digest is what was GOT
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
        """Re-run EVERY tool's evidence in the shipped image and return one rich
        record per verification (label, tool, RAW check, engine_coupled, rc,
        passed, out). The contract (env_honesty.check_build) reads these to assert
        VALIDATED_IN_IMAGE — both the shape (on the RAW check) and the pass."""
        # Run each tool's evidence the way the shipped image is RUN on HPC: PLAIN
        # exec in the self-activating image (env baked onto PATH), NOT via `pixi run`
        # / `micromamba run`. This makes validated==shipped literal — a tool that
        # passes only under engine-run activation but is unreachable via `apptainer
        # exec image <tool>` is now correctly caught (the GAB python-on-PATH gap).
        finals = [(v, v["check"]) for v in self.verifications]
        res = self.cb.validate_in_image(image, [c for _, c in finals])
        records = []
        for v, run_cmd in finals:
            r = res["checks"].get(run_cmd, {})
            records.append({"label": v["label"], "tool": v.get("tool", ""),
                            "check": v["check"], "engine_coupled": v["engine_coupled"],
                            "rc": r.get("rc"), "passed": r.get("rc") == 0,
                            "out": r.get("out", "")})
        return {"success": res["success"], "verifications": records}

    # -- the content address ---------------------------------------------
    def content_digest(self) -> str:
        """sha256 over what was actually GOT: the engine lock + the long-tail
        commands + platform + engine. Identical requests → identical digest."""
        parts = {
            "lock": self.lock_text,
            "longtail": sorted(s["command"] for s in self.cb.longtail),
            "platform": self.platform,
            "engine": self.cb.engine.name if (self.conda_specs or self.pip_specs) else "none",
        }
        return _freeze.compute_content_digest(parts)

    # -- EnvCache bridge: "solve once, pull by digest" --------------------
    def request_key(self) -> str:
        """The cache LOOKUP handle — what was ASKED for (tools+platform+accel),
        order-independent. content_digest (what was GOT) is the real identity; this
        is just the handle freeze.request_key produces for the host path too, so
        the same env asked for either way collides in one cache."""
        tools: list[tuple[str, str]] = []
        for s in self.conda_specs + self.pip_specs:
            name, _, ver = s.replace("==", "=").partition("=")
            tools.append((name.strip(), ver.strip()))
        for spec in self.tools:
            tools.append((spec.get("tool", "") or spec.get("purpose", ""), ""))
        accel = (self.accelerator or {}).get("type", "none") if self.accelerator else "none"
        return _freeze.request_key(tools, self.platform, accel)

    def to_cache_record(self, result: dict) -> dict:
        """The artifact record stored in the EnvCache from a successful BuildResult.
        Container-native is always a recipe BUILD (we never adopt a foreign image);
        redistributable derives from the I13 firewall."""
        return {
            "request_key":     self.request_key(),
            "content_digest":  result["content_digest"],
            "mode":            "container-native",
            "image":           result["image"],
            "image_digest":    result["image_digest"],
            "platform":        result["platform"],
            "engine":          result.get("engine", "none"),
            "validation_locus": result.get("validation_locus", "unknown"),
            "gated":           self.license_gated,
            "redistributable": self.redistributable,
        }

    def build_or_cached(self, cache, image_present=None) -> dict[str, Any]:
        """Solve-once entry: an anchored cache hit (image still present) is returned
        without rebuilding; otherwise run() builds, and a successful+honest build is
        registered. `image_present` defaults to the docker-backed check (injectable
        for tests)."""
        if image_present is None:
            from agent.skills.container_build import image_present as _ip
            image_present = _ip
        key = self.request_key()
        hit = cache.lookup_anchored(key, image_present)
        if hit:
            return {"success": True, "cached": True, **hit}
        result = self.run()
        if result.get("success"):
            cache.register(key, self.to_cache_record(result))
            result["cached"] = False
        return result

    # -- run it all -------------------------------------------------------
    def run(self) -> dict[str, Any]:
        """Full core flow → a BuildResult. The honesty gate is env_honesty.check_build:
        the build is accepted (success=True) only if it satisfies the container-native
        Layer-1 contract — BUILT, VALIDATED_IN_IMAGE, POLICY_CLEAN."""
        try:
            b = self.build()
            if not b.get("success"):
                return b
            fr = self.freeze()
            if not fr.get("success"):
                return {"success": False, "stage": "freeze", **fr}
            digest = self.cb.image_digest(fr["image"])
            v = self.verify_in_image(fr["image"])
            # WHERE this build + its in-image validation ran. The VALIDATED_IN_IMAGE
            # pass/fail above is sound regardless (emulators are faithful), but I7
            # timings are authoritative only when native — so we stamp it, never
            # overclaiming. Cheap (one `docker version`); the emulator probe (a
            # container run) is left to a diagnostic/face, not the build hot path.
            locus = _locus.detect_locus(self.platform)
            result = {
                "name": self.name, "version": self.version, "platform": self.platform,
                "engine": self.cb.engine.name if (self.conda_specs or self.pip_specs) else "none",
                "image": fr["image"], "image_digest": digest,
                "content_digest": self.content_digest(),
                "conda_specs": list(self.conda_specs),
                "longtail_steps": [{"command": s["command"], "purpose": s["purpose"]}
                                   for s in self.cb.longtail],
                "verifications": v["verifications"],
                "validated_in_shipped_image": v["success"],
                "validation_locus": locus["locus"],
                "i7_authoritative": locus["i7_authoritative"],
                "locus_advisory": locus["advisory"],
                # the full resolved closure (for the env report's "along for the
                # ride" split) — read from the built env, captured while the build
                # container is still alive (close() is in the finally below).
                "resolved_packages": self.cb.resolved_packages(),
                # POLICY_CLEAN inputs
                "accelerator": self.accelerator,
                "license_gated": self.license_gated,
                "licenses": self.licenses,
                "redistributable": self.redistributable,
            }
            violations = _honesty.check_build(result)
            result["honesty_violations"] = violations
            result["success"] = not violations
            return result
        finally:
            self.cb.close()
