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
from agent.skills.outcomes import proven, broke


class EnvBuild:
    def __init__(self, name: str, version: str = "", *, platform: str = "linux/amd64",
                 base: str = BASE_IMAGE, engine: Optional[EnvEngine] = None,
                 channels: Optional[list[str]] = None,
                 accelerator: Optional[dict] = None, license_gated: bool = False,
                 licenses: Optional[list[str]] = None, redistributable: bool = True,
                 prebaked_lock_files: Optional[dict[str, str]] = None,
                 apt_snapshot: str = ""):
        self.name = name
        self.version = version
        self.platform = platform
        # apt_snapshot pins both the live build container's apt AND the emitted
        # Dockerfile's apt to snapshot.debian.org/<timestamp>. Threaded through
        # to ContainerBuild so it's used end-to-end.
        self.apt_snapshot = apt_snapshot
        self.cb = ContainerBuild(base=base, platform=platform, engine=engine,
                                 channels=channels, apt_snapshot=apt_snapshot)
        self.conda_specs: list[str] = []
        self.pip_specs: list[str] = []        # PyPI specs (engine --pypi, into the lock)
        self.tools: list[dict] = []           # long-tail generator specs
        self.verifications: list[dict] = []   # [{label, tool, check, engine_coupled}]
        self.lock_text = ""
        self.lock_files: dict[str, str] = {}  # engine lock artifacts {name: content}
        # If set, REPLAY from these instead of solving — the recipe-replay path.
        # The lock files (pixi.toml + pixi.lock for pixi; environment-lock.txt for
        # micromamba) were captured at a prior freeze and carried verbatim in the
        # recipe. Same lock → same env, byte-for-byte, across time and machines.
        self.prebaked_lock_files = prebaked_lock_files
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
            return broke("env_build.start_failed", **{**s, "success": False, "stage": "start"})
        # ENV LAYER — two paths:
        #   PREBAKED (recipe replay): write the captured lock files + materialize
        #     via `pixi install --locked` — NO solve. Identical env across time/
        #     machines; rebuild months later doesn't drift on newer bioconda builds.
        #   FRESH (first freeze): co-solve all conda + pip specs in one pass, then
        #     read the lock files back out for the recipe and the content digest.
        if self.prebaked_lock_files:
            d = self.cb.declare_locked(self.prebaked_lock_files)
            if not d.get("success"):
                return broke("env_build.declare_locked_failed", **{**d, "success": False, "stage": "declare_locked"})
        else:
            if self.conda_specs:
                d = self.cb.declare(self.conda_specs)
                if not d.get("success"):
                    return broke("env_build.declare_failed", **{**d, "success": False, "stage": "declare"})
            if self.pip_specs:
                dp = self.cb.declare_pypi(self.pip_specs)
                if not dp.get("success"):
                    return broke("env_build.declare_pypi_failed", **{**dp, "success": False, "stage": "declare_pypi"})
        if self.conda_specs or self.pip_specs or self.prebaked_lock_files:
            # capture lock per-file AFTER both layers — `lock_files` carries the
            # files individually so the recipe can ship them; `lock_text` is the
            # concatenation order-defined by engine.lock_artifacts() for the
            # content digest (unchanged from the pre-prebaked behavior).
            for art in self.cb.engine.lock_artifacts():
                cat = self.cb.exec(f"cat {self.cb.workdir}/{art} 2>/dev/null")
                content = cat.get("stdout", "")
                self.lock_files[art] = content
                self.lock_text += content
        for spec in self.tools:
            r = self.cb.install(spec)
            if not r.get("success"):
                return broke("env_build.install_failed", **{**r, "success": False, "stage": "install", "tool": spec.get("purpose")})
        return proven("env_build.built", success=True)

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
        # Banner probes — per-tool self-reported version capture from the shipped
        # binary. Tokens come from the structural `tool` field set by the install
        # primitive (not freeform purpose), and container_build sanitises each one
        # before synthesizing a probe command — non-fakeable by the agent.
        tools = list(dict.fromkeys(v.get("tool", "") for v in self.verifications
                                   if v.get("tool")))
        res = self.cb.validate_in_image(image, [c for _, c in finals], probe_tools=tools)
        banners = res.get("banners", {}) or {}
        records = []
        for v, run_cmd in finals:
            r = res["checks"].get(run_cmd, {})
            tool = v.get("tool", "")
            records.append({"label": v["label"], "tool": tool,
                            "check": v["check"], "engine_coupled": v["engine_coupled"],
                            "rc": r.get("rc"), "passed": r.get("rc") == 0,
                            "out": r.get("out", ""),
                            "banner": banners.get(tool, "")})
        if res["success"]:
            return proven("env_build.verified_in_image", success=True, verifications=records)
        return broke("env_build.verification_in_image_failed", success=False, verifications=records)

    # -- the content address ---------------------------------------------
    def content_digest(self) -> str:
        """sha256 over what was actually GOT: the engine lock + the long-tail
        commands + platform + engine + the digest-pinned BASE IMAGE. Identical
        requests → identical digest.

        `base` is the runtime FROM (BASE_IMAGE, pinned by @sha256) — the OS
        foundation of the artifact. Including it binds the base layer to the anchor
        (two builds on different bases no longer collide). NOT included: the apt
        runtime libs (system_packages) — `apt-get update` pulls fresh lists from the
        Debian mirror, so those versions can drift even from a pinned base and would
        make the digest non-reproducible (verify-by-rebuild would spuriously fail).
        They are captured in the SBOM (system_packages) as informational, and the
        report states plainly they are not version-pinned.

        UNLESS an apt_snapshot is pinned — then the apt layer IS reproducible (the
        snapshot URL serves byte-identical bytes), so the snapshot timestamp folds
        into the digest. When apt_snapshot is empty the digest is unchanged from
        the pre-Phase-2 computation (existing recipes still verify)."""
        parts = {
            "lock": self.lock_text,
            "longtail": sorted(s["command"] for s in self.cb.longtail),
            "platform": self.platform,
            "engine": self.cb.engine.name if (self.conda_specs or self.pip_specs) else "none",
            "base": self.cb.base,
        }
        if self.apt_snapshot:
            parts["apt_snapshot"] = self.apt_snapshot
        return _freeze.compute_content_digest(parts)

    # -- EnvCache bridge: "solve once, pull by digest" --------------------
    def request_key(self) -> str:
        """The cache LOOKUP handle — what was ASKED for (tools+platform+policy),
        order-independent. content_digest (what was GOT) is the real identity;
        this is just the handle freeze.request_key produces for the host path too,
        so the same env asked for either way collides in one cache.

        Includes all POLICY facets (gated, accelerator-policy hash, licenses
        hash) so a policy-distinct artifact doesn't share a slot with one that
        was built under different policy — the dorado-stress D5 finding."""
        tools: list[tuple[str, str]] = []
        for s in self.conda_specs + self.pip_specs:
            name, _, ver = s.replace("==", "=").partition("=")
            tools.append((name.strip(), ver.strip()))
        for spec in self.tools:
            tools.append((spec.get("tool", "") or spec.get("purpose", ""), ""))
        accel = (self.accelerator or {}).get("type", "none") if self.accelerator else "none"
        return _freeze.request_key(
            tools, self.platform, accel,
            gated=self.license_gated, accel_policy=self.accelerator,
            licenses=self.licenses,
        )

    # `to_cache_record` + `build_or_cached` lived here: a SECOND EnvCache record builder
    # and a second solve-once entry point, both with zero production callers (only tests).
    # Deleted (audit 2026-07-16) rather than repaired, because they were actively harmful
    # to keep:
    #   - `to_cache_record` OMITTED `verifications`, so it minted precisely the shape that
    #     fails today's contract — the `samtools` defect, encoded as a constructor. Any
    #     future path wired to it would have reintroduced the hole structurally.
    #   - it was the only producer of a third `mode` vocabulary ("container-native", vs
    #     freeze_record's "adopt"|"build"), which the audit flagged as a latent landmine
    #     across 5 consumers. The landmine's only producer was dead code; deleting it
    #     removes the divergence outright instead of "unifying" two spellings.
    # The live path is freeze() → env_freeze.build_env_image → EnvBuild.run(), which
    # assembles its record in freeze_tools and gates it on check_build there.

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
                return broke("env_build.freeze_failed", **{**fr, "success": False, "stage": "freeze"})
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
                "longtail_steps": [{"command": s["command"], "purpose": s["purpose"],
                                    "tool": s.get("tool", ""),
                                    **({"provenance": s["provenance"]} if s.get("provenance") else {})}
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
                # the OS/apt layer of the SBOM, read from the SHIPPED image — makes
                # the artifact fully self-describing (conda + pip + apt, versioned).
                "system_packages": self.cb.system_packages(fr["image"]),
                # PER-FILE lock artifacts (pixi.toml + pixi.lock for pixi). The
                # recipe carries these so a replay months later does NO solve —
                # identical env across time and machines. `lock` (above) is the
                # concatenation used for the content digest; `lock_files` is the
                # structured form the recipe + replay path consume.
                "lock_files": dict(self.lock_files),
                # The snapshot.debian.org timestamp pinning the apt layer. Empty
                # for pre-Phase-2 builds (apt floats). When set, the recipe carries
                # it forward so every replay resolves apt to the same bytes.
                "apt_snapshot": self.apt_snapshot,
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
