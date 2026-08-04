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

from agent.skills.outcomes import proven, refused, broke

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
# A generator-declared runtime apt package name. `runtime_packages` reaches the
# RUNTIME stage's apt line, which is bare-interpolated into a Dockerfile RUN — so a
# value like `x && curl y | sh` would be baked into the shipped image. Same posture
# as `_SAFE_TOOL` below: a token that is not a plain Debian package name is DROPPED,
# never shell-escaped-and-run. Debian policy: lowercase alnum start, then alnum . + -
_SAFE_APT_PKG = re.compile(r"^[a-z0-9][a-z0-9.+-]*$")
# `jq` is in the BUILD set (not RUNTIME) because the SWH-fallback helper that
# the source-tier installs call uses jq to parse SWH's vault API JSON. Tiny
# (~1MB) and only present in the BUILDER stage — never ships.
_BUILD_APT = (_RUNTIME_APT + " curl tar gzip bzip2 xz-utils unzip jq "
              "build-essential git zlib1g-dev libbz2-dev liblzma-dev "
              "libcurl4-openssl-dev libssl-dev")
_BASE_APT = _BUILD_APT  # back-compat: ContainerBuild.start() provisions the build toolchain

# A fixed UTC timestamp identifying a snapshot.debian.org archive (e.g.
# "20260526T200000Z" → https://snapshot.debian.org/archive/debian/20260526T200000Z/).
# snapshot.debian.org is Debian's official append-only mirror of the apt archive
# AS IT EXISTED at any past timestamp. When we point apt-get at it, `apt-get
# install procps libssl3 ...` resolves to the EXACT versions that existed at the
# captured moment — same bytes across time/machines, no `apt-get update` drift.
# We use HTTP (not HTTPS): apt verifies packages by GPG signatures embedded in
# the signed Release files, so HTTP transport doesn't weaken authenticity. Old
# snapshots have expired Valid-Until fields → `Acquire::Check-Valid-Until=false`
# tells apt to accept them anyway (the GPG signature itself is still valid).
def _snapshot_sources_list(snapshot: str) -> str:
    base = f"http://snapshot.debian.org/archive/debian/{snapshot}"
    sec = f"http://snapshot.debian.org/archive/debian-security/{snapshot}"
    return (f"deb {base}/ bookworm main\n"
            f"deb {sec}/ bookworm-security main\n")


# Software Heritage link-rot fallback for source-tier git clones. SWH (funded by
# INRIA + UNESCO) continuously archives GitHub/GitLab/etc.; a commit SHA that
# resolves to the original repo is also resolvable via SWH's vault API. The
# helper tries upstream first (fast path, zero overhead); only on failure does it
# fall back to SWH's git_bare vault, which bakes a bare git repo on demand for a
# specific commit. The final `git checkout <commit>` proves the bytes are right
# regardless of source: SWH can't lie about which bytes the commit SHA names.
# The script gets installed once during the apt setup step (live build container
# AND emitted Dockerfile builder stage) so install_commands.source/script_repo
# can call `_swh_clone` instead of a bare `git clone`.
_SWH_CLONE_SCRIPT = r"""#!/bin/sh
# _swh_clone <url> <commit> <dst>
# Clone <url> into <dst>; if upstream is unreachable, fetch a bare git repo for
# <commit> from Software Heritage and clone from that. Idempotent. Exits 0 on
# success, nonzero on both upstream-AND-SWH failure (loud).
set -eu
url="$1"; commit="$2"; dst="$3"
if git clone "$url" "$dst" 2>/dev/null; then
    exit 0
fi
echo "[swh] upstream clone failed: $url -- falling back to Software Heritage" >&2
api="https://archive.softwareheritage.org/api/1/vault/git_bare/swh:1:rev:${commit}/"
status=""; fetch=""; i=0
while [ $i -lt 60 ]; do
    resp="$(curl -fsSL -X POST "$api" 2>/dev/null || curl -fsSL "$api" 2>/dev/null || true)"
    [ -z "$resp" ] && { sleep 5; i=$((i+1)); continue; }
    status="$(echo "$resp" | jq -r '.status // empty')"
    case "$status" in
        done)    fetch="$(echo "$resp" | jq -r '.fetch_url')"; break ;;
        failed)  echo "[swh] cooking failed for commit $commit" >&2; exit 2 ;;
        new|pending) sleep 10 ;;
        *) echo "[swh] unexpected status='$status': $resp" >&2; sleep 10 ;;
    esac
    i=$((i+1))
done
[ "$status" = "done" ] || { echo "[swh] timeout waiting for $commit" >&2; exit 2; }
tmp="$(mktemp -d)"
curl -fsSL "$fetch" -o "$tmp/bare.tar"
mkdir -p "$(dirname "$dst")"
git clone "$tmp/bare.tar" "$dst"
rm -rf "$tmp"
"""


def _swh_clone_install_lines() -> list[str]:
    """Dockerfile RUN lines that install /usr/local/bin/_swh_clone. base64-encoded
    so the multi-line script survives the single-RUN-line constraint. Idempotent;
    safe to include even when no source-tier installs are present (tiny overhead)."""
    import base64
    b64 = base64.b64encode(_SWH_CLONE_SCRIPT.encode()).decode()
    return [f"RUN echo {b64} | base64 -d > /usr/local/bin/_swh_clone \\",
            "    && chmod +x /usr/local/bin/_swh_clone", ""]


# The build/ship base, PINNED BY DIGEST for reproducibility. The
# `debian:bookworm-slim` tag moves with security updates, so a bare tag makes a
# rebuild non-reproducible at the OS layer. Pinning the multi-arch *index* digest
# freezes the base bytes while `--platform` still resolves the per-arch sub-image
# deterministically — so the conda/pip lock (env layer) + sha256-anchored binaries
# + THIS pin together make the whole stack reproducible-by-rebuild, except the apt
# packages installed on top (still floating — a separate follow-up). Bump this
# DELIBERATELY to take base security updates.
BASE_IMAGE = ("debian:bookworm-slim@sha256:"
              "0104b334637a5f19aa9c983a91b54c89887c0984081f2068983107a6f6c21eeb")

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


def registry_manifest_digest(ref: str) -> str:
    """The PULLABLE manifest digest of an image we obtained from a registry —
    `sha256:…` as it appears in `<repo>@sha256:…`. "" when the image isn't local or
    carries no repo digest (e.g. it was built here, never pushed).

    THIS IS NOT `.Id`, and the difference is a real bug we shipped. `.Id` is the
    daemon's local content id; under the classic overlay2 store it is the image's
    *config blob* digest, which is NOT what anyone can `docker pull`. It happens to
    equal the manifest digest under the containerd snapshotter — which is what this
    project's dev Mac runs, so `verify_env_recipe`'s adopt branch compared `.Id`
    against a recorded manifest digest and PASSED, locally, by luck. On a normal
    overlay2 daemon (most Linux, most CI, most users) it reported "recipe not
    reproduced" for every adopt recipe ever written (audit 2026-07-16 §14/Tier 6).

    So `image_digest` and this are not two copies of one concept to be unified —
    they answer two different questions and both are needed:
        image_digest(x)             → "what are these bytes, locally?"  (we BUILT it)
        registry_manifest_digest(x) → "what would anyone else pull?"    (we ADOPTED it)
    Conflating them under one name is the same disease as duplicating a definition:
    one name, two meanings, and the wrong one silently in the load path."""
    if not ref:
        return ""
    p = subprocess.run(
        ["docker", "image", "inspect", "--format",
         "{{range .RepoDigests}}{{println .}}{{end}}", ref],
        capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        return ""
    repo = ref.split("@", 1)[0].split(":")[0] if "@" in ref else ref.split(":")[0]
    digests = [ln.strip() for ln in (p.stdout or "").splitlines() if "@sha256:" in ln]
    # Prefer the entry for the repo we asked about; an image can carry repo digests
    # for several repos (same bytes, mirrored), and picking another repo's digest
    # would be a different pullable reference.
    for d in digests:
        if d.split("@", 1)[0] == repo:
            return d.split("@", 1)[1]
    return digests[0].split("@", 1)[1] if digests else ""


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
    def install_from_lock(self, cb: "ContainerBuild", lock_files: dict[str, str]) -> dict:
        # Install from PREBAKED lock files (recipe-replay path). Each engine writes
        # its own lock files to the workdir and runs its lock-aware install. Default:
        # not supported (return an error rather than silently re-solving).
        return refused("container_build.install_from_lock_unsupported", success=False, stage="install_from_lock",
                stderr=f"engine {self.name!r} does not implement install_from_lock")
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
        if r["returncode"] == 0:
            return proven("container_build.pixi_setup_ok", success=True, stderr=r["stderr"][-400:])
        return broke("container_build.pixi_setup_failed", success=False, stderr=r["stderr"][-400:])
    def add(self, cb, specs, channels):
        quoted = " ".join(f'"{s}"' for s in specs)
        r = cb.exec(f"pixi add {quoted}", timeout=1800)
        if r["returncode"] == 0:
            return proven("container_build.pixi_add_ok", success=True, stderr=(r["stderr"] or "")[-800:])
        return broke("container_build.pixi_add_failed", success=False, stderr=(r["stderr"] or "")[-800:])
    def add_pypi(self, cb, specs):
        # PyPI specs land in pixi.toml/pixi.lock and materialize via the SAME
        # `pixi install --locked` (no extra Dockerfile step) — in-lock, reproducible.
        quoted = " ".join(f'"{s}"' for s in specs)
        r = cb.exec(f"pixi add --pypi {quoted}", timeout=1800)
        if r["returncode"] == 0:
            return proven("container_build.pixi_add_pypi_ok", success=True, stderr=(r["stderr"] or "")[-800:])
        return broke("container_build.pixi_add_pypi_failed", success=False, stderr=(r["stderr"] or "")[-800:])
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
    def install_from_lock(self, cb, lock_files: dict[str, str]) -> dict[str, Any]:
        """Install the env from a PREBAKED pixi.toml + pixi.lock — NO solve. The
        recipe carries these files so a rebuild months / years later picks up the
        IDENTICAL package set, not whatever bioconda happens to resolve today. The
        URL+sha256 in pixi.lock makes every package byte-exact (or `pixi install`
        fails loud — no silent substitution). The lock files come from a prior
        freeze of the same recipe (env_build captured them as `lock_files`)."""
        import base64
        for name, content in lock_files.items():
            b64 = base64.b64encode(content.encode()).decode()
            r = cb.exec(f"mkdir -p {self.workdir} && echo {b64} | base64 -d > {self.workdir}/{name}",
                        timeout=60)
            if r["returncode"] != 0:
                return broke("container_build.write_lock_failed", success=False, stage="write_lock", file=name,
                        stderr=(r["stderr"] or "")[-400:])
        r = cb.exec(f"cd {self.workdir} && pixi install --locked", timeout=1800)
        if r["returncode"] == 0:
            return proven("container_build.install_from_lock_ok", success=True, stderr=(r["stderr"] or "")[-800:])
        return broke("container_build.install_from_lock_failed", success=False, stderr=(r["stderr"] or "")[-800:])


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
        return proven("container_build.micromamba_setup_ok", success=True)  # env.yml is written at add()
    def add(self, cb, specs, channels):
        chans = "\n".join(f"  - {c}" for c in channels)
        deps = "\n".join(f"  - {s}" for s in specs)
        yml = f"name: env\nchannels:\n{chans}\ndependencies:\n{deps}\n"
        # write environment.yml, solve into a named env, then EXPORT an explicit lock
        cb.exec(f"mkdir -p {self.workdir}", timeout=60)
        w = cb.exec(f"cat > {self.workdir}/environment.yml <<'YML'\n{yml}YML", timeout=60)
        if w["returncode"] != 0:
            return broke("container_build.micromamba_write_yml_failed", success=False, stage="write_yml", stderr=w["stderr"][-400:])
        s = cb.exec(f"micromamba create -y -n env -f {self.workdir}/environment.yml", timeout=1800)
        if s["returncode"] != 0:
            return broke("container_build.micromamba_solve_failed", success=False, stage="solve", stderr=(s["stderr"] or "")[-800:])
        e = cb.exec(f"micromamba env export -n env --explicit > {self.workdir}/env.lock", timeout=120)
        if e["returncode"] == 0:
            return proven("container_build.micromamba_add_ok", success=True, stderr=(e["stderr"] or "")[-400:])
        return broke("container_build.micromamba_export_failed", success=False, stderr=(e["stderr"] or "")[-400:])
    def add_pypi(self, cb, specs):
        # micromamba's explicit lock (URLs+sha256) can't capture PyPI, so a pip
        # install here would NOT replay in the materialized image — refuse honestly
        # rather than silently drop it. PyPI specs ⇒ use the pixi engine (default).
        return refused("container_build.micromamba_pypi_unsupported", success=False, reason="PyPI specs are not supported by the micromamba "
                "engine (its explicit lock can't capture pip, so they wouldn't materialize in "
                "the shipped image) — use the pixi engine (the default) for PyPI.")
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
    apt_snapshot: str = "",
    activation_env: Optional[dict[str, str]] = None,
) -> str:
    """Assemble the ship-image Dockerfile from a recorded build (pure — no docker).

    Multi-stage (Phase D): a `builder` stage carries the full toolchain + engine +
    long-tail builds; the shipped RUNTIME stage starts slim and COPYs only the engine
    env (at identical paths, so conda/pixi prefixes stay valid) + /usr/local + /opt/
    tools from the builder, with just the *.so RUNTIME apt plus the union of every
    recorded step's DECLARED `runtime_packages` (that is how a jar tool's JRE reaches
    the shipped stage; names that aren't plain Debian package tokens are dropped). build-essential / -dev headers / git / the engine installer never
    ship. Engine-specific lines come from `engine`; long-tail commands are baked
    VERBATIM (the exact commands that ran + validated in the build container)."""
    build_apt = _BUILD_APT + (f" {apt_extra}" if apt_extra.strip() else "")
    runtime_apt = _RUNTIME_APT
    # What the SHIPPED stage needs is DECLARED by each generator as data. This used to
    # read `purpose.endswith("(java jar)")` — a human-facing prose string, rendered two
    # lines down as a Dockerfile comment — which meant only the jar tier could ever
    # earn a JRE. A java tool delivered by synthesized / script_repo / release_binary
    # (an snpEff or Trimmomatic zip is exactly that shape) apt-installed its JRE into
    # the BUILDER, and the runtime stage COPYs only /usr/local + /opt/tools, so
    # /usr/bin/java never shipped. VALIDATED_IN_IMAGE did not catch it: those tiers'
    # default evidence ends in `command -v {wrap}`, which passes on a wrapper script
    # whose interpreter is missing.
    extra_runtime = sorted({
        pkg for s in longtail_steps for pkg in (s.get("runtime_packages") or [])
        if _SAFE_APT_PKG.match(str(pkg))
    })
    if extra_runtime:
        runtime_apt += " " + " ".join(extra_runtime)

    def _labels():
        out = ['LABEL build_method="container-native"']
        if has_env_layer:                        # engine present ONLY if conda/pip specs declared
            out.append(f'LABEL env_engine="{engine.name}"')
        return out

    def _apt(pkgs):
        if apt_snapshot:
            # Point apt at snapshot.debian.org BEFORE update — freezes the apt
            # archive bytes to the captured moment. printf builds the sources.list
            # inline so the Dockerfile stays single-file (no COPY of a list file).
            sources = _snapshot_sources_list(apt_snapshot).replace("\n", "\\n")
            return [f"RUN printf '{sources}' > /etc/apt/sources.list \\",
                    "    && rm -rf /etc/apt/sources.list.d/* \\",
                    "    && apt-get -o Acquire::Check-Valid-Until=false update \\",
                    "    && apt-get install -y --no-install-recommends \\",
                    f"        {pkgs} \\", "    && rm -rf /var/lib/apt/lists/*", ""]
        return ["RUN apt-get update && apt-get install -y --no-install-recommends \\",
                f"        {pkgs} \\", "    && rm -rf /var/lib/apt/lists/*", ""]

    # ---- builder stage: full toolchain + engine + long-tail builds ----
    lines = ["# Auto-generated by bioinf_agent — container-native build (validated==shipped)",
             "# Multi-stage: builder (full toolchain) -> slim runtime (env + artifacts only).",
             f"FROM {base} AS builder", *_labels(), ""]
    lines += _apt(build_apt)
    # SWH-fallback helper for source/script_repo installs — see _SWH_CLONE_SCRIPT
    # for the contract. Stays in the BUILDER stage; never ships to runtime.
    lines += _swh_clone_install_lines()
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
              "COPY --from=builder /opt/tools /opt/tools", ""]
    # Bake conda activate.d env deltas (JAVA_HOME, GDAL_DATA, PROJ_LIB, R_HOME, …)
    # + the fully-activated PATH. AFTER engine.runtime_lines so the activated PATH
    # (which adds e.g. the JVM bin) wins. Without this, apptainer exec sees an
    # env missing the activation vars and JVM/GDAL/PROJ/R conda tools break —
    # the tool ran green in the build only because the build activates the env.
    if activation_env:
        lines += ["# conda activate.d env deltas — baked so `apptainer exec` gets the",
                  "# same activated environment the build validated against."]
        for k in sorted(activation_env):
            v = activation_env[k].replace("\\", "\\\\").replace('"', '\\"').replace("$", "\\$")
            lines.append(f'ENV {k}="{v}"')
        lines.append("")
    lines += ["WORKDIR /data", 'CMD ["/bin/bash"]', ""]
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

    def __init__(self, base: str = BASE_IMAGE,
                 platform: str = "linux/amd64",
                 engine: Optional[EnvEngine] = None,
                 channels: Optional[list[str]] = None,
                 workdir: str = "/work",
                 apt_snapshot: str = ""):
        self.base = base
        self.platform = platform
        self.engine = engine or PixiEngine(platform)
        self.channels = channels or ["conda-forge", "bioconda"]
        # When set, every apt-get call (live build container AND emitted Dockerfile)
        # points at snapshot.debian.org/<apt_snapshot> instead of the live Debian
        # mirror. Freezes the apt layer to fixed bytes across time/machines.
        self.apt_snapshot = apt_snapshot
        self.workdir = workdir
        self.cid: Optional[str] = None
        self.has_env_layer = False
        self._engine_installed = False
        self.longtail: list[dict] = []
        self.log: list[str] = []

    def _emulated(self) -> bool:
        """True when the target build platform differs from the host CPU arch, so
        the build runs under QEMU emulation (10-50x slower — an import-heavy tool's
        evidence smoke needs a much bigger timeout). Compares amd64-ness both ways."""
        import platform as _plat
        host = _plat.machine().lower()
        host_amd = host in ("x86_64", "amd64")
        tgt_amd = "amd64" in (self.platform or "").lower() or "x86_64" in (self.platform or "").lower()
        return host_amd != tgt_amd

    def capture_activation_env(self) -> dict[str, str]:
        """Capture the env-var deltas conda's `activate.d/*.sh` scripts produce, so
        freeze can BAKE them as `ENV` in the runtime image.

        The self-activating image bakes PATH + CONDA_PREFIX but does NOT run
        activate.d — so JAVA_HOME (openjdk), GDAL_DATA, PROJ_LIB, R_HOME, … are
        unset and their tools break under `apptainer exec` (which ignores the
        docker ENTRYPOINT, so a login-shell hook can't set them either — they MUST
        be static ENV). We run the ENGINE's activation (`pixi run env`) in the
        builder and keep (a) every var whose value points INTO the env prefix (the
        activate.d outputs) and (b) the fully-activated PATH (adds e.g. the JVM
        bin). Result is baked verbatim → the shipped env == the validated env."""
        if not self.has_env_layer or not self.cid:
            return {}
        ep = self.engine.env_prefix()
        r = self.exec(self.engine.run("env"), timeout=120)
        if r.get("returncode") != 0:
            return {}
        skip = {"CONDA_PREFIX", "PWD", "OLDPWD", "SHLVL", "_", "HOME",
                "HOSTNAME", "TERM", "PS1", "SHELL"}
        out: dict[str, str] = {}
        for line in (r.get("stdout") or "").splitlines():
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k in skip or k.startswith("PIXI_") or k.startswith("CONDA_"):
                continue
            if k == "PATH":
                out["PATH"] = v            # activated PATH
            elif ep and ep in v:            # var points into the env prefix
                out[k] = v
        return out

    @staticmethod
    def _sh(args: list[str], timeout: int = 1800) -> dict[str, Any]:
        # errors="replace": a tool's `--version` / banner probe can emit non-UTF-8
        # bytes (e.g. `pigz` with a compress flag writes gzip magic 0x1f 0x8b to
        # stdout). Strict text-mode decoding would raise UnicodeDecodeError and
        # crash the whole build/validation; we tolerate garbled bytes instead —
        # the returncode is what the honesty check reads, the text is diagnostic.
        try:
            p = subprocess.run(args, capture_output=True, text=True,
                               errors="replace", timeout=timeout)
        except subprocess.TimeoutExpired as e:
            # A hung/slow step (e.g. an import-heavy tool's evidence smoke under
            # QEMU cross-arch emulation) must NOT crash the whole freeze with an
            # unhandled TimeoutExpired — return a structured failure (rc=124, the
            # conventional timeout code) so the honesty check reports a clean
            # evidence/install failure and the freeze returns a broke() record.
            out = e.stdout if isinstance(e.stdout, str) else (e.stdout or b"").decode("utf-8", "replace")
            err = e.stderr if isinstance(e.stderr, str) else (e.stderr or b"").decode("utf-8", "replace")
            return {"returncode": 124, "stdout": out or "",
                    "stderr": ((err or "") + f"\n[_sh] command timed out after {timeout}s").strip()}
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
            return broke("container_build.start_run_failed", success=False, stage="run", stderr=r["stderr"][-800:])
        self.cid = (r["stdout"] or "").strip()
        if self.apt_snapshot:
            # Pin apt to the captured snapshot.debian.org timestamp BEFORE update —
            # the live build container resolves apt versions identically to what a
            # rebuild at any future time would. Acquire::Check-Valid-Until=false
            # accepts the snapshot's expired Valid-Until (GPG signature still valid).
            import base64
            b64 = base64.b64encode(_snapshot_sources_list(self.apt_snapshot).encode()).decode()
            setup = (f"echo {b64} | base64 -d > /etc/apt/sources.list && "
                     f"rm -rf /etc/apt/sources.list.d/* && "
                     f"apt-get -o Acquire::Check-Valid-Until=false -qq update && "
                     f"apt-get -qq install -y --no-install-recommends {_BASE_APT} "
                     f">/dev/null 2>&1")
        else:
            setup = (f"apt-get -qq update && apt-get -qq install -y "
                     f"--no-install-recommends {_BASE_APT} >/dev/null 2>&1")
        s = self._sh(["docker", "exec", self.cid, "bash", "-c", setup], timeout=900)
        if s["returncode"] != 0:
            self.log.append(f"start rc={s['returncode']}")
            return broke("container_build.start_setup_failed", success=False, container=self.cid, stderr=s["stderr"][-800:])
        # Install the SWH-fallback helper for source-tier installs. Same script
        # also goes into the emitted Dockerfile's builder stage so the bytes that
        # ran here match the bytes that ship.
        import base64
        b64 = base64.b64encode(_SWH_CLONE_SCRIPT.encode()).decode()
        sw = self._sh(["docker", "exec", self.cid, "bash", "-c",
                       f"echo {b64} | base64 -d > /usr/local/bin/_swh_clone "
                       "&& chmod +x /usr/local/bin/_swh_clone"], timeout=60)
        self.log.append(f"start rc=0 swh_clone_install={sw['returncode']}")
        return proven("container_build.started", success=True, container=self.cid, stderr="")

    # -- DECLARE: conda/pip (one co-solve via the engine) ------------------
    def declare(self, specs: list[str], timeout: int = 1800) -> dict[str, Any]:
        """Add ALL conda/pip specs in ONE call → one co-solve → a lock. (Never one
        at a time — sequential solves are order-dependent and can downgrade.) The
        engine is installed lazily here on first use."""
        if not specs:
            return proven("container_build.declare_no_specs", success=True, skipped="no specs")
        if not self._engine_installed:
            inst = self.exec(self.engine.install_commands(), timeout=900)
            if inst["returncode"] != 0:
                return broke("container_build.declare_engine_install_failed", success=False, stage="engine_install",
                        stderr=(inst["stderr"] or "")[-800:])
            es = self.engine.setup(self)
            if not es.get("success", True):
                return broke("container_build.declare_engine_setup_failed", success=False, stage="engine_setup", stderr=es.get("stderr", ""))
            self._engine_installed = True
        res = self.engine.add(self, specs, self.channels)
        self.log.append(f"declare {specs} -> {res.get('success')}")
        if res.get("success"):
            self.has_env_layer = True
        return res

    # -- DECLARE: install from a PREBAKED lock — no solve --------------------
    def declare_locked(self, lock_files: dict[str, str], timeout: int = 1800) -> dict[str, Any]:
        """Materialize the env from a PREBAKED lock instead of solving from specs.
        Used by the recipe-replay path: a freeze captures the engine's lock files
        (pixi.toml + pixi.lock for pixi) and the recipe carries them so a later
        rebuild gets the IDENTICAL env, byte-for-byte, no consultation with the
        live conda channels. Same lazy-engine-install pattern as `declare`."""
        if not lock_files:
            return proven("container_build.declare_locked_no_files", success=True, skipped="no lock files")
        if not self._engine_installed:
            inst = self.exec(self.engine.install_commands(), timeout=900)
            if inst["returncode"] != 0:
                return broke("container_build.declare_locked_engine_install_failed", success=False, stage="engine_install",
                        stderr=(inst["stderr"] or "")[-800:])
            # NB: skip engine.setup() — it would `pixi init` and write a fresh
            # pixi.toml we'd immediately overwrite. install_from_lock writes the
            # prebaked pixi.toml + pixi.lock directly, no init needed.
            self._engine_installed = True
        res = self.engine.install_from_lock(self, lock_files)
        self.log.append(f"declare_locked {sorted(lock_files)} -> {res.get('success')}")
        if res.get("success"):
            self.has_env_layer = True
        return res

    # -- DECLARE: PyPI specs (co-solved into the env+lock by the engine) ----
    def declare_pypi(self, specs: list[str], timeout: int = 1800) -> dict[str, Any]:
        """Add PyPI specs via the engine (pixi add --pypi → pixi.lock, materializes
        with the conda layer). The engine is installed lazily here on first use, so a
        pip-only env still gets one. Refused by engines whose lock can't hold pip."""
        if not specs:
            return proven("container_build.declare_pypi_no_specs", success=True, skipped="no specs")
        if not self._engine_installed:
            inst = self.exec(self.engine.install_commands(), timeout=900)
            if inst["returncode"] != 0:
                return broke("container_build.declare_pypi_engine_install_failed", success=False, stage="engine_install", stderr=(inst["stderr"] or "")[-800:])
            es = self.engine.setup(self)
            if not es.get("success", True):
                return broke("container_build.declare_pypi_engine_setup_failed", success=False, stage="engine_setup", stderr=es.get("stderr", ""))
            self._engine_installed = True
        res = self.engine.add_pypi(self, specs)
        self.log.append(f"declare_pypi {specs} -> {res.get('success')}")
        if res.get("success"):
            self.has_env_layer = True
        return res

    # -- DECLARE: long-tail command (binary/jar/source/cargo/go/perl) ------
    def run(self, command: str, evidence: str, purpose: str = "", tool: str = "",
            engine_coupled: bool = False, provenance: dict | None = None,
            runtime_packages: list[str] | None = None,
            timeout: int = 1800) -> dict[str, Any]:
        """Run a long-tail install command, then PROVE it with `evidence` (exit 0),
        both in the build container. On success the command is recorded for verbatim
        baking; `evidence` is re-run in the built image at freeze.

        `tool` is the command this step puts on PATH (`seqtk`) — DISTINCT from
        `purpose`, which is prose for humans (`seqtk (source @ 94e7070)`). Every one of
        the ten install_commands generators already computes it; it used to be dropped
        right here, one line before it would have been recorded, after which
        `_install_anchor` tried to scrape it back out of the prose. That round trip is
        why the ENV report labelled four binaries "tool" and cited htslib's version for
        bcftools. The producer knows the name — record it (audit 2026-07-16, Rule 1).

        engine_coupled: the command (and evidence) need the engine env active — the
        BUILD uses an engine-provided toolchain (rust/go/perl) or the artifact lives
        in the engine env. Wrapped with engine.run() so it's correct both in the
        build container AND when baked (the engine layer is in the image)."""
        cmd = self.engine.run(command) if engine_coupled else command
        ev_cmd = self.engine.run(evidence) if engine_coupled else evidence
        inst = self.exec(cmd, timeout=timeout)
        if inst["returncode"] != 0:
            return broke("container_build.run_install_failed", success=False, stage="install", stderr=(inst["stderr"] or "")[-800:])
        # Evidence smoke: 120s is fine natively, but a cross-arch (QEMU) build of an
        # import-heavy tool (hail/pyspark/torch class — Talos's `talos --help`
        # imports cpg_flow+hail, ~2s native → minutes emulated) blows past it. Give
        # emulated builds a far larger cap; the _sh timeout handler keeps a genuine
        # hang from crashing the freeze either way.
        ev_timeout = 900 if self._emulated() else 120
        ev = self.exec(ev_cmd, timeout=ev_timeout)
        if ev["returncode"] != 0:
            return broke("container_build.run_evidence_failed", success=False, stage="evidence", evidence=ev_cmd,
                    stderr=(ev["stderr"] or "")[-800:])
        rec = {"command": cmd, "purpose": purpose, "evidence": ev_cmd, "tool": tool,
               "runtime_packages": list(runtime_packages or [])}
        if provenance:                       # synthesis tier: carry the per-command
            rec["provenance"] = provenance   # provenance into the recipe (audit + verify)
        self.longtail.append(rec)
        self.log.append(f"run [{purpose}] rc=0 ev_ok coupled={engine_coupled}")
        return proven("container_build.run_ok", success=True, evidence_output=(ev["stdout"] or "").strip()[:200])

    def install(self, spec: dict, timeout: int = 1800) -> dict[str, Any]:
        """Run an install_commands generator's spec ({command, evidence, tool, purpose,
        engine_coupled?}). The single entry point for every long-tail tier — the
        generator carries the per-tier knowledge; the locus just runs+bakes it.

        `spec["tool"]` is emitted by ALL TEN generators and was dropped here until the
        2026-07-16 audit — see `run()`'s docstring for what that cost downstream."""
        return self.run(spec["command"], spec["evidence"], spec.get("purpose", ""),
                        tool=spec.get("tool", ""),
                        engine_coupled=spec.get("engine_coupled", False),
                        provenance=spec.get("provenance"),
                        runtime_packages=spec.get("runtime_packages"), timeout=timeout)

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
                    return broke("container_build.freeze_cp_failed", success=False, stage="cp", file=f, stderr=cp["stderr"][-400:])
        # Capture conda activate.d env deltas (JAVA_HOME, GDAL_DATA, …) + the
        # activated PATH from the live builder, to BAKE them into the runtime image
        # (apptainer exec can't run an activation hook — the env must be static).
        activation_env = self.capture_activation_env()
        dockerfile = emit_dockerfile(self.base, engine=self.engine,
                                     has_env_layer=self.has_env_layer, longtail_steps=self.longtail,
                                     apt_snapshot=self.apt_snapshot,
                                     activation_env=activation_env)
        (build_dir / "Dockerfile").write_text(dockerfile)
        tag = f"{name}:{version}" if version else f"{name}:latest"
        b = self._sh(["docker", "buildx", "build", "--platform", self.platform,
                      "--load", "-t", tag, str(build_dir)], timeout=2400)
        if b["returncode"] != 0:
            return broke("container_build.freeze_build_failed", success=False, stage="build", dockerfile=str(build_dir / "Dockerfile"),
                    stderr=(b["stderr"] or "")[-1200:])
        return proven("container_build.frozen", success=True, image=tag, engine=self.engine.name,
                dockerfile=str(build_dir / "Dockerfile"),
                build_method="container-native", platform=self.platform)

    # Banner probe — non-fakeable version capture. The probe COMMAND is synthesized
    # in here from a tool token only; no agent text reaches the shell. A token that
    # doesn't match a safe identifier is SKIPPED (no banner captured) rather than
    # risk shell injection through a crafted tool name.
    _SAFE_TOOL = re.compile(r"^[A-Za-z0-9._-]+$")
    # Two variants — `--version` first (universal among modern tools), then bare
    # invocation (catches the long-tail like seqtk that prints its version banner
    # only when called with no args). Each is plain-exec in the shipped image.
    _BANNER_VARIANTS = ("--version", "")

    def validate_in_image(self, image: str, checks: list[str],
                          probe_tools: Optional[list[str]] = None) -> dict[str, Any]:
        """Re-run checks in the BUILT image — proves validated==shipped. Runs each
        check PLAIN (no engine activation prefix): the runtime image is self-
        activating (env baked onto PATH), so this is byte-for-byte how `apptainer
        exec image <check>` runs it on HPC. (Activating here would mask a tool that
        the deployment contract can't actually reach.)

        Also probes each `probe_tools` token for its self-reported version banner
        (`<tool> --version`, then bare `<tool>` — combined stdout+stderr, truncated
        to 1200 chars). The probe is NON-FAKEABLE: the command is synthesized HERE
        from the structural tool token only — no agent text reaches the shell.
        Tokens that don't match `_SAFE_TOOL` are SKIPPED (empty banner) rather than
        risk shell injection through a crafted name. Banners are returned in their
        own `banners` field, separate from the evidence-check `out` (the renderer
        prefers banners for version extraction; the evidence `out` is from a
        check-command that CAN be agent-supplied via install primitives)."""
        results, ok = {}, True
        for c in checks:
            r = self._sh(["docker", "run", "--rm", "--platform", self.platform, image, "bash", "-c",
                          f'cd {self.workdir} 2>/dev/null || true; {c}'],
                         timeout=300)
            results[c] = {"rc": r["returncode"], "out": (r["stdout"] or "").strip()[:200]}
            ok = ok and r["returncode"] == 0
        banners: dict[str, str] = {}
        for tool in (probe_tools or []):
            if not tool or tool in banners:
                continue
            if not self._SAFE_TOOL.match(tool):
                banners[tool] = ""   # unsafe token → no probe, no banner
                continue
            parts: list[str] = []
            for flag in self._BANNER_VARIANTS:
                cmd = f"{tool} {flag}".strip()
                # Bail out BEFORE invoking when the token isn't on PATH. `2>&1 || true`
                # captures the shell's own `bash: line 1: X: command not found` as the
                # banner text, and the renderer reads banners as VERSION material — so
                # an absent token used to render as a tool's self-reported version. Not
                # every verification is labelled on a binary (an `openjdk` row is
                # verified by invoking `java`), and this is a version field: it must be
                # empty when there is nothing to report, never a shell error.
                p = self._sh(["docker", "run", "--rm", "--platform", self.platform, image,
                              "bash", "-c",
                              f"command -v {tool} >/dev/null 2>&1 || exit 0; {cmd} 2>&1 || true"],
                             timeout=60)
                text = (p.get("stdout") or "").strip()
                if text:
                    parts.append(text)
                    if sum(len(x) for x in parts) > 1200:
                        break
            banners[tool] = "\n".join(parts)[:1200]
        if ok:
            return proven("container_build.validated_in_image", success=True, checks=results, banners=banners)
        return broke("container_build.validation_in_image_failed", success=False, checks=results, banners=banners)

    def image_digest(self, image: str) -> str:
        """The built image's content id (sha256), the local shipping handle.

        `.Id` is the RIGHT answer here and only here: an image we just BUILT has no
        registry manifest digest until it is pushed, so its daemon content id is the
        only id it has. For an image we PULLED, the question is different and so is
        the answer — see `registry_manifest_digest`."""
        r = self._sh(["docker", "image", "inspect", "--format", "{{index .Id}}", image])
        return (r["stdout"] or "").strip() if r["returncode"] == 0 else ""

    def resolved_packages(self) -> list[dict]:
        """The FULL resolved package closure actually installed in the env —
        captured ENGINE-AGNOSTICALLY from the conda prefix metadata: conda-meta/
        *.json (every conda package) + site-packages/*.dist-info (every PyPI
        package). Read from the built env in the live build container, so it's the
        ground truth the agent can't fake — this is the 'along for the ride'
        provenance the env report splits against the requested tools. Empty for a
        pure long-tail env (no conda/pip layer → no prefix)."""
        if not self.cid or not self.has_env_layer:
            return []
        ep = self.engine.env_prefix()
        cmd = (f'for f in {ep}/conda-meta/*.json; do [ -e "$f" ] && '
               f'echo "conda $(basename "$f" .json)"; done; '
               f'for d in {ep}/lib/python*/site-packages/*.dist-info; do [ -e "$d" ] && '
               f'echo "pypi $(basename "$d" .dist-info)"; done')
        r = self.exec(cmd, timeout=120)
        return self._parse_prefix_scan(r.get("stdout") or "")

    @staticmethod
    def _parse_prefix_scan(stdout: str) -> list[dict]:
        """Parse the `conda <name-version-build>` / `pypi <name-version>` lines a
        conda-prefix scan emits into a deduped, sorted [{name, version, kind}].
        Shared by resolved_packages (live build container) and conda_sbom_from_image
        (adopted image) so the two SBOM sources parse identically."""
        pkgs: dict[str, dict] = {}
        for line in (stdout or "").splitlines():
            kind, _, stem = line.strip().partition(" ")
            if not stem:
                continue
            if kind == "conda":           # name-version-build
                parts = stem.rsplit("-", 2)
                name, ver = (parts[0], parts[1]) if len(parts) == 3 else (stem, "")
            elif kind == "pypi":           # name-version
                name, _, ver = stem.rpartition("-")
                name = (name or stem).replace("_", "-")
            else:
                continue
            pkgs.setdefault(name.lower(), {"name": name, "version": ver, "kind": kind})
        return sorted(pkgs.values(), key=lambda p: p["name"].lower())

    @staticmethod
    def conda_sbom_from_image(image: str, platform: str) -> list[dict]:
        """The image-based analog of resolved_packages() — read the conda/pip
        closure from a SHIPPED image (via a throwaway container) rather than a live
        build container. This is the ADOPT path's SBOM source: an adopted
        biocontainer has no build container, but we still want the full per-tool
        package closure so the env report can show HUMAN-READABLE per-tool versions
        (samtools 1.21, bwa 0.7.17) instead of the shared mulled image tag.

        BioContainers put the conda prefix at /usr/local (mulled / single-tool) or
        /opt/conda; scan those plus any /opt/conda/envs/*. Best-effort: [] if none
        exist or docker fails (the report then falls back to the tag)."""
        scan = (
            'for ep in /usr/local /opt/conda /opt/conda/envs/*; do '
            '[ -d "$ep/conda-meta" ] || continue; '
            'for f in "$ep"/conda-meta/*.json; do [ -e "$f" ] && echo "conda $(basename "$f" .json)"; done; '
            'for d in "$ep"/lib/python*/site-packages/*.dist-info; do [ -e "$d" ] && echo "pypi $(basename "$d" .dist-info)"; done; '
            'done'
        )
        r = ContainerBuild._sh(["docker", "run", "--rm", "--platform", platform, image,
                                "bash", "-c", scan], timeout=120)
        if r.get("returncode") != 0:
            return []
        return ContainerBuild._parse_prefix_scan(r.get("stdout") or "")

    def system_packages(self, image: str) -> list[dict]:
        """The apt/dpkg packages in the SHIPPED image — the system (OS) layer of the
        SBOM. Read from the FINAL image, not the build container: the multi-stage
        builder carries the full toolchain (build-essential, -dev headers) that does
        NOT ship, so only the runtime image tells the truth about what's delivered.
        Completes the self-describing artifact (conda + pip + apt, all versioned) so
        it's auditable WITHOUT forcing byte-identical rebuilds. Best-effort: [] if
        the image has no dpkg (e.g. a non-debian adopted base)."""
        return ContainerBuild.apt_sbom_from_image(image, self.platform)

    @staticmethod
    def apt_sbom_from_image(image: str, platform: str) -> list[dict]:
        """system_packages() as a static, image-only reader — used by the ADOPT
        path (no build instance) to capture the biocontainer's OS layer too, so an
        adopted artifact is as self-describing as a built one."""
        r = ContainerBuild._sh(["docker", "run", "--rm", "--platform", platform, image, "bash", "-c",
                                "dpkg-query -W -f='${Package} ${Version}\\n' 2>/dev/null"], timeout=120)
        if r.get("returncode") != 0:
            return []
        pkgs = []
        for line in (r.get("stdout") or "").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                pkgs.append({"name": parts[0], "version": parts[1], "kind": "apt"})
        return sorted(pkgs, key=lambda p: p["name"])

    def close(self) -> None:
        if self.cid:
            self._sh(["docker", "rm", "-f", self.cid])
            self.cid = None
