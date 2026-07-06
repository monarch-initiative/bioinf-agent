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
    return {"command": cmd, "evidence": ev, "tool": wrap, "purpose": f"{name} (release binary)"}


def jar(name: str, jar_url: str, *, sha256: str = "", java_flags: list[str] | None = None,
        wrapper: str = "", evidence: str = "") -> dict[str, Any]:
    """Java tool (SELF-CONTAINED): ensure a JRE is on PATH (apt default-jre-headless,
    only if `java` is absent — so N jars don't re-apt), download the jar (arch-
    independent bytecode), optional sha256 verify, then write a `java -jar` wrapper
    on PATH. A single-.jar URL (Picard/GATK) and a .zip distribution (Exomiser; the
    jar is auto-located) are both handled. The JRE comes from apt (base PATH), NOT
    the engine, so a jar wrapper runs without `pixi run` — locus-clean, no engine
    coupling. Default evidence proves the wrapper is installed AND the JRE runs; the
    jar's actual execution is proven at workflow-run time (run_step_in_container)."""
    wrap = wrapper or name
    flags = " ".join(java_flags or ["-Xmx4g"])
    asset = jar_url.rsplit("/", 1)[-1] or f"{name}.jar"
    dest = f"{_TOOLS}/{name}"
    parts = [
        'command -v java >/dev/null 2>&1 || { apt-get update && '
        'apt-get install -y --no-install-recommends default-jre-headless && '
        'rm -rf /var/lib/apt/lists/*; }',
        f"mkdir -p {dest}",
        f"curl -fsSL -o {dest}/{asset} {shlex.quote(jar_url)}",
    ]
    if sha256:
        parts.append(f'echo "{sha256.lower()}  {dest}/{asset}" | sha256sum -c -')
    if asset.lower().endswith(".zip"):
        parts += [
            f"unzip -o {dest}/{asset} -d {dest}",
            f"JAR=\"$(find {dest} -name '*.jar' | grep -i {shlex.quote(name)} | head -n1)\"",
            f'JAR="${{JAR:-$(find {dest} -name \'*.jar\' | head -n1)}}"', 'test -n "$JAR"',
            f"printf '#!/bin/sh\\nexec java {flags} -jar %s \"$@\"\\n' \"$JAR\" > /usr/local/bin/{wrap}",
        ]
    else:
        parts.append(
            f"printf '#!/bin/sh\\nexec java {flags} -jar {dest}/{asset} \"$@\"\\n' > /usr/local/bin/{wrap}")
    parts.append(f"chmod +x /usr/local/bin/{wrap}")
    cmd = "set -eux; " + "; ".join(parts)
    ev = evidence or f"command -v {wrap} && java -version"
    return {"command": cmd, "evidence": ev, "tool": wrap, "purpose": f"{name} (java jar)"}


def source(name: str, repo_url: str, *, ref: str = "", build_command: str = "make",
           bin_path: str = "", wrapper: str = "", evidence: str = "") -> dict[str, Any]:
    """Git-source tool built with the apt C toolchain: clone → checkout pinned ref
    → build → MANUAL install of the built binary to /usr/local/bin (most academic
    tools have no `make install` target — that's the half-baked norm). Locally
    built ⇒ can't be wrong-arch, so presence (command -v) is the honest anchor.

    Clone goes through `_swh_clone` (installed in the builder stage) so a rebuild
    when the upstream repo is dead falls back to Software Heritage's vault for
    the same commit. `git checkout <ref>` after still proves the bytes match —
    SWH can't lie about a commit SHA's contents."""
    src = f"{_TOOLS}/{name}/src"
    binp = bin_path or name
    wrap = wrapper or name
    parts = [f"_swh_clone {shlex.quote(repo_url)} {shlex.quote(ref)} {src}",
             f"cd {src}"]
    if ref:
        parts.append(f"git checkout {shlex.quote(ref)}")
    parts += [build_command,
              f"test -f {src}/{binp}",
              f"install -m 0755 {src}/{binp} /usr/local/bin/{wrap}"]
    cmd = "set -eux; " + "; ".join(parts)
    # N3 (batch-3): wrapper-smoke evidence — INVOKE the binary to prove it
    # runs, not just that the file exists. A C source build CAN produce a
    # binary that runs into shared-lib failures at exec time; `command -v`
    # passed those cases silently. Chain mirrors release_binary's default.
    ev = evidence or (
        f"{wrap} --help >/dev/null 2>&1 || {wrap} --version >/dev/null 2>&1 || "
        f"{wrap} -h >/dev/null 2>&1 || command -v {wrap}"
    )
    return {"command": cmd, "evidence": ev, "tool": wrap,
            "purpose": f"{name} (source @ {ref or 'HEAD'})"}


def cargo(name: str, crate: str = "", *, version: str = "", git_url: str = "",
          binary_name: str = "", evidence: str = "") -> dict[str, Any]:
    """Rust crate (TOOLCHAIN-COUPLED): built with the engine's rust toolchain
    (declare ['rust'] first), output to /usr/local/bin via --root. --locked uses
    the crate's Cargo.lock. The OUTPUT binary is self-contained at runtime; only
    the BUILD needs the engine, so it runs via engine.run() (engine_coupled)."""
    binp = binary_name or crate or name
    if git_url:
        src = f"--git {shlex.quote(git_url)}"
    else:
        src = shlex.quote(crate or name) + (f" --version {shlex.quote(version)}" if version else "")
    # N3 (batch-3): smoke-test evidence — INVOKE the binary, don't just check
    # that the file exists. Cargo's --root produces a binary in /usr/local/bin
    # that's self-contained AT BUILD TIME, but a botched build or a binary
    # with missing dynamic deps would silently pass `command -v`.
    return {"command": f"cargo install {src} --root /usr/local --locked",
            "evidence": evidence or (
                f"{binp} --help >/dev/null 2>&1 || {binp} --version >/dev/null 2>&1 || "
                f"{binp} -h >/dev/null 2>&1 || command -v {binp}"),
            "tool": binp,
            "purpose": f"{name} (cargo, via engine rust)", "engine_coupled": True}


def go(name: str, package: str, *, version: str = "latest", binary_name: str = "",
       evidence: str = "") -> dict[str, Any]:
    """Go tool (TOOLCHAIN-COUPLED): built with the engine's go toolchain (declare
    ['go'] first), output to /usr/local/bin via GOBIN. Self-contained at runtime."""
    binp = binary_name or package.rstrip("/").split("/")[-1]
    spec = f"{package}@{version}" if version else package
    return {"command": f"GOBIN=/usr/local/bin GOFLAGS=-mod=mod go install {shlex.quote(spec)}",
            # N3 (batch-3): same smoke-test treatment as cargo/source/script_repo.
            "evidence": evidence or (
                f"{binp} --help >/dev/null 2>&1 || {binp} --version >/dev/null 2>&1 || "
                f"{binp} -h >/dev/null 2>&1 || command -v {binp}"),
            "tool": binp,
            "purpose": f"{name} (go, via engine go)", "engine_coupled": True}


def perl_cpanm(module: str, *, distribution: str = "", cpanm_flags: str = "--notest",
               build_env: str = "", evidence: str = "") -> dict[str, Any]:
    """CPAN module (TOOLCHAIN-COUPLED): installed into the engine's perl via cpanm
    (declare ['perl','perl-app-cpanminus', and for XS 'c-compiler'/'cxx-compiler']
    first). Both install AND the `perl -M{module} -e1` load run via engine.run(),
    since the module lives in the engine's perl. PREFER conda when the module is
    packaged (perl-* on bioconda) — this is the unpackaged-residue fallback.
    build_env (e.g. HTSLIB_DIR=$CONDA_PREFIX) handles XS link hints.

    XS-against-conda-perl hardening (C2): conda perl (5.32) still #includes
    <xlocale.h>, which modern glibc folded into <locale.h> — so we shim it in the
    engine perl's include dir BEFORE cpanm compiles. $CONDA_PREFIX resolves under
    engine.run() (the env is active), and the conda c-/cxx-compiler (declared in the
    env) gives XS perl's OWN toolchain (the system gcc has the wrong sysroot). The
    shim is harmless for pure-perl modules (an unused header)."""
    target = distribution or module
    pre = f"{build_env} " if build_env.strip() else ""
    shim = 'printf "#include <locale.h>\\n" > "$CONDA_PREFIX/include/xlocale.h" 2>/dev/null || true'
    return {"command": f"{shim}; {pre}cpanm {cpanm_flags} {shlex.quote(target)}",
            "evidence": evidence or f"perl -M{module} -e1", "tool": module,
            "purpose": f"{module} (cpanm, via engine perl)", "engine_coupled": True}


def r_package(name: str, *, source: str = "cran", repos: str = "https://cloud.r-project.org",
              bioc_mirror: str = "https://bioconductor.org",
              evidence: str = "") -> dict[str, Any]:
    """R package (TOOLCHAIN-COUPLED): installed into the engine's R via Rscript
    (declare ['r-base'] first — and the conda c-/cxx-compiler when a source build
    compiles C/C++/Fortran). source = 'cran' | 'bioconductor' | 'github:owner/repo'.
    Verified in-image by `library(name)` (loads or exits non-zero). PREFER conda when
    the package is on bioconda (r-* / bioconductor-* install far more reliably) — this
    is the unpackaged-residue fallback, the R analog of perl_cpanm.

    Every install command ends with a `requireNamespace || stop()` load-or-die check
    so a silent-success — BiocManager's mirror auto-detect picking an unreachable
    mirror is the load-bearing case, observed under conda r-base where auto-select
    pulls a foreign Bioconductor mirror that 404s — fails the build AT THIS STEP with
    the install tool's actual warnings/error, instead of slipping past returncode 0
    and surfacing later at the evidence stage with no diagnostic. The bioconductor
    branch also pins `options(BioC_mirror = bioc_mirror)` so the auto-select wildcard
    is out of the picture entirely. The github branch passes `dependencies = FALSE`
    so transitive deps must come from conda or earlier R install steps (the install
    plan is the source of truth, not remotes' opportunistic auto-resolution)."""
    load_or_die = (f'if(!requireNamespace("{name}",quietly=TRUE)) '
                   f'stop("install reported success but \'{name}\' is not loadable")')
    if source == "bioconductor":
        inst = (f'options(BioC_mirror="{bioc_mirror}"); '
                f'if(!requireNamespace("BiocManager",quietly=TRUE)) '
                f'install.packages("BiocManager",repos="{repos}"); '
                f'BiocManager::install("{name}",ask=FALSE,update=FALSE); '
                f'{load_or_die}')
    elif source.startswith("github:"):
        repo = source.split(":", 1)[1]
        inst = (f'if(!requireNamespace("remotes",quietly=TRUE)) '
                f'install.packages("remotes",repos="{repos}"); '
                f'remotes::install_github("{repo}",dependencies=FALSE); '
                f'{load_or_die}')
    else:  # cran (default)
        inst = f'install.packages("{name}",repos="{repos}"); {load_or_die}'
    cmd = f"Rscript -e {shlex.quote(inst)}"
    # Evidence loads the package AND prints its version. The version cat is what
    # lands "GAPIT 4.1.0" in the installed-version column of the env report:
    # R packages installed via BiocManager / remotes::install_github don't show
    # up in conda's package db (they're not conda-installed), so the report's
    # SBOM has no entry for them — without the cat() the renderer falls all the
    # way through `_resolved_version`'s chain (conda → banner → evidence-out →
    # install-anchor) and prints '—'. With it, the version lands in
    # verifications[].out where `_extract_version` finds it.
    #
    # Honesty preserved: the version is captured at validation time IN the
    # shipped image, by R's own packageVersion() reading the installed
    # package's DESCRIPTION. It's the same source of truth the install primitive
    # uses on the host; just executed at the right locus.
    ev = evidence or (
        f"Rscript -e "
        f"{shlex.quote(f'suppressPackageStartupMessages(library({name})); cat(as.character(packageVersion(' + repr(name) + ')))')}"
    )
    return {"command": cmd, "evidence": ev, "tool": name,
            "purpose": f"{name} (R {source})", "engine_coupled": True}


def pip_install_with_flags(name: str, *, version: str = "",
                           flags: list[str] | None = None,
                           evidence: str = "") -> dict[str, Any]:
    """Pip install that needs FLAGS the engine can't express (`--no-binary`,
    `--no-build-isolation`, `--index-url`, etc.). Routes as an engine-coupled
    long-tail step run AFTER the pixi/micromamba layer materializes python.

    Why a separate generator (not the engine's --pypi path): `pixi add --pypi`
    is backed by uv and defaults to wheels. It doesn't accept pip flags, so a
    `pip install --no-binary :all: pysam` request taken through the engine
    silently SUBSTITUTES a manylinux wheel for the validated source compile —
    the pysam-stress P2 trust violation (validated source-built pysam==0.24.0
    cp311 → shipped pysam==0.24.0 cp314 manylinux wheel).

    By contrast, this generator emits a literal `pip install <flags> <spec>`
    that runs under the engine's python (engine_coupled=True), so the flags
    are honored verbatim and the shipped artifact matches the validated one.
    Verifications use the same dist-metadata probe as the engine pip path,
    so the env report renders consistently regardless of route.

    Trade-off: the flag-bearing install is NOT in the engine lock (uv can't
    represent it). The recipe replays the exact command — same source compile,
    same env — so reproducibility is preserved at the command level even
    though the lock can't see this install."""
    flags = list(flags or [])
    spec = f"{name}=={version}" if version else name
    flag_str = " ".join(shlex.quote(f) for f in flags)
    # `python -m pip install` (not bare `pip install`) because pixi/uv envs
    # don't always put a `pip` binary on PATH — engine pip uses uv. The module
    # form (P4 fix, verification-driven 2026-05-27) works whenever both python
    # AND the pip module are in the env (env_freeze.ensure_python_for_pip
    # declares both when pip_with_flags is non-empty).
    cmd = f"python -m pip install {flag_str} {shlex.quote(spec)}".strip()
    # collapse the double-space when flag_str is empty (defensive — caller
    # should not pass empty flags, but we don't want a malformed command if so)
    cmd = " ".join(cmd.split())
    # Dist-metadata probe (same shape as env_freeze._pip_presence_check): robust
    # to import-name mismatches like pyyaml→yaml. Anchored on the package name
    # so the env_honesty shape rule (word-boundary tool-token) still binds.
    ev = evidence or (
        f"python -c \"import importlib.metadata as _m; _m.version({name!r})\""
    )
    return {"command": cmd, "evidence": ev, "tool": name,
            "purpose": f"{name} (pip with flags)", "engine_coupled": True}


def spack(name: str, *, package: str = "", spack_ref: str = "v0.22.1",
          evidence: str = "") -> dict[str, Any]:
    """Spack package (the HPC from-source registry, thousands of curated recipes).
    Bootstraps Spack with its STORE under /opt/tools/spack (the slim multi-stage
    runtime COPYs /opt/tools, so the dep-closure store dirs ship at the EXACT paths
    the binaries' RPATHs point to — the relocation crux, verified by spike). Uses the
    build container's system gcc (`spack compiler find` — never builds a compiler),
    `spack gc` trims build-only deps to the runtime closure, the tool's bins are
    symlinked onto /usr/local/bin, and Spack's own source is dropped so only the
    store ships. From-source ⇒ can't be wrong-arch; presence + the in-image evidence
    are the anchors.

    `spack_ref` pins Spack (default v0.22.1 — builtin packages are in-repo there;
    v1.0 split them out). NOTE: from-source builds are slow; this tier is practical
    on a NATIVE amd64 host (a dep closure under emulation is pathologically slow).
    Pass a real `evidence` that RUNS the tool — `command -v` alone won't catch a
    mis-relocated binary that's on PATH but can't load its libs."""
    pkg = package or name
    root = "/opt/tools/spack"
    parts = [
        "command -v python3 >/dev/null 2>&1 || { apt-get update && "
        "apt-get install -y --no-install-recommends python3 && rm -rf /var/lib/apt/lists/*; }",
        f"git clone --depth 1 --branch {shlex.quote(spack_ref)} https://github.com/spack/spack {root}",
        f"export SPACK_ROOT={root}",                 # required under Rosetta; harmless native
        f". {root}/share/spack/setup-env.sh",
        "spack compiler find",
        f"spack install --fail-fast {shlex.quote(pkg)}",
        "spack gc -y || true",                       # trim build-only deps to the runtime closure
        f'P="$(spack location -i {shlex.quote(pkg)})"; ln -sf "$P"/bin/* /usr/local/bin/',
        # slim: keep the store (opt/), drop Spack's own source/python so it never ships
        f"find {root} -maxdepth 1 -mindepth 1 ! -name opt -exec rm -rf {{}} +",
    ]
    cmd = "set -eux; " + "; ".join(parts)
    return {"command": cmd, "evidence": evidence or f"command -v {name}", "tool": name,
            "purpose": f"{name} (spack {spack_ref})"}


def synthesized(name: str, commands: list[dict], *, tool: str = "", evidence: str = "",
                engine_coupled: bool = False, repo: str = "", commit: str = "") -> dict[str, Any]:
    """The UNIVERSAL long-tail installer — the ONE shape the bespoke tail (compiled
    source / run-by-path script / release binary / jar / half-baked) collapses into.
    Instead of enumerating a generator per tool, the AGENT reads the tool's own
    build files and submits a VALIDATED, provenance-tagged command sequence; this
    runs it VERBATIM in the build container (joined under `set -eux`, one shell so
    `cd` persists) and proves it with a single `evidence` check. Safe because every
    command was gated by provenance + grounding (synthesis.validate_submission) and
    the result is gated by the honesty contract (the tool must run in the shipped
    image). `commands` = [{command, provenance, engine_coupled?}]; the per-command
    provenance rides into the recipe (the longtail) for audit + verify-by-rebuild.

    If ANY command is engine-coupled (built against an engine toolchain), the whole
    sequence runs with the engine env active — harmless for the plain steps."""
    seq = [c["command"] for c in commands if c.get("command")]
    cmd = "set -eux; " + "; ".join(seq)
    wrap = tool or name
    coupled = engine_coupled or any(c.get("engine_coupled") for c in commands)
    return {"command": cmd, "evidence": evidence or f"command -v {wrap}", "tool": wrap,
            "purpose": f"{name} (synthesized @ {(commit or 'HEAD')[:12]})",
            "engine_coupled": coupled,
            "provenance": {"source": "synthesized", "repo": repo, "commit": commit,
                           "commands": [{"command": c.get("command"),
                                         "provenance": c.get("provenance")} for c in commands]}}


def script_repo(name: str, repo_url: str, *, ref: str = "", script_rel: str = "",
                interpreter: str = "", build_command: str = "",
                wrapper: str = "", evidence: str = "") -> dict[str, Any]:
    """Clone-and-run script repo, with optional in-image build step.

    Three shapes, one generator:
      1. PURE RUN-BY-PATH (academic norm): a Python/Perl script collection,
         no packaging. Clone → chmod the entry script → /usr/local/bin
         wrapper that execs it (optionally via `interpreter`). No build.
      2. BUILD + SCRIPT-ENTRY (N2, batch-3): a project that builds compiled
         assets but is invoked through a script entrypoint (yarn-PnP Node:
         `yarn install && yarn build` then `node dist/main.js`; pip-editable
         + module: `pip install -e .` then `python -m foo`). Pass
         `build_command` (runs at the clone dir) AND `script_rel` +
         `interpreter`. The wrapper runs the entrypoint AFTER the build has
         produced its artifacts.
      3. (existing) PURE BUILD (no entrypoint, no script_rel): use the
         `source` generator instead — it writes a wrapper around the
         built binary at `bin_path`.

    Default evidence is wrapper-smoke: `{wrap} --help || {wrap} --version
    || {wrap} -h || command -v {wrap}`. The plain `command -v` fallback used
    to be the ONLY evidence; it passed when the wrapper file existed even
    if executing it crashed (N3, batch-3 Apollo3: wrapper existed, but
    `dist/main.js` had never been built, so any actual invocation crashed
    with MODULE_NOT_FOUND — VALIDATED_IN_IMAGE said pass anyway). Try the
    real invocation first; fall back to `command -v` only when none of the
    standard help/version flags exist (the truly-no-args-no-help case)."""
    clone = f"{_TOOLS}/{name}"
    wrap = wrapper or name
    entry = f"{clone}/{script_rel}" if script_rel else f"{clone}/{name}"
    # SWH-fallback clone (see `source` for the contract).
    parts = [f"_swh_clone {shlex.quote(repo_url)} {shlex.quote(ref)} {clone}",
             f"cd {clone}"]
    if ref:
        parts.append(f"git checkout {shlex.quote(ref)}")
    # OPTIONAL build (N2): runs at the clone dir before the wrapper is written,
    # so the wrapper points at assets the build actually produced.
    if build_command:
        parts.append(build_command)
    parts.append(f"chmod +x {entry} 2>/dev/null || true")
    runline = f"{interpreter} {entry}".strip()
    parts.append(f"printf '#!/bin/sh\\nexec {runline} \"$@\"\\n' > /usr/local/bin/{wrap}")
    parts.append(f"chmod +x /usr/local/bin/{wrap}")
    cmd = "set -eux; " + "; ".join(parts)
    # N3 (batch-3): wrapper-smoke evidence — actually INVOKE the wrapper to
    # prove it runs, not just that the file exists. Mirrors release_binary's
    # default. Multiple flag tries so we handle CLIs that use --help vs -h vs
    # --version, and CLIs that exit non-zero on `--help` (some do; the >/dev/
    # null + chain on || tolerates either exit code as long as exec didn't
    # crash before the binary started).
    ev = evidence or (
        f"{wrap} --help >/dev/null 2>&1 || {wrap} --version >/dev/null 2>&1 || "
        f"{wrap} -h >/dev/null 2>&1 || command -v {wrap}"
    )
    # ENGINE COUPLING (Talos, batch-N): a run-by-path repo whose interpreter is a
    # CONDA-PROVIDED language runtime (python/Rscript/perl) needs the conda env
    # ACTIVE for BOTH the build (`pip install -e .` — pip/python live in the env,
    # not on the builder's base PATH) AND the wrapper-smoke evidence (`python
    # entry` — same). Without this the build dies `pip: command not found`. The
    # coupling is applied by container_build.run() (wraps in `pixi run bash -c`),
    # correct in the build container AND when the longtail step is baked. node/
    # yarn (Apollo3) and make/gcc (C tools via the `source` gen) are system
    # toolchains → stay UNcoupled (still on PATH inside/outside pixi run anyway).
    engine_coupled = interpreter in {"python", "python3", "Rscript", "perl"}
    return {"command": cmd, "evidence": ev, "tool": wrap,
            "purpose": f"{name} (script repo @ {ref or 'HEAD'})",
            "engine_coupled": engine_coupled}
