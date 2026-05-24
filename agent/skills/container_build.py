"""
Container-native build locus — the teardown pivot.

Install + validate INSIDE the ship-platform linux container, so the bytes we
validate are the bytes we ship. No host build, no cross-arch replay. This
collapses the per-tier recipe-replay zoo (`_emit_binary/jar/source/cargo/go/
perl_install`) into ONE generic "bake the recorded commands" path: a command
that ran in the build container is baked VERBATIM as a `RUN` — no translation.

Two phases:
  DECLARE     — accumulate ALL conda/pip specs (one co-solve via the EnvEngine) +
                record each long-tail command (binary/jar/source/…) as it runs.
  MATERIALIZE — emit a Dockerfile: the engine's env layer from the lock
                (reproducible) + one `RUN` per recorded long-tail command; build
                for the ship platform; validate in the freshly-built image
                (validated==shipped).

THE ENGINE IS A STRATEGY, NOT A MARRIAGE. The LOCUS (build-in-container +
verbatim long-tail bake) is engine-agnostic. HOW the conda/pip env is declared,
solved, locked, and invoked is an `EnvEngine` — pixi by default, micromamba+
explicit-lock as the conservative alternative, and an org could drop in conda-lock
or (later) nix. "We are the universal adapter" — one level down. A single-platform
explicit lock is fully reproducible (we always target one ship platform), so
reproducibility does NOT depend on any one engine.

Host-agnostic by construction: everything runs in a linux/{arch} container via
buildx — qemu on a non-linux/amd64 host (e.g. Apple Silicon), native on linux-x86
/CI — SAME artifact on any host. Docker (+buildx) is the only host requirement.

`emit_dockerfile` + the engines' line-emitters are pure (unit-testable);
`ContainerBuild` drives a real build container and is exercised by live verification.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from typing import Any, Optional

# Base apt for the build/ship image: curl+certs for the engine installer and any
# long-tail download; the common archive + C-build tools + bioinformatics dev libs
# so a long-tail/half-baked source command (tar/zip extract, `make`, custom gcc)
# just works when baked.
#
# Multi-stage (Phase D): the BUILD stage gets the full toolchain; the RUNTIME stage
# gets only the shared-library RUNTIME packages (the *.so a source/binary tool links
# against) — build-essential/git/-dev headers are dropped from the shipped image.
# The conda env bundles its OWN deps, so conda tools need nothing here; this set
# covers the source/binary tiers compiled against the system libs in the build stage.
# `time` ships in the RUNTIME set so run_step_in_container's GNU `time -v` path
# works IN the shipped image (exact peak RSS for I7); without it, sub-second tools
# fall back to a single docker-stats sample that reads 0.
_RUNTIME_APT = "ca-certificates procps time zlib1g libbz2-1.0 liblzma5 libcurl4 libssl3"
_BUILD_APT = (_RUNTIME_APT + " curl tar gzip bzip2 xz-utils unzip "
              "build-essential git zlib1g-dev libbz2-dev liblzma-dev "
              "libcurl4-openssl-dev libssl-dev")
_BASE_APT = _BUILD_APT  # back-compat: ContainerBuild.start() provisions the build toolchain

# docker platform → conda subdir token (for engines that need it in a URL).
_PLATFORM_SUBDIR = {"linux/amd64": "linux-64", "linux/arm64": "linux-aarch64",
                    "linux/arm64/v8": "linux-aarch64"}


def _docker_repo(name: str) -> str:
    """Sanitize an image repository name to docker's rules: lowercase, and only
    [a-z0-9._-] (a pipeline name like 'VEP_annotate' is otherwise rejected)."""
    return re.sub(r"[^a-z0-9._-]+", "-", name.lower()).strip("-._") or "bioinf"


def image_present(ref: str) -> bool:
    """Is `ref` (a tag or sha256:… digest) present in the local docker daemon?
    The real backing for EnvCache.lookup_anchored — a cache hit is only honest if
    the image it points at still exists to be shipped."""
    if not ref:
        return False
    p = subprocess.run(["docker", "image", "inspect", ref],
                       capture_output=True, text=True, timeout=60)
    return p.returncode == 0


# ---------------------------------------------------------------------------
# EnvEngine strategies — the swappable conda/pip solve+lock+invoke layer.
# ---------------------------------------------------------------------------

class EnvEngine:
    """Strategy for the conda/pip layer. Subclasses implement: how to install the
    engine (in the build container AND the ship Dockerfile), how to declare+co-
    solve+lock, how to materialize from the lock, and how to invoke a tool. The
    locus (ContainerBuild) is written ONLY against this interface."""
    name = "base"
    workdir = "/work"

    def exec_env(self) -> str:                       # shell prefix so the engine is on PATH in exec
        raise NotImplementedError
    def install_commands(self) -> str:              # shell to install the engine in the build container
        raise NotImplementedError
    def setup(self, cb: "ContainerBuild") -> dict:  # init the project/env
        raise NotImplementedError
    def add(self, cb: "ContainerBuild", specs: list[str], channels: list[str]) -> dict:  # ONE co-solve + lock
        raise NotImplementedError
    def add_pypi(self, cb: "ContainerBuild", specs: list[str]) -> dict:  # PyPI specs into the env+lock
        raise NotImplementedError
    def run(self, tool_cmd: str) -> str:            # wrap a conda-env tool invocation
        raise NotImplementedError
    def bootstrap_lines(self) -> list[str]:         # Dockerfile (builder): install the engine
        raise NotImplementedError
    def materialize_lines(self) -> list[str]:       # Dockerfile (builder): build env from the lock
        raise NotImplementedError
    def runtime_lines(self) -> list[str]:           # Dockerfile (runtime): COPY the env from builder
        raise NotImplementedError
    def lock_artifacts(self) -> list[str]:          # files to copy out of the build container
        raise NotImplementedError


class PixiEngine(EnvEngine):
    """Default. pixi.toml + pixi.lock (multi-platform, conda+PyPI in one). Solves
    cross-platform; `pixi run` invokes without activation."""
    name = "pixi"
    _BIN = "/root/.pixi/bin"

    def __init__(self, platform: str = "linux/amd64"):
        self.platform = platform

    def exec_env(self) -> str:
        return f'export PATH="{self._BIN}:$PATH"'
    def install_commands(self) -> str:
        return "curl -fsSL https://pixi.sh/install.sh | bash"
    def setup(self, cb):
        chans = " ".join(f"-c {c}" for c in cb.channels)
        r = cb.exec(f"pixi init {self.workdir} {chans}", timeout=120)
        return {"success": r["returncode"] == 0, "stderr": r["stderr"][-400:]}
    def add(self, cb, specs, channels):
        quoted = " ".join(f'"{s}"' for s in specs)
        r = cb.exec(f"pixi add {quoted}", timeout=1800)
        return {"success": r["returncode"] == 0, "stderr": (r["stderr"] or "")[-800:]}
    def add_pypi(self, cb, specs):
        # PyPI specs land in pixi.toml/pixi.lock and materialize via the SAME
        # `pixi install --locked` (no extra Dockerfile step) — in-lock, reproducible.
        quoted = " ".join(f'"{s}"' for s in specs)
        r = cb.exec(f"pixi add --pypi {quoted}", timeout=1800)
        return {"success": r["returncode"] == 0, "stderr": (r["stderr"] or "")[-800:]}
    def run(self, tool_cmd):
        # wrap in a shell so builtins/pipes/env-prefixes in tool_cmd work (pixi run
        # execs directly otherwise — `pixi run command -v X` would fail).
        return f"pixi run bash -c {shlex.quote(tool_cmd)}"
    def bootstrap_lines(self):
        return [f"RUN {self.install_commands()}", f'ENV PATH="{self._BIN}:$PATH"', ""]
    def materialize_lines(self):
        return [f"WORKDIR {self.workdir}", "COPY pixi.toml pixi.lock ./",
                "RUN pixi install --locked", ""]
    def env_prefix(self) -> str:
        return f"{self.workdir}/.pixi/envs/default"   # pixi's default-env conda prefix
    def runtime_lines(self):
        # COPY the pixi launcher + the project env from the builder at the SAME paths
        # (pixi/conda prefixes are path-baked, so identical paths keep them valid —
        # no relocation). The build toolchain stays behind in the builder stage.
        #
        # SELF-ACTIVATING: bake the solved env's bin on PATH (+ CONDA_PREFIX) so the
        # shipped image runs the way HPC runs it — `apptainer exec image <tool>` /
        # plain `docker run image <tool>` reach the conda tools AND python directly,
        # NOT only via `pixi run`. Without this the env is invisible to apptainer
        # exec (every conda tool 404s) and a run-by-path wrapper's `python` breaks.
        ep = self.env_prefix()
        return [f"COPY --from=builder {self._BIN.rsplit('/bin', 1)[0]} {self._BIN.rsplit('/bin', 1)[0]}",
                f"COPY --from=builder {self.workdir} {self.workdir}",
                f'ENV PATH="{ep}/bin:{self._BIN}:$PATH"',
                f'ENV CONDA_PREFIX="{ep}"', ""]
    def lock_artifacts(self):
        return ["pixi.toml", "pixi.lock"]


class MicromambaEngine(EnvEngine):
    """Conservative alternative. environment.yml → one solve → an EXPLICIT lock
    (`micromamba env export --explicit`: URLs+sha256, bit-reproducible for the
    ship platform). `micromamba run -n env` invokes. Proves the locus is engine-
    agnostic and that reproducibility doesn't require pixi."""
    name = "micromamba"
    _ROOT = "/opt/micromamba"

    def __init__(self, platform: str = "linux/amd64"):
        self.platform = platform
        self.subdir = _PLATFORM_SUBDIR.get(platform, "linux-64")

    def exec_env(self) -> str:
        return f'export MAMBA_ROOT_PREFIX={self._ROOT}; export PATH="/usr/local/bin:$PATH"'
    def install_commands(self) -> str:
        # static micromamba binary for the ship arch → /usr/local/bin
        return (f"curl -Ls https://micro.mamba.pm/api/micromamba/{self.subdir}/latest "
                f"| tar -xj -C /usr/local bin/micromamba")
    def setup(self, cb):
        return {"success": True}                     # env.yml is written at add()
    def add(self, cb, specs, channels):
        chans = "\n".join(f"  - {c}" for c in channels)
        deps = "\n".join(f"  - {s}" for s in specs)
        yml = f"name: env\nchannels:\n{chans}\ndependencies:\n{deps}\n"
        # write environment.yml, solve into a named env, then EXPORT an explicit lock
        cb.exec(f"mkdir -p {self.workdir}", timeout=60)
        w = cb.exec(f"cat > {self.workdir}/environment.yml <<'YML'\n{yml}YML", timeout=60)
        if w["returncode"] != 0:
            return {"success": False, "stage": "write_yml", "stderr": w["stderr"][-400:]}
        s = cb.exec(f"micromamba create -y -n env -f {self.workdir}/environment.yml", timeout=1800)
        if s["returncode"] != 0:
            return {"success": False, "stage": "solve", "stderr": (s["stderr"] or "")[-800:]}
        e = cb.exec(f"micromamba env export -n env --explicit > {self.workdir}/env.lock", timeout=120)
        return {"success": e["returncode"] == 0, "stderr": (e["stderr"] or "")[-400:]}
    def add_pypi(self, cb, specs):
        # micromamba's explicit lock (URLs+sha256) can't capture PyPI, so a pip
        # install here would NOT replay in the materialized image — refuse honestly
        # rather than silently drop it. PyPI specs ⇒ use the pixi engine (default).
        return {"success": False, "reason": "PyPI specs are not supported by the micromamba "
                "engine (its explicit lock can't capture pip, so they wouldn't materialize in "
                "the shipped image) — use the pixi engine (the default) for PyPI."}
    def run(self, tool_cmd):
        return f"micromamba run -n env bash -c {shlex.quote(tool_cmd)}"
    def bootstrap_lines(self):
        return [f"RUN {self.install_commands()}", f'ENV MAMBA_ROOT_PREFIX={self._ROOT}', ""]
    def materialize_lines(self):
        return [f"WORKDIR {self.workdir}", "COPY env.lock ./",
                "RUN micromamba create -y -n env --file env.lock && micromamba clean -afy", ""]
    def env_prefix(self) -> str:
        return f"{self._ROOT}/envs/env"   # the named env's conda prefix
    def runtime_lines(self):
        # COPY the named env from the builder at the SAME root prefix (paths are
        # baked into conda prefixes); the micromamba binary rides the generic
        # /usr/local COPY. Build toolchain stays in the builder. SELF-ACTIVATING:
        # the env bin on PATH (+ CONDA_PREFIX) so plain `apptainer exec image <tool>`
        # reaches the conda tools — not only via `micromamba run`.
        ep = self.env_prefix()
        return [f"COPY --from=builder {self._ROOT} {self._ROOT}",
                f'ENV MAMBA_ROOT_PREFIX={self._ROOT}',
                f'ENV PATH="{ep}/bin:$PATH"',
                f'ENV CONDA_PREFIX="{ep}"', ""]
    def lock_artifacts(self):
        return ["environment.yml", "env.lock"]


def emit_dockerfile(
    base: str,
    *,
    engine: EnvEngine,
    has_env_layer: bool,
    longtail_steps: list[dict],
    apt_extra: str = "",
) -> str:
    """Assemble the ship-image Dockerfile from a recorded build (pure — no docker).

    Multi-stage (Phase D): a `builder` stage carries the full toolchain + engine +
    long-tail builds; the shipped RUNTIME stage starts slim and COPYs only the engine
    env (at identical paths, so conda/pixi prefixes stay valid) + /usr/local + /opt/
    tools from the builder, with just the *.so RUNTIME apt (plus a JRE when a jar tool
    is present). build-essential / -dev headers / git / the engine installer never
    ship. Engine-specific lines come from `engine`; long-tail commands are baked
    VERBATIM (the exact commands that ran + validated in the build container)."""
    build_apt = _BUILD_APT + (f" {apt_extra}" if apt_extra.strip() else "")
    runtime_apt = _RUNTIME_APT
    if any(str(s.get("purpose", "")).endswith("(java jar)") for s in longtail_steps):
        runtime_apt += " default-jre-headless"   # jar wrappers need a JRE at runtime

    def _labels():
        out = ['LABEL build_method="container-native"']
        if has_env_layer:                        # engine present ONLY if conda/pip specs declared
            out.append(f'LABEL env_engine="{engine.name}"')
        return out

    def _apt(pkgs):
        return ["RUN apt-get update && apt-get install -y --no-install-recommends \\",
                f"        {pkgs} \\", "    && rm -rf /var/lib/apt/lists/*", ""]

    # ---- builder stage: full toolchain + engine + long-tail builds ----
    lines = ["# Auto-generated by bioinf_agent — container-native build (validated==shipped)",
             "# Multi-stage: builder (full toolchain) -> slim runtime (env + artifacts only).",
             f"FROM {base} AS builder", *_labels(), ""]
    lines += _apt(build_apt)
    lines += ["RUN mkdir -p /opt/tools", ""]     # so the runtime COPY of /opt/tools always resolves
    if has_env_layer:
        lines += engine.bootstrap_lines()
        lines += engine.materialize_lines()
    for step in longtail_steps:
        if step.get("purpose"):
            lines.append(f"# {step['purpose']}")
        lines += [f"RUN {step['command']}", ""]

    # ---- runtime stage (shipped): slim base + runtime libs + COPY artifacts ----
    lines += ["# ---- runtime image (shipped) ----", f"FROM {base}", *_labels(), ""]
    lines += _apt(runtime_apt)
    if has_env_layer:
        lines += engine.runtime_lines()
    lines += ["COPY --from=builder /usr/local /usr/local",
              "COPY --from=builder /opt/tools /opt/tools", "",
              "WORKDIR /data", 'CMD ["/bin/bash"]', ""]
    return "\n".join(lines)


class ContainerBuild:
    """A live linux build container that records what it installs, then freezes to
    a ship image. Written ONLY against EnvEngine — swap the engine, same locus.

    Usage:
        cb = ContainerBuild(platform="linux/amd64", engine=PixiEngine("linux/amd64"))
        cb.start(); cb.declare(["samtools=1.21"])
        cb.run("curl ... seqkit ...", evidence="seqkit version", purpose="seqkit binary")
        fr = cb.freeze("demo", "1.0"); cb.validate_in_image(fr["image"], [...]); cb.close()
    """

    def __init__(self, base: str = "debian:bookworm-slim",
                 platform: str = "linux/amd64",
                 engine: Optional[EnvEngine] = None,
                 channels: Optional[list[str]] = None,
                 workdir: str = "/work"):
        self.base = base
        self.platform = platform
        self.engine = engine or PixiEngine(platform)
        self.channels = channels or ["conda-forge", "bioconda"]
        self.workdir = workdir
        self.cid: Optional[str] = None
        self.has_env_layer = False
        self._engine_installed = False
        self.longtail: list[dict] = []
        self.log: list[str] = []

    @staticmethod
    def _sh(args: list[str], timeout: int = 1800) -> dict[str, Any]:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}

    def exec(self, command: str, timeout: int = 1800) -> dict[str, Any]:
        """Run a shell command in the build container (engine on PATH)."""
        assert self.cid, "call start() first"
        full = f'{self.engine.exec_env()}; cd {self.workdir} 2>/dev/null || true; {command}'
        return self._sh(["docker", "exec", self.cid, "bash", "-c", full], timeout=timeout)

    def start(self) -> dict[str, Any]:
        """Launch the build container for the ship platform + install the base
        toolchain. The conda/pip ENGINE is installed lazily on the first declare()
        — a pure binary/source/half-baked env carries no pixi/micromamba at all."""
        r = self._sh(["docker", "run", "-d", "--platform", self.platform,
                      self.base, "sleep", "infinity"], timeout=300)
        if r["returncode"] != 0:
            return {"success": False, "stage": "run", "stderr": r["stderr"][-800:]}
        self.cid = (r["stdout"] or "").strip()
        setup = f"apt-get -qq update && apt-get -qq install -y --no-install-recommends {_BASE_APT} >/dev/null 2>&1"
        s = self._sh(["docker", "exec", self.cid, "bash", "-c", setup], timeout=900)
        self.log.append(f"start rc={s['returncode']}")
        return {"success": s["returncode"] == 0, "container": self.cid,
                "stderr": s["stderr"][-800:] if s["returncode"] else ""}

    # -- DECLARE: conda/pip (one co-solve via the engine) ------------------
    def declare(self, specs: list[str], timeout: int = 1800) -> dict[str, Any]:
        """Add ALL conda/pip specs in ONE call → one co-solve → a lock. (Never one
        at a time — sequential solves are order-dependent and can downgrade.) The
        engine is installed lazily here on first use."""
        if not specs:
            return {"success": True, "skipped": "no specs"}
        if not self._engine_installed:
            inst = self.exec(self.engine.install_commands(), timeout=900)
            if inst["returncode"] != 0:
                return {"success": False, "stage": "engine_install",
                        "stderr": (inst["stderr"] or "")[-800:]}
            es = self.engine.setup(self)
            if not es.get("success", True):
                return {"success": False, "stage": "engine_setup", "stderr": es.get("stderr", "")}
            self._engine_installed = True
        res = self.engine.add(self, specs, self.channels)
        self.log.append(f"declare {specs} -> {res.get('success')}")
        if res.get("success"):
            self.has_env_layer = True
        return res

    # -- DECLARE: PyPI specs (co-solved into the env+lock by the engine) ----
    def declare_pypi(self, specs: list[str], timeout: int = 1800) -> dict[str, Any]:
        """Add PyPI specs via the engine (pixi add --pypi → pixi.lock, materializes
        with the conda layer). The engine is installed lazily here on first use, so a
        pip-only env still gets one. Refused by engines whose lock can't hold pip."""
        if not specs:
            return {"success": True, "skipped": "no specs"}
        if not self._engine_installed:
            inst = self.exec(self.engine.install_commands(), timeout=900)
            if inst["returncode"] != 0:
                return {"success": False, "stage": "engine_install", "stderr": (inst["stderr"] or "")[-800:]}
            es = self.engine.setup(self)
            if not es.get("success", True):
                return {"success": False, "stage": "engine_setup", "stderr": es.get("stderr", "")}
            self._engine_installed = True
        res = self.engine.add_pypi(self, specs)
        self.log.append(f"declare_pypi {specs} -> {res.get('success')}")
        if res.get("success"):
            self.has_env_layer = True
        return res

    # -- DECLARE: an authored file (patch / config / wrapper) --------------
    def write_file(self, path: str, content: str, *, mode: str = "",
                   purpose: str = "") -> dict[str, Any]:
        """Capture an agent-authored file (a patched Makefile, a config, a wrapper
        script) INTO the build — written now in the container AND recorded as a
        base64 RUN so freeze bakes its exact bytes. Replaces stage_authored_artifact
        + I9 hashing: the content lives in the recorded build, so there is no host
        orphan to trace (the container is the captured state)."""
        import base64
        import shlex as _shlex
        b64 = base64.b64encode(content.encode()).decode()
        q = _shlex.quote(path)
        cmd = f'mkdir -p "$(dirname {q})"; echo {b64} | base64 -d > {q}'
        if mode:
            cmd += f"; chmod {mode} {q}"
        r = self.exec(cmd, timeout=120)
        if r["returncode"] != 0:
            return {"success": False, "stderr": (r["stderr"] or "")[-400:]}
        self.longtail.append({"command": cmd, "purpose": purpose or f"authored file {path}",
                              "evidence": f"test -f {q}"})
        self.log.append(f"write_file {path} ({len(content)}B)")
        return {"success": True, "path": path}

    # -- DECLARE: long-tail command (binary/jar/source/cargo/go/perl) ------
    def run(self, command: str, evidence: str, purpose: str = "",
            engine_coupled: bool = False, timeout: int = 1800) -> dict[str, Any]:
        """Run a long-tail install command, then PROVE it with `evidence` (exit 0),
        both in the build container. On success the command is recorded for verbatim
        baking; `evidence` is re-run in the built image at freeze.

        engine_coupled: the command (and evidence) need the engine env active — the
        BUILD uses an engine-provided toolchain (rust/go/perl) or the artifact lives
        in the engine env. Wrapped with engine.run() so it's correct both in the
        build container AND when baked (the engine layer is in the image)."""
        cmd = self.engine.run(command) if engine_coupled else command
        ev_cmd = self.engine.run(evidence) if engine_coupled else evidence
        inst = self.exec(cmd, timeout=timeout)
        if inst["returncode"] != 0:
            return {"success": False, "stage": "install", "stderr": (inst["stderr"] or "")[-800:]}
        ev = self.exec(ev_cmd, timeout=120)
        if ev["returncode"] != 0:
            return {"success": False, "stage": "evidence", "evidence": ev_cmd,
                    "stderr": (ev["stderr"] or "")[-800:]}
        self.longtail.append({"command": cmd, "purpose": purpose, "evidence": ev_cmd})
        self.log.append(f"run [{purpose}] rc=0 ev_ok coupled={engine_coupled}")
        return {"success": True, "evidence_output": (ev["stdout"] or "").strip()[:200]}

    def install(self, spec: dict, timeout: int = 1800) -> dict[str, Any]:
        """Run an install_commands generator's spec ({command, evidence, purpose,
        engine_coupled?}). The single entry point for every long-tail tier — the
        generator carries the per-tier knowledge; the locus just runs+bakes it."""
        return self.run(spec["command"], spec["evidence"], spec.get("purpose", ""),
                        engine_coupled=spec.get("engine_coupled", False), timeout=timeout)

    def run_tool(self, tool_cmd: str, timeout: int = 300) -> dict[str, Any]:
        """Invoke a conda-env tool via the engine's run wrapper (pixi run / micromamba run)."""
        return self.exec(self.engine.run(tool_cmd), timeout=timeout)

    # -- MATERIALIZE -------------------------------------------------------
    def freeze(self, name: str, version: str = "", *, output_dir: str = "/tmp") -> dict[str, Any]:
        """Emit the Dockerfile from the recorded build, build it for the ship
        platform, and return the image tag. The env layer's lock artifacts are
        copied from the build container into the build context."""
        from pathlib import Path
        assert self.cid, "call start() first"
        name = _docker_repo(name)
        build_dir = Path(output_dir) / f"cbuild_{name}"
        build_dir.mkdir(parents=True, exist_ok=True)
        if self.has_env_layer:
            for f in self.engine.lock_artifacts():
                cp = self._sh(["docker", "cp", f"{self.cid}:{self.workdir}/{f}", str(build_dir / f)])
                if cp["returncode"] != 0:
                    return {"success": False, "stage": "cp", "file": f, "stderr": cp["stderr"][-400:]}
        dockerfile = emit_dockerfile(self.base, engine=self.engine,
                                     has_env_layer=self.has_env_layer, longtail_steps=self.longtail)
        (build_dir / "Dockerfile").write_text(dockerfile)
        tag = f"{name}:{version}" if version else f"{name}:latest"
        b = self._sh(["docker", "buildx", "build", "--platform", self.platform,
                      "--load", "-t", tag, str(build_dir)], timeout=2400)
        if b["returncode"] != 0:
            return {"success": False, "stage": "build", "dockerfile": str(build_dir / "Dockerfile"),
                    "stderr": (b["stderr"] or "")[-1200:]}
        return {"success": True, "image": tag, "engine": self.engine.name,
                "dockerfile": str(build_dir / "Dockerfile"),
                "build_method": "container-native", "platform": self.platform}

    def validate_in_image(self, image: str, checks: list[str]) -> dict[str, Any]:
        """Re-run checks in the BUILT image — proves validated==shipped. Runs each
        check PLAIN (no engine activation prefix): the runtime image is self-
        activating (env baked onto PATH), so this is byte-for-byte how `apptainer
        exec image <check>` runs it on HPC. (Activating here would mask a tool that
        the deployment contract can't actually reach.)"""
        results, ok = {}, True
        for c in checks:
            r = self._sh(["docker", "run", "--rm", "--platform", self.platform, image, "bash", "-c",
                          f'cd {self.workdir} 2>/dev/null || true; {c}'],
                         timeout=300)
            results[c] = {"rc": r["returncode"], "out": (r["stdout"] or "").strip()[:200]}
            ok = ok and r["returncode"] == 0
        return {"success": ok, "checks": results}

    def image_digest(self, image: str) -> str:
        """The built image's content id (sha256), the local shipping handle."""
        r = self._sh(["docker", "image", "inspect", "--format", "{{index .Id}}", image])
        return (r["stdout"] or "").strip() if r["returncode"] == 0 else ""

    def close(self) -> None:
        if self.cid:
            self._sh(["docker", "rm", "-f", self.cid])
            self.cid = None
