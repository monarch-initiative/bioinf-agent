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


def target_arch(platform: str) -> str:
    """linux/amd64 -> amd64 ; linux/arm64 -> arm64. The Go/Docker arch token."""
    return (platform.split("/")[-1] or "").strip() or "amd64"


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
