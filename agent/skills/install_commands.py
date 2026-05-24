"""
Install-command generators — per-tier "how to install" knowledge in ONE place.

The container-native model replaces the freeze-time `_emit_*` translators: each
generator returns the SHELL COMMAND that installs a long-tail tool IN the build
container. `ContainerBuild.run()` executes it + proves it; freeze bakes it
VERBATIM (no translation, no per-tier freeze branch). The knowledge that used to
live in three places (install primitive + install_method record + _emit_/dispatch)
now lives here, once.

SELF-CONTAINED tiers (here): release binaries, git-source built with the apt C
toolchain, and the MANUAL/half-baked tier (the agent records its own ad-hoc
commands via ContainerBuild.run + authored files via ContainerBuild.write_file —
no generator needed; the container is the captured state, so there are no host
orphans). These install to /usr/local/bin and need no engine env at runtime.

TOOLCHAIN-COUPLED tiers (follow-up): cargo→rust, go→go, perl→perl all need a
conda-provided toolchain to BUILD, so their build command must run with the engine
env active (engine.run(...)). Handled in a later phase alongside that wrapping.

Pure (params in → command string out), unit-testable.
"""

from __future__ import annotations

import shlex
from pathlib import PurePosixPath
from typing import Any

_TOOLS = "/opt/tools"
_ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tar.xz", ".zip")


def release_binary(name: str, url: str, *, sha256: str = "", binary_in_archive: str = "",
                   wrapper: str = "", evidence: str = "") -> dict[str, Any]:
    """Precompiled binary on a release/vendor URL: download → optional sha256
    verify → extract (archive) or place (bare) → chmod → /usr/local/bin wrapper.
    A sha256 mismatch hard-fails; a smoke (evidence) catches a wrong-arch binary."""
    wrap = wrapper or name
    asset = url.rsplit("/", 1)[-1]
    is_archive = asset.lower().endswith(_ARCHIVE_SUFFIXES)
    dest = f"{_TOOLS}/{name}"
    parts = [f"mkdir -p {dest}", "cd /tmp", f"curl -fsSL -o {asset} {shlex.quote(url)}"]
    if sha256:
        parts.append(f'echo "{sha256.lower()}  {asset}" | sha256sum -c -')
    if is_archive:
        parts.append(f"unzip -o {asset} -d {dest}" if asset.lower().endswith(".zip")
                     else f"tar -xf {asset} -C {dest}")
        base = PurePosixPath(binary_in_archive or name).name
        parts += [f'BIN="$(find {dest} -type f -name {shlex.quote(base)} | head -n1)"',
                  'test -n "$BIN"',
                  f'chmod +x "$BIN"', f'ln -sf "$BIN" /usr/local/bin/{wrap}']
    else:
        parts.append(f"install -m 0755 {asset} /usr/local/bin/{wrap}")
    cmd = "set -eux; " + "; ".join(parts)
    ev = evidence or f"{wrap} --version 2>&1 || {wrap} version 2>&1 || command -v {wrap}"
    return {"command": cmd, "evidence": ev, "purpose": f"{name} (release binary)"}


def source(name: str, repo_url: str, *, ref: str = "", build_command: str = "make",
           bin_path: str = "", wrapper: str = "", evidence: str = "") -> dict[str, Any]:
    """Git-source tool built with the apt C toolchain: clone → checkout pinned ref
    → build → MANUAL install of the built binary to /usr/local/bin (most academic
    tools have no `make install` target — that's the half-baked norm). Locally
    built ⇒ can't be wrong-arch, so presence (command -v) is the honest anchor."""
    src = f"{_TOOLS}/{name}/src"
    binp = bin_path or name
    wrap = wrapper or name
    parts = [f"git clone {shlex.quote(repo_url)} {src}", f"cd {src}"]
    if ref:
        parts.append(f"git checkout {shlex.quote(ref)}")
    parts += [build_command,
              f"test -f {src}/{binp}",
              f"install -m 0755 {src}/{binp} /usr/local/bin/{wrap}"]
    cmd = "set -eux; " + "; ".join(parts)
    ev = evidence or f"command -v {wrap}"
    return {"command": cmd, "evidence": ev, "purpose": f"{name} (source @ {ref or 'HEAD'})"}


def script_repo(name: str, repo_url: str, *, ref: str = "", script_rel: str = "",
                interpreter: str = "", wrapper: str = "", evidence: str = "") -> dict[str, Any]:
    """Run-by-path script collection (very common for half-baked academic tools:
    a repo of Python/Perl scripts with no packaging). Clone → chmod the entry
    script → a /usr/local/bin wrapper that execs it (optionally via `interpreter`,
    e.g. the env python). No build."""
    clone = f"{_TOOLS}/{name}"
    wrap = wrapper or name
    entry = f"{clone}/{script_rel}" if script_rel else f"{clone}/{name}"
    parts = [f"git clone {shlex.quote(repo_url)} {clone}", f"cd {clone}"]
    if ref:
        parts.append(f"git checkout {shlex.quote(ref)}")
    parts.append(f"chmod +x {entry} 2>/dev/null || true")
    runline = f"{interpreter} {entry}".strip()
    parts.append(f"printf '#!/bin/sh\\nexec {runline} \"$@\"\\n' > /usr/local/bin/{wrap}")
    parts.append(f"chmod +x /usr/local/bin/{wrap}")
    cmd = "set -eux; " + "; ".join(parts)
    ev = evidence or f"command -v {wrap}"
    return {"command": cmd, "evidence": ev, "purpose": f"{name} (script repo @ {ref or 'HEAD'})"}
