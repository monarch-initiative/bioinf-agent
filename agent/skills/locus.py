"""
locus — WHERE a build + its in-image validation actually execute, and whether the
numbers they produce are authoritative.

The container-native contract is "validated == shipped": we re-run each tool's
evidence in the exact image we ship. That pass/fail signal is SOUND under CPU
emulation — qemu and Rosetta are faithful x86 emulators, so a tool that runs proves
it runs on amd64, and a tool that's broken on amd64 fails under emulation too. What
emulation gets WRONG is timing/resource measurement (the Layer-2 I7 numbers) and
the slow-vs-hung distinction (the numba-import-300s false timeout). So the single
honesty-load-bearing distinction is NATIVE vs EMULATED — not which emulator.

This module is the build-locus seam. Today it reports whether the local Docker
daemon runs the target platform natively or under emulation (and, best-effort,
which emulator — purely for an actionable speed advisory). It is where a
remote-amd64-host backend (DOCKER_HOST=ssh://…) will later plug in: the rest of the
system asks `detect_locus(platform)` and acts on `i7_authoritative`, never caring
how the locus was chosen. Library-first: pure detection, no build orchestration.
"""

from __future__ import annotations

import platform as _platform
import subprocess
from functools import lru_cache
from typing import Any


def _sh(args: list[str], timeout: int = 30) -> dict[str, Any]:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {"rc": p.returncode, "out": (p.stdout or "").strip(),
                "err": (p.stderr or "").strip()}
    except Exception as e:  # docker absent / not running / timeout — never fatal
        return {"rc": -1, "out": "", "err": str(e)}


#: Every spelling of a platform this system actually writes down, mapped to the
#: Go/Docker arch token. BOTH dialects reach here and the corpus contains both:
#: freeze()'s `platform=` argument is a CONDA subdir ("linux-64") and is echoed
#: verbatim onto every BUILD record, while container_build receives a DOCKER
#: platform ("linux/amd64") which is what every ADOPT record carries.
#: Kept in step with freeze._PLATFORM_CANON, which is the set of spellings that can
#: reach a record's `platform` field — including the darwin/osx ones (a request_key
#: may name them even though we never ship a darwin image).
_ARCH_TOKENS = {
    "linux-64": "amd64", "linux/amd64": "amd64", "amd64": "amd64", "x86_64": "amd64",
    "osx-64": "amd64", "darwin/amd64": "amd64",
    "linux-aarch64": "arm64", "linux-arm64": "arm64", "linux/arm64": "arm64",
    "linux/arm64/v8": "arm64", "arm64": "arm64", "aarch64": "arm64",
    "osx-arm64": "arm64", "darwin/arm64": "arm64",
}


def target_arch(platform: str) -> str:
    """linux/amd64 -> amd64 ; linux-aarch64 -> arm64. The Go/Docker arch token.

    Reads BOTH the conda-subdir and docker-platform spellings (see `_ARCH_TOKENS`).
    It used to be `platform.split("/")[-1]`, which silently handled only the docker
    form: "linux-aarch64" has no slash, so it parsed to ITSELF and compared unequal
    to every real architecture — fine while the only consumer was
    `detect_locus`-vs-daemon on amd64, and wrong the moment anything compared this
    to an arch read off an image.

    An unrecognized spelling returns "" — NOT the old "amd64" default. A caller
    comparing architectures must be able to tell "this is amd64" from "I do not know
    what this string means", and defaulting the unknown case to the commonest answer
    is how a mismatch check silently passes.
    """
    p = (platform or "").strip().lower()
    return _ARCH_TOKENS.get(p, "")


def image_arch(ref: str) -> dict[str, Any]:
    """What architecture the image at `ref` ACTUALLY is, read from the image itself.

    `daemon_arch()`'s missing sibling, and the observation this whole module was
    short of: every other architecture fact in the system is derived from the
    caller's REQUEST (`platform=`), so a record could state its architecture three
    times — `platform`, `validation_locus`, `image_digest` — without one of them
    having looked at the artifact.

    Returns {resolved, arch}. The two ways `arch` comes back empty are DIFFERENT
    facts and this function will not flatten them:

      resolved=False  — the ref does not inspect (image absent from the daemon).
                        We failed to look; nothing is claimed about the artifact.
      resolved=True, arch=""
                      — the ref inspects but names NO architecture, because it is a
                        MANIFEST LIST / OCI index: a digest addressing a *menu* of
                        per-arch images rather than one image's bytes. Docker reports
                        `.Architecture` as "" for these, correctly. This is a finding,
                        not a gap — see BUILT.platform_pinned in env_honesty.
    """
    if not (ref or "").strip():
        return {"resolved": False, "arch": ""}
    r = _sh(["docker", "image", "inspect", "--format", "{{.Architecture}}", ref])
    if r["rc"] != 0:
        return {"resolved": False, "arch": ""}
    return {"resolved": True, "arch": r["out"].strip().lower()}


#: The in-image probe behind `image_accelerator`. ONE `docker run`, and every line
#: it prints is a fact that needs no parsing to read back:
#:
#:   cudadir   the /usr/local/cuda-X.Y directory NAME — the version is the name.
#:   condacuda the conda-meta filename for cuda-version/cudatoolkit — likewise, conda
#:             writes `<name>-<version>-<build>.json`, so the version is the filename.
#:   rocm      /opt/rocm/.info/version, a file whose entire contents ARE the version.
#:   nvcc      presence only. `nvcc --version` prints prose ("Cuda compilation tools,
#:             release 12.4, V12.4.131") and pulling a number out of it is a regex over
#:             a blob — the shape that made the ENV report cite htslib's version for a
#:             bcftools fork. We record that the compiler is THERE and take the version
#:             from a source that doesn't need interpreting.
#:
#: Deliberately `|| true` throughout: this is an OBSERVATION, and an image without CUDA
#: must return "no CUDA here", not a non-zero rc that the caller reads as "we failed to
#: look". Those are the two different empties `resolved` exists to keep apart.
_ACCEL_PROBE = (
    'echo "cudadir=$(ls -d /usr/local/cuda-[0-9]* 2>/dev/null | sort | tail -1)"; '
    'echo "nvcc=$(command -v nvcc 2>/dev/null)"; '
    'echo "rocm=$(head -1 /opt/rocm/.info/version 2>/dev/null)"; '
    'echo "condacuda=$(ls /opt/conda/conda-meta /usr/local/conda-meta 2>/dev/null '
    '| grep -E "^(cuda-version|cudatoolkit|cuda-nvcc)-" | sort | tail -1)"'
)


def _accel_version_from_dirname(d: str) -> str:
    """/usr/local/cuda-12.4 -> 12.4. The version IS the directory name's tail."""
    base = (d or "").rstrip("/").rsplit("/", 1)[-1]
    return base[len("cuda-"):] if base.startswith("cuda-") else ""


def _accel_version_from_conda_meta(fn: str) -> str:
    """cuda-version-12.4-h1234567_0.json -> 12.4. Conda's own filename convention is
    `<name>-<version>-<build>.json`, so the version is a field, not a scrape."""
    stem = (fn or "").strip()
    if stem.endswith(".json"):
        stem = stem[:-5]
    parts = stem.rsplit("-", 2)          # name, version, build
    return parts[1] if len(parts) == 3 else ""


def image_accelerator(ref: str) -> dict[str, Any]:
    """What accelerator toolkit the image at `ref` ACTUALLY contains, read from the
    image itself. `image_arch`'s sibling, and the observation I12 was short of.

    Before this, every accelerator fact on a record came from the caller: `freeze`'s
    `accel=` / `cuda_version=` arguments, or an `accelerator` dict typed straight into
    `patch_pipeline`. I12 then checked those fields against *each other* — that a
    `cuda` claim carried SOME toolkit_version string, that a `runtime_verified` claim
    carried SOME probe string — and never against the artifact. A record could state
    `type: cuda, toolkit_version: 99.9` over an image with no CUDA in it and clear the
    contract, because nothing in the system had ever opened the image to look.

    Returns {resolved, type, version, source, driver_requirement}:

      resolved=False           — we failed to LOOK (image absent from the daemon, or a
                                 manifest index, which has no filesystem to probe). Says
                                 nothing about the artifact. Never a pass, never a fail.
      resolved=True,           — we looked and there is no accelerator toolkit here.
        type="none"              A real, load-bearing observation: it is what refuses a
                                 cuda claim over a CPU-only image.
      resolved=True,           — we looked and found one. `source` names WHERE the
        type="cuda"|"rocm"       version came from, so a reader never has to guess which
                                 of four possible sources produced the number.

    `driver_requirement` is the image's OWN statement of the host-driver floor it needs
    (nvidia's `NVIDIA_REQUIRE_CUDA`), captured VERBATIM and never parsed into a number.
    It reads `cuda>=12.4 brand=tesla,driver>=470,driver<471 brand=unknown,…` — a set of
    per-brand windows, not a scalar, and squeezing it into one integer would be an
    interpretation dressed up as a measurement. It is disclosure: the gate shows it
    beside a claimed min_driver_version and lets a human compare.
    """
    if not (ref or "").strip():
        return {"resolved": False, "type": "", "version": "", "source": "",
                "driver_requirement": ""}

    # 1. Metadata first — no container, no emulation, works on any image the daemon
    #    holds. nvidia's official images bake CUDA_VERSION, which is exact.
    env_ver, driver_req = "", ""
    r = _sh(["docker", "image", "inspect", "--format",
             "{{range .Config.Env}}{{println .}}{{end}}", ref])
    if r["rc"] != 0:
        return {"resolved": False, "type": "", "version": "", "source": "",
                "driver_requirement": ""}
    for line in r["out"].splitlines():
        k, _, v = line.partition("=")
        if k == "CUDA_VERSION":
            env_ver = v.strip()
        elif k == "NVIDIA_REQUIRE_CUDA":
            driver_req = v.strip()
    if env_ver:
        return {"resolved": True, "type": "cuda", "version": env_ver,
                "source": "image env CUDA_VERSION", "driver_requirement": driver_req}

    # 2. Filesystem — one run, for images that carry a toolkit without advertising it
    #    in ENV (our own conda-built images, rocm images, anything not from nvidia).
    p = _sh(["docker", "run", "--rm", "--entrypoint", "sh", ref, "-c", _ACCEL_PROBE],
            timeout=120)
    if p["rc"] != 0:
        # The image holds no `sh`, or won't start. We failed to look — and saying
        # "no accelerator" here would turn a distroless image into a refusal.
        return {"resolved": False, "type": "", "version": "", "source": "",
                "driver_requirement": driver_req}
    found = {}
    for line in p["out"].splitlines():
        k, _, v = line.partition("=")
        found[k.strip()] = v.strip()

    if found.get("rocm"):
        return {"resolved": True, "type": "rocm", "version": found["rocm"],
                "source": "/opt/rocm/.info/version", "driver_requirement": driver_req}
    if found.get("cudadir"):
        return {"resolved": True, "type": "cuda",
                "version": _accel_version_from_dirname(found["cudadir"]),
                "source": f"toolkit directory {found['cudadir']}",
                "driver_requirement": driver_req}
    if found.get("condacuda"):
        return {"resolved": True, "type": "cuda",
                "version": _accel_version_from_conda_meta(found["condacuda"]),
                "source": f"conda-meta/{found['condacuda']}",
                "driver_requirement": driver_req}
    if found.get("nvcc"):
        # The compiler is here but no source that states a version without being
        # interpreted. An honest half-observation: the type is certain, the version
        # is not recorded rather than guessed at.
        return {"resolved": True, "type": "cuda", "version": "",
                "source": f"nvcc at {found['nvcc']} (version not stated by a "
                          f"non-prose source)", "driver_requirement": driver_req}
    return {"resolved": True, "type": "none", "version": "",
            "source": "no CUDA/ROCm toolkit found in the image "
                      "(env, /usr/local/cuda-*, /opt/rocm, conda-meta, nvcc)",
            "driver_requirement": driver_req}


def daemon_is_remote() -> bool:
    """Is the active Docker daemon REMOTE (DOCKER_HOST=ssh://… or tcp://…)? The build
    + in-image validation path is daemon-agnostic — point DOCKER_HOST at a native
    amd64 host and freeze() builds + validates natively (locus=native) with no code
    change. The one caveat is Layer-2 run_step_in_container, which bind-mounts LOCAL
    test data: a remote daemon can't see local paths, so that path needs the daemon
    local (or the data on the remote host)."""
    import os
    return os.environ.get("DOCKER_HOST", "").strip().startswith(("ssh://", "tcp://"))


@lru_cache(maxsize=1)
def daemon_arch() -> str:
    """The Docker daemon's native architecture (amd64/arm64). On Apple Silicon
    Docker Desktop this is arm64 — the Linux VM's arch — which is exactly why a
    linux/amd64 build there is emulated. Empty string if the daemon can't be
    queried (docker absent / not running)."""
    r = _sh(["docker", "version", "--format", "{{.Server.Arch}}"])
    return r["out"] if r["rc"] == 0 and r["out"] else ""


@lru_cache(maxsize=1)
def _detect_emulator() -> str:
    """Best-effort: which emulator Docker uses for amd64 (rosetta|qemu|unknown).
    NOT honesty-load-bearing — only sharpens the speed advisory.

    We read it HOST-side from Docker Desktop's settings store. (The obvious approach
    — reading the VM's binfmt_misc registrations from inside a container — can't
    work: /proc/sys/fs/binfmt_misc is not mounted in a container's mount namespace,
    so it always reads empty.) When the Rosetta flag isn't explicitly set we return
    'unknown' rather than guess — newer Docker Desktop defaults Rosetta on, and the
    generic advisory stays correct either way."""
    import json
    import os
    if _platform.system() != "Darwin":
        return "unknown"
    path = os.path.expanduser(
        "~/Library/Group Containers/group.com.docker/settings-store.json")
    try:
        with open(path) as fh:
            data = json.load(fh)
    except Exception:
        return "unknown"
    for k, v in data.items():  # key name has drifted across versions; match loosely
        if "rosetta" in k.lower():
            return "rosetta" if v else "qemu"
    return "unknown"


def _emulation_advisory(emulator: str) -> str:
    host = _platform.system()
    apple_silicon = host == "Darwin" and _platform.machine() in ("arm64", "aarch64")
    # Rosetta-class emulation is near-native for MOST work, but specific workloads —
    # notably heavy compute-at-import like scipy.stats (measured ~100x slower under
    # Rosetta than native arm64) — can be pathologically slow. So a slow or
    # timed-out validation under emulation is INCONCLUSIVE, not a failure: the tool
    # may be perfectly healthy on the native ship arch. I7 timings aren't real here.
    caveat = (" Most work runs near-native, but some operations (e.g. heavy scientific "
              "imports such as scipy.stats) can be far slower than native — a slow or "
              "timed-out validation under emulation is INCONCLUSIVE, not a failure. I7 "
              "resource numbers are not authoritative. For authoritative native validation, "
              "point Docker at a native amd64 host (export DOCKER_HOST=ssh://user@amd64-host); "
              "the build + in-image validation path is daemon-agnostic, so freeze() then "
              "reports locus=native with no other change.")
    if emulator == "qemu" and apple_silicon:
        return ("amd64 is emulated via qemu (slow). Enable 'Use Rosetta for x86/amd64 "
                "emulation' in Docker Desktop → Settings → General." + caveat)
    if emulator == "rosetta":
        return "amd64 is emulated via Rosetta." + caveat
    if apple_silicon:
        # Docker Desktop ≥4 defaults amd64 emulation to Rosetta on Apple Silicon, so
        # this is normal — do NOT falsely nag to flip a switch that's already on.
        return "amd64 is emulated on Apple Silicon (Docker Desktop defaults to Rosetta)." + caveat
    return "the target platform is emulated on this host." + caveat


def detect_locus(platform: str = "linux/amd64") -> dict[str, Any]:
    """Where a build for `platform` will run, and whether its resource numbers are
    authoritative. Cheap: one `docker version` plus (when emulated) a host-side
    settings read.

    Returns:
      locus            : "native" | "emulated" | "unknown"
      daemon_arch      : the daemon's arch ("" if unqueryable)
      target_arch      : the requested arch
      daemon_location  : "local" | "remote" (DOCKER_HOST ssh://|tcp://)
      i7_authoritative : True ONLY when native — the gate for trusting I7 timings
      emulator         : "none" | "rosetta" | "qemu" | "unknown"
      advisory         : an actionable one-liner ("" when native)
    """
    tgt = target_arch(platform)
    dmn = daemon_arch()
    location = "remote" if daemon_is_remote() else "local"

    if not dmn:
        return {"locus": "unknown", "daemon_arch": "", "target_arch": tgt,
                "daemon_location": location, "i7_authoritative": False,
                "emulator": "unknown",
                "advisory": "could not query the Docker daemon (is Docker running?)"}

    if dmn == tgt:
        return {"locus": "native", "daemon_arch": dmn, "target_arch": tgt,
                "daemon_location": location, "i7_authoritative": True,
                "emulator": "none", "advisory": ""}

    emulator = _detect_emulator()
    return {"locus": "emulated", "daemon_arch": dmn, "target_arch": tgt,
            "daemon_location": location, "i7_authoritative": False,
            "emulator": emulator, "advisory": _emulation_advisory(emulator)}


def i7_authoritative(platform: str = "linux/amd64") -> bool:
    """Convenience predicate for the Layer-2 path: are captured I7 resource numbers
    real (native) or emulator artefacts (emulated/unknown)?"""
    return detect_locus(platform)["i7_authoritative"]
