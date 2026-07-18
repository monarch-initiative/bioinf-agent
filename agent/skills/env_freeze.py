"""
env_freeze — the container-native freeze: a spec/draft → a shipped env IMAGE.

This is the C3 teardown bridge that retires the host recipe zoo. The OLD path
(docker_builder.build_recipe + _recipe_dockerfile + nine `_emit_*`) translated a
spec's typed install_method records back into Dockerfile RUN steps for the host-
REPLAY locus (conda at /opt/conda, rustup/go-tarball, conda-activate). This module
instead routes each install_method to the SINGLE source of per-tier knowledge —
the install_commands generators — and builds on the container-native locus
(EnvBuild: install + validate IN the ship image, one generic bake, validated==
shipped). One emitter, not nine; the per-tier knowledge lives once (install_commands).

Toolchain-coupled tiers (cargo/go/perl) build with the ENGINE's toolchain, so the
required toolchain conda specs are injected automatically (rust / go / perl +
perl-app-cpanminus + c-/cxx-compiler for XS) — the container-native replacement
for the host path's rustup / go-tarball / conda-builddeps emitters.

`_map_install` (per-tier record → generator spec) and `plan_conda` (toolchain
injection) are pure given injected network fns, so they unit-test without docker or
the network. `build_env_image` drives a real ContainerBuild and is live-proven.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any, Callable, Optional

from agent.skills import freeze as _freeze
from agent.skills import install_commands as ic
from agent.skills import resolver as _resolver
from agent.skills.env_build import EnvBuild
from agent.skills.outcomes import refused, broke

# coupled tiers → the engine toolchain conda specs they need to BUILD in-container.
# (pip is NOT here — it's declared through the engine directly via add_pip, not a
# long-tail generator; it needs no extra toolchain.)
_TOOLCHAIN_SPECS = {
    "cargo":     ["rust"],
    "go":        ["go"],
    "perl":      ["perl", "perl-app-cpanminus", "c-compiler", "cxx-compiler"],
    # zlib is a near-universal #include for Bioc/CRAN packages that touch compressed
    # data (snpStats: -lz for read_uncertain.c; many htslib-adjacent R packages).
    # The shipped conda r-base links against zlib already, but the dev headers ship
    # in `zlib` (conda-forge) — install both so source-compiled R packages find them.
    "r_install": ["r-base", "c-compiler", "cxx-compiler", "fortran-compiler", "zlib"],
}


def _pip_presence_check(name: str) -> str:
    """In-image presence for a pip dist via python's metadata — NOT `pip show`
    (pixi --pypi uses uv, so the env ships no pip binary), and dist-name based
    (robust to an import-name mismatch like pyyaml->yaml)."""
    return f"python -c \"import importlib.metadata as _m; _m.version('{name}')\""


def _r_presence_check(conda_name: str) -> str:
    """In-image presence for an R conda package (bioconductor-* / r-*) via its
    library name in R's installed.packages(). The conda channel lowercases the
    name (`bioconductor-deseq2`) but the canonical R library preserves case
    (`DESeq2`); case-sensitive requireNamespace would fail on a healthy install.
    ignore.case=TRUE on installed.packages() rownames is the robust check — we
    only care THAT the package is installed; the case-canonical form is the R
    library's own. Evidence_shape's prefix-stripping (`bioconductor-` / `r-`)
    means the shape rule sees `deseq2` as the tool token, which is exactly what
    the grep references."""
    lib = conda_name
    for pre in ("bioconductor-", "r-"):
        if conda_name.startswith(pre):
            lib = conda_name[len(pre):]
            break
    expr = (f"q(status=as.integer(length(grep('^{lib}$', "
            f"rownames(installed.packages()), ignore.case=TRUE)) == 0))")
    return f'Rscript -e "{expr}"'


def _conda_pkg_bin_check_sh(name: str) -> str:
    """A SUBSHELL expression that finds the FIRST `bin/*` file installed by
    the conda package `name` (via its `conda-meta/{name}-*.json` record),
    tests that the basename is on PATH, exits 0 on hit. Returned as a
    `(...)` group so it composes via `||` inside an outer evidence chain
    without bleeding `exit` to the parent.

    The bug this fixes (N1, batch-3 Apollo3 stress): `conda install nodejs`
    delivers a binary named `node` (not `nodejs`); `conda install mongodb`
    delivers `mongod`; openjdk→java, mysql→mysqld, postgresql→postgres,
    python→python3. The generic `command -v {pkg}` probe can't find these,
    and most don't write python dist-metadata either, so the .ENV.html row
    reported VALIDATED_IN_IMAGE.evidence_passed=False even though the
    package was healthily installed.

    Re-parse-safety (the original sh -c wrap broke this):
      Pre-fix the body was wrapped in `sh -c '<body>'`. Production invokes
      evidence inside another `bash -c "...; <evidence>; ..."` (see
      container_build.validate_in_image), so bash re-parsed the outer
      string. The body's internal single-quoted sed pattern
      (`'s|...|p'`) closed bash's outer single-quote mid-body, leaving
      sed's `(`/`)`/`|`/`*` characters UNQUOTED → `syntax error near
      unexpected token ')'`. The probe shipped in C4 but was DOA in
      production until this fix. Found by tests/integration/
      test_n1_conda_meta_probe_docker.py (Round 2, May 2026).

      The fix drops the `sh -c '...'` wrap and uses a plain bash subshell
      `(...)`. The body is fine bash code as-is — the sed pattern's inner
      `'...'` works because there is no nesting; the outer bash is the
      ONLY shell parsing the string. The subshell scopes `exit 0`/`exit 1`
      so the probe still short-circuits the `||` chain without killing
      the parent.

    The probe reads the conda-meta JSON's `files: [\"bin/X\", \"lib/Y\", …]`
    list (which conda writes at install time and never removes) and asks
    `command -v` on the first bin/ basename. Shell-only — no python
    dependency in the image (mongodb-/nodejs-only envs typically don't ship
    one). The package's NAME appears as a word-boundary token in the
    `conda-meta/{name}-*.json` glob, satisfying env_honesty.evidence_shape's
    anchor rule.

    Looks under `/opt/conda/envs/*/conda-meta/` (pixi-managed envs),
    `/opt/conda/conda-meta/` (base-env installs) AND `/usr/local/conda-meta/`
    (BioContainers' prefix) so it tolerates every layout we ship or adopt. The
    conda-meta JSON is well-formed enough that a `sed -nE` over `\"bin/...\"`
    strings finds the bin entries reliably.

    `/usr/local` is load-bearing for the ADOPT path: a published BioContainer
    installs its conda prefix at /usr/local, NOT /opt/conda. Without that glob the
    probe cannot see any adopted package whose binary name differs from its package
    name, and gating adopt on it false-refuses healthy envs — measured on real
    images: gatk4 rc=1, htslib rc=127, perl-bioperl rc=1, all perfectly fine
    packages (audit 2026-07-16 Tier 2)."""
    # Conda package names use a-z 0-9 - . _ — no shell metachars; safe to
    # interpolate directly into the subshell body.
    body = (
        f'for f in /opt/conda/envs/*/conda-meta/{name}-*.json '
        f'/opt/conda/conda-meta/{name}-*.json '
        f'/usr/local/conda-meta/{name}-*.json; do '
        f'[ -e "$f" ] || continue; '
        f'for b in $(sed -nE \'s|.*"bin/([^"/]+)".*|\\1|p\' "$f" | sort -u); do '
        f'command -v "$b" >/dev/null 2>&1 && exit 0; '
        f'done; '
        f'done; '
        f'exit 1'
    )
    return f"( {body} )"


def _perl_module_check_sh(name: str) -> str:
    """A SUBSHELL that LOADS the Perl module a `perl-*` conda package installs.

    A pure Perl module library (perl-bio-db-hts) ships NO bin/ entry, so neither
    `command -v perl-bio-db-hts` nor the conda-meta bin probe can ever find it — the
    package is healthy and the probe says rc=1. Gating adopt on that false-refuses an env
    CLAUDE.md itself routes to conda ("Prefer conda when the module is on bioconda (e.g.
    perl-bio-db-hts…)").

    The module NAME is not derivable from the package name — `perl-bio-db-hts` capitalizes
    to `Bio::DB::HTS`, which no mechanical rule produces (Bio::Db::Hts is wrong). So we
    don't guess: we read the .pm paths out of the package's OWN conda-meta record
    (`lib/perl5/5.32/site_perl/Bio/DB/HTS.pm`) and turn the path into the module
    (`Bio::DB::HTS`). The package tells us its own module names; we just ask.

    Bounded to the first few modules — bioperl ships hundreds, and one successful load is
    the proof we need. The package name appears as a word-boundary token in the
    conda-meta glob, so env_honesty's evidence_shape anchor rule is satisfied."""
    body = (
        f'for f in /usr/local/conda-meta/{name}-*.json '
        f'/opt/conda/envs/*/conda-meta/{name}-*.json '
        f'/opt/conda/conda-meta/{name}-*.json; do '
        f'[ -e "$f" ] || continue; '
        f'for m in $(sed -nE \'s|.*"lib/perl5/[^"]*site_perl/([A-Za-z][^"]*)\\.pm".*|\\1|p\' "$f" '
        f'| sed \'s|/|::|g\' | sort -u | head -5); do '
        f'perl -M"$m" -e1 >/dev/null 2>&1 && exit 0; '
        f'done; '
        f'done; '
        f'exit 1'
    )
    return f"( {body} )"


def _conda_presence_check(name: str) -> str:
    """In-image presence for a conda tool — honesty-ordered:

      1. `command -v {name}` — the trivial case (samtools→samtools, bwa→bwa).
         Short-circuits on hit so a pure-CLI env doesn't need any of below.
      2. conda-meta bin probe (N1, batch-3): for conda packages where the
         binary name differs from the package name (mongodb→mongod,
         nodejs→node, openjdk→java, mysql→mysqld, postgresql→postgres,
         python→python3, …). Reads the package's conda-meta JSON `files:`
         list and tests the first bin/* basename is on PATH. Shell-only,
         no python in the env required.
      3. `python -c '_m.distribution(name)'` — the python dist-metadata
         fallback. Covers library-only conda packages that ship NO binary
         (numpy, scipy, networkx, python-louvain): nothing on PATH, nothing
         in conda-meta/bin, but importable as a python distribution.

    All three clauses reference `{name}` as a word-boundary token (the
    conda-meta glob `{name}-*.json` anchors clause 2), so env_honesty's
    evidence_shape rule accepts the chain without weakening — a probe that
    passes on a clean image can only pass if `name` is genuinely installed.

    R-conda-package detour (R5, batch-2 stress): `bioconductor-*` / `r-*`
    packages are R libraries — none of the three clauses can find them.
    Route those to the R-aware Rscript installed.packages() check first."""
    if name.startswith("bioconductor-") or name.startswith("r-"):
        return _r_presence_check(name)
    if name.startswith("perl-"):
        # Same detour as R, same reason: a `perl-*` package is a Perl module library and
        # the three generic clauses cannot see one that ships no binary. The module probe
        # goes LAST so a perl package that DOES ship a binary (perl-bioperl) still
        # short-circuits on the cheap clauses.
        return (
            f"command -v {name} || "
            f"{_conda_pkg_bin_check_sh(name)} || "
            f"{_perl_module_check_sh(name)}"
        )
    return (
        f"command -v {name} || "
        f"{_conda_pkg_bin_check_sh(name)} || "
        f"python -c \"import importlib.metadata as _m; _m.distribution('{name}')\""
    )


def _r_source_from_method(im: dict) -> str:
    """Reconstruct install_r_package's source arg (cran|bioconductor|github:owner/
    repo) from the recorded install_method.source R expression."""
    expr = im.get("source", "") or ""
    m = re.search(r"install_github\(['\"]([^'\"]+)['\"]", expr)
    if m:
        return f"github:{m.group(1)}"
    if "BiocManager" in expr:
        return "bioconductor"
    return "cran"


def _is_commit_sha(s: str) -> bool:
    """A full 40-hex git commit — an immutable content address (a rebuild at this
    ref reproduces the exact source tree). Tags/branches are NOT (they move)."""
    s = (s or "").strip().lower()
    return len(s) == 40 and all(c in "0123456789abcdef" for c in s)


def _replay_assurance(tier: str, im: dict) -> tuple[str, bool]:
    """The honest per-tier SHIP assurance (C5) → `(assurance, verified)`.

    HOW is each shipped non-conda tool anchored? `verified=True` ONLY for an
    IMMUTABLE content anchor a rebuild reproduces (a pinned git commit, an immutable
    registry version compiled in-image, a hash-locked package). Everything else
    ships VALID but DISCLOSED-unverified with its reason — so the ENV report can
    never imply uniform trust across tiers (a source tool on a floating branch,
    which DRIFTS, must not look like a digest-pinned one).

    binary + jar set their own provenance inline in `_map_install` (they carry a
    runtime-fetched sha the F1/F2 chain reasons over); this helper is the single
    source of truth for the purely-static tiers. Conda (adopt/build) and flagless
    pip are IMAGE-level anchors (manifest digest / engine lock) disclosed at the
    image, not per-tool, so they are not routed here."""
    im = im or {}
    if tier == "source":
        ref = im.get("commit_sha") or im.get("ref") or ""
        if _is_commit_sha(ref):
            return "commit_pinned", True
        return ("ref_pinned_tofu", False) if ref.strip() else ("unpinned", False)
    if tier == "synthesized":
        return ("commit_pinned", True) if _is_commit_sha(im.get("commit_sha") or "") \
            else ("unpinned", False)
    if tier in ("cargo", "go"):
        ver = (im.get("version") or "").strip().lower()
        return ("built_pinned", True) if (ver and ver != "latest") \
            else ("built_unpinned", False)
    if tier == "perl":
        return "cpan_tofu", False
    if tier == "r_install":
        src = im.get("source") or ""
        ref = im.get("commit_sha") or im.get("ref") or ""
        if src.startswith("github:") and _is_commit_sha(ref):
            return "commit_pinned", True
        return "repo_tofu", False
    if tier == "pip":   # only flag-bearing pip reaches a longtail step (flagless → engine lock)
        return "command_pinned", False
    return "unanchored", False


def _map_install(
    x: dict,
    platform: str = "linux/amd64",
    *,
    resolve_linux_asset: Callable[..., dict] = _resolver.resolve_linux_asset,
    sha256_of_url: Callable[..., dict] = _resolver.sha256_of_url,
) -> dict[str, Any]:
    """Map a non-conda install to a generator spec AND attach its per-tool SHIP
    assurance (C5). binary + jar set provenance inline (they carry a runtime sha);
    this wrapper fills every other tier via `_replay_assurance` so the shipped
    record — and the ENV report rendered from it — discloses HOW each tool is
    anchored across ALL tiers, never implying uniform trust. Delegates the actual
    mapping to `_map_install_spec`."""
    m = _map_install_spec(x, platform, resolve_linux_asset=resolve_linux_asset,
                          sha256_of_url=sha256_of_url)
    spec = m.get("spec")
    if isinstance(spec, dict) and "provenance" not in spec:
        a, v = _replay_assurance(x.get("type", ""), x.get("install_method") or {})
        spec["provenance"] = {"tier": x.get("type", ""), "verified": v, "assurance": a}
    return m


def _map_install_spec(
    x: dict,
    platform: str = "linux/amd64",
    *,
    resolve_linux_asset: Callable[..., dict] = _resolver.resolve_linux_asset,
    sha256_of_url: Callable[..., dict] = _resolver.sha256_of_url,
) -> dict[str, Any]:
    """Map ONE non-conda install_method record to an install_commands generator
    spec (the thing EnvBuild.add_tool consumes). Returns {"spec": <gen>} or
    {"error": <reason>} for a non-replayable record. Network (binary asset
    resolution) is injected so the mapping is unit-testable."""
    name = x.get("name", "")
    t = x.get("type", "")
    im = x.get("install_method") if isinstance(x.get("install_method"), dict) else {}

    if t == "jar":
        jar_url = im.get("source") or im.get("jar_url")
        if not jar_url:
            return refused("build.jar_no_url", error=f"jar tool '{name}' has no jar_url to replay")
        hh = sha256_of_url(jar_url)              # jars rarely publish a checksum; best-effort
        jar_sha = hh.get("sha256", "") if hh.get("ok") else ""
        gen = ic.jar(name, jar_url, sha256=jar_sha,
                     java_flags=im.get("java_flags"), wrapper=name)
        # C5: a jar is TOFU at best — no publisher auth. We pin the bytes we ship
        # (pinned_tofu) when the freeze re-fetch hashed, else unanchored. Never verified.
        gen["provenance"] = {"tier": "jar", "verified": False,
                             "assurance": "pinned_tofu" if jar_sha else "unanchored",
                             "asset_url": jar_url, "asset_sha256": jar_sha or None}
        return {"spec": gen}

    if t == "source":
        # Three replay shapes (mutually exclusive, all routed through this branch):
        #   1. ENTRYPOINT-ONLY  → script_repo (clone + wrapper; no build)
        #   2. ENTRYPOINT + BUILD → script_repo with build_command (N2, batch-3:
        #      yarn-PnP Node, pip-install-editable + python -m — needs the build
        #      AND wraps an interpreter+script invocation)
        #   3. BUILD + BIN_PATH → source (clone + build + wrapper around the built
        #      compiled binary)
        if im.get("entrypoint"):
            return {"spec": ic.script_repo(
                name, im.get("source") or "",
                ref=im.get("commit_sha") or im.get("ref") or "",
                script_rel=im.get("entrypoint"),
                interpreter=im.get("interpreter") or "",
                build_command=im.get("build_command") or "",   # N2: optional in-image build
                # Prefer the agent's recorded smoke as the in-image evidence over the
                # default wrapper-smoke (`{tool} --help`). For an import-heavy python
                # entrypoint (Talos → cpg_flow+hail), `--help` runs minutes under QEMU
                # cross-arch and blows the evidence timeout; `import talos` is seconds.
                evidence=im.get("verify_command") or "",
                wrapper=name)}
        if not im.get("build_command") or not im.get("bin_path"):
            return refused("build.source_non_replayable",
                           error=f"source tool '{name}' is not replayable: install_method needs "
                             f"build_command + bin_path (compiled tool) OR entrypoint "
                             f"(run-by-path script repo, optionally with build_command "
                             f"for projects that build assets but run as a script) — "
                             f"re-run install_git_repo with one of those shapes.")
        return {"spec": ic.source(name, im.get("source") or "",
                                  ref=im.get("commit_sha") or im.get("ref") or "",
                                  build_command=im.get("build_command"),
                                  bin_path=im.get("bin_path"), wrapper=name)}

    if t == "synthesized":
        # The UNIVERSAL tail: a validated, provenance-tagged command sequence the
        # agent synthesized from the tool's OWN build files (gated at authoring by
        # synthesis.validate_submission). Replayed verbatim; no per-tool generator.
        cmds = im.get("commands") or []
        if not cmds:
            return refused("build.synthesized_no_commands",
                           error=f"synthesized tool '{name}' has no commands to replay")
        return {"spec": ic.synthesized(name, cmds, tool=im.get("tool") or name,
                                       evidence=im.get("evidence") or "",
                                       engine_coupled=im.get("engine_coupled", False),
                                       repo=im.get("source") or "",
                                       commit=im.get("commit_sha") or "")}

    if t == "cargo":
        return {"spec": ic.cargo(name, im.get("crate") or name, version=im.get("version") or "",
                                 git_url=im.get("git_url") or "",
                                 binary_name=im.get("binary_name") or name)}

    if t == "go":
        return {"spec": ic.go(name, im.get("package") or name, version=im.get("version") or "latest",
                              binary_name=im.get("binary_name") or name)}

    if t == "perl":
        module = im.get("module") or name
        dist = im.get("distribution") or (im.get("source") or "").replace("cpanm ", "").strip() or module
        return {"spec": ic.perl_cpanm(module, distribution=dist,
                                      cpanm_flags=im.get("cpanm_flags") or "--notest",
                                      build_env=im.get("build_env") or "")}

    if t == "r_install":
        # functional_evidence (when the install captured a self-contained functional
        # smoke) becomes the VALIDATED_IN_IMAGE evidence → freeze proves the package
        # RAN, not merely imported. Absent → ic.r_package's default import evidence.
        return {"spec": ic.r_package(name, source=_r_source_from_method(im),
                                     evidence=im.get("functional_evidence") or "")}

    if t == "binary":
        la = resolve_linux_asset(im.get("binary_url") or "")
        if not la.get("found"):
            return broke("build.binary_asset_unresolved",
                         error=f"could not resolve a {platform} asset for binary '{name}': "
                             f"{la.get('reason')}", available=la.get("available"))
        hh = sha256_of_url(la["url"])
        if not hh.get("ok"):
            return broke("build.binary_asset_hash_failed",
                         error=f"could not hash {platform} asset for '{name}': {hh.get('reason')}")
        freeze_hash  = (hh.get("sha256") or "").lower()
        install_url  = (im.get("binary_url") or "").strip()
        install_sha  = (im.get("asset_sha256") or "").strip().lower()
        # `same_asset`: freeze re-fetched the EXACT asset the install anchored.
        # The cross-platform bridge (darwin install → linux ship) resolves a
        # DIFFERENT asset — there is no install-time hash for it to compare to.
        same_asset   = bool(install_url) and la["url"] == install_url

        # ── INSTALL→SHIP INTEGRITY FIREWALL (F2) ────────────────────────────
        # When freeze re-fetches the SAME asset the install anchored, the bytes
        # MUST be unchanged. A mismatch means the release asset was mutated
        # between install and freeze (swapped upload / compromised mirror) —
        # and VALIDATED_IN_IMAGE would NOT catch it (a swapped binary runs fine).
        # Refuse: this is the only point the install→ship chain is checkable.
        if same_asset and install_sha and freeze_hash and freeze_hash != install_sha:
            return refused("build.binary_integrity_mismatch",
                           error=(f"binary '{name}': ship asset changed between install and "
                                  f"freeze — install anchored {install_sha}, freeze re-fetch is "
                                  f"{freeze_hash} (same URL {la['url']}). Refusing to ship a "
                                  f"mutated asset."),
                           url=la["url"], install_asset_sha256=install_sha,
                           freeze_asset_sha256=freeze_hash)

        # ── ASSURANCE DISCLOSURE (F1 / C5) ──────────────────────────────────
        # The shipped binary is `verified` ONLY when it was authenticated against
        # a publisher checksum that held from install through ship. Everything
        # else ships (valid) but is disclosed as unverified with its reason:
        #   authenticated       — publisher sha matched at install AND chain intact
        #   pinned_tofu         — no publisher sha, but same asset unchanged (TOFU)
        #   unanchored_x_platform — ship asset never installed (cross-arch bridge)
        #   unanchored          — same asset but install recorded no hash
        chain_intact  = same_asset and bool(install_sha) and freeze_hash == install_sha
        authenticated = bool(im.get("asset_authenticated"))
        if chain_intact and authenticated:
            assurance, verified = "authenticated", True
        elif chain_intact:
            assurance, verified = "pinned_tofu", False
        elif not same_asset:
            assurance, verified = "unanchored_cross_platform", False
        else:
            assurance, verified = "unanchored", False

        inner = PurePosixPath(im.get("local_path") or name).name
        gen = ic.release_binary(name, la["url"], sha256=freeze_hash,
                                binary_in_archive=inner, wrapper=name)
        gen["provenance"] = {"tier": "binary", "verified": verified,
                             "assurance": assurance, "asset_url": la["url"],
                             "asset_sha256": freeze_hash,
                             "install_asset_sha256": install_sha or None}
        return {"spec": gen}

    return refused("build.unknown_install_type",
                   error=f"install_method.type {t!r} for '{name}' has no container-native generator")


def plan_conda(conda_deps: list[str], non_conda: list[dict]) -> list[str]:
    """Conda specs for the env = the declared deps PLUS the engine toolchains the
    coupled tiers (cargo/go/perl) need to BUILD in-container. Pure. Order-stable,
    deduped (a toolchain already declared isn't doubled)."""
    have = set(conda_deps)
    extra: list[str] = []
    for x in non_conda:
        for spec in _TOOLCHAIN_SPECS.get(x.get("type", ""), []):
            if spec not in have:
                have.add(spec)
                extra.append(spec)
    return list(conda_deps) + extra


def ensure_python_for_pip(conda_specs: list[str], has_pip: bool,
                          *, has_flag_bearing_pip: bool = False) -> list[str]:
    """pip is installed via the engine's PyPI path (`pixi add --pypi` → uv), which
    needs a python interpreter IN the env. If pip specs are present but no conda
    package provides python, inject `python` — the pip analog of plan_conda's
    toolchain injection. (A bare `python` lets the solver choose; a transitively-
    provided python makes this a no-op via the explicit-match guard.)

    `has_flag_bearing_pip` (P4 fix, verification-driven 2026-05-27): when an env
    has flag-bearing pip installs (routed through install_commands.pip_install_
    with_flags as a long-tail tool, since `pixi add --pypi` doesn't honor pip
    flags), the long-tail command runs `python -m pip install <flags> <spec>`.
    That needs BOTH python AND the pip module in the env. pixi/uv envs do NOT
    auto-install `pip` (uv is used instead of pip for engine-routed installs),
    so we declare `pip` explicitly when at least one flag-bearing pip install
    is present. Pre-fix: `pip: command not found` inside the build container.
    """
    if not (has_pip or has_flag_bearing_pip):
        return conda_specs
    out = list(conda_specs)
    if not any(re.match(r"python($|[=<>!~ ])", s.strip()) for s in out):
        out.append("python")
    # For flag-bearing pip, declare `pip` itself — pixi/uv envs don't ship the
    # pip binary or the module by default; the long-tail's `python -m pip ...`
    # needs it.
    if has_flag_bearing_pip and not any(
        re.match(r"pip($|[=<>!~ ])", s.strip()) for s in out
    ):
        out.append("pip")
    return out


def build_env_image(
    spec: dict,
    *,
    name: str,
    version: str = "",
    conda_deps: Optional[list[str]] = None,
    channels: Optional[list[str]] = None,
    primary_tools: Optional[list[str]] = None,
    platform: str = "linux/amd64",
    accelerator: Optional[dict] = None,
    license_gated: bool = False,
    licenses: Optional[list[str]] = None,
    redistributable: bool = True,
    engine=None,
    conda_lock_files: Optional[dict[str, str]] = None,
    apt_snapshot: str = "",
) -> dict[str, Any]:
    """Build a ship-platform env IMAGE from a spec/draft, container-native. Routes
    each non-conda install to its generator, injects coupled toolchains, and runs
    EnvBuild (which is gated by env_honesty.check_build → BUILT/VALIDATED_IN_IMAGE/
    POLICY_CLEAN). `platform` is a DOCKER platform ('linux/amd64'). Returns the
    EnvBuild BuildResult (+ 'request_key'); refuses early on a non-replayable record.

    `conda_lock_files`: if provided (recipe-replay path), skip the conda/pip solve
    entirely and materialize the env from these prebaked lock files — same env
    bytes across time/machines, no drift from bioconda moving.

    `apt_snapshot`: UTC timestamp (e.g. "20260526T200000Z") pinning the apt layer
    to snapshot.debian.org at that moment. Empty on FRESH freeze → auto-generated
    as `now()` so the resulting recipe captures a timestamp going forward. The
    recipe-replay path passes the recipe's stored timestamp through verbatim."""
    if not apt_snapshot:
        # Fresh freeze: stamp the current UTC. Granularity to the second is more
        # than enough; snapshot.debian.org keeps every snapshot ever taken.
        from datetime import datetime, timezone
        apt_snapshot = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    conda_deps = conda_deps or []
    primary_tools = primary_tools or []
    non_conda = _freeze.non_conda_installs(spec)

    # pip splits THREE ways:
    #   1. FLAGLESS pip → declared THROUGH the engine (`pixi add --pypi`, in-lock).
    #      Default path; reproducible via the engine lock.
    #   2. FLAG-BEARING pip → routed as ENGINE-COUPLED LONG-TAIL (a literal
    #      `pip install <flags> <spec>` run after the engine layer materializes
    #      python). `pixi add --pypi` (uv) doesn't honor pip flags, so routing
    #      a `pip install --no-binary :all: pysam` through the engine silently
    #      swaps a wheel for the validated source compile — the pysam-stress
    #      P2 trust violation. By emitting a literal `pip install <flags>` we
    #      preserve install fidelity (the flags are honored) at the cost of
    #      lock representability for that one install (the command itself is
    #      the provenance, recorded in shipped_binaries / longtail_steps).
    #   3. (anything else) — non-conda non-pip: source/binary/jar/cargo/go/perl
    #      already handled via _map_install below.
    all_pip = [x for x in non_conda if x.get("type") == "pip"]

    def _pip_flags(x: dict) -> list[str]:
        f = (x.get("install_method") or {}).get("pip_flags") or []
        return list(f) if isinstance(f, list) else []

    pip_with_flags = [x for x in all_pip if _pip_flags(x)]
    pip_installs   = [x for x in all_pip if not _pip_flags(x)]
    tool_installs  = [x for x in non_conda if x.get("type") != "pip"]

    # A SOURCE install whose in-image build shells out to pip/python (a
    # git-vendored python package built `pip install -e .`, e.g. Talos) needs
    # `python` AND the `pip` binary IN the conda env — pixi/uv envs don't ship
    # pip by default (same gap ensure_python_for_pip already closes for the
    # flag-bearing pip tier). The script_repo generator marks these engine_coupled
    # so the build runs inside the activated env; without pip declared here that
    # activated env still lacks pip → `pip: command not found` at build.
    def _source_needs_pip(x: dict) -> bool:
        if x.get("type") != "source":
            return False
        im = x.get("install_method") or {}
        if (im.get("interpreter") or "") in ("python", "python3"):
            return True
        bc = im.get("build_command") or ""
        return bool(re.search(r"\b(pip|python|python3)\b", bc))
    source_needs_pip = any(_source_needs_pip(x) for x in tool_installs)

    # map every generator install up front so a non-replayable one fails BEFORE
    # we spin a build container.
    tool_specs = []
    for x in tool_installs:
        m = _map_install(x, platform)
        if m.get("outcome") in ("refused", "broke"):
            # Forward the mapper's failure. The boundary carries its OWN literal
            # code (per the outcomes re-wrap convention) but preserves the SPECIFIC
            # inner code as `inner_code` — the integrity firewall
            # build.binary_integrity_mismatch, asset-unresolved, non-replayable, …
            # — so a caller can still tell WHY it refused (the old single generic
            # code hid that, and wrongly forced `refused` even for a `broke` inner
            # like a network hash failure). Class-preserving: refused stays refused.
            fields = {**m, "success": False, "stage": "map_install",
                      "inner_code": m.get("code")}
            if m["outcome"] == "refused":
                return refused("build.map_install_refused", **fields)
            return broke("build.map_install_failed", **fields)
        tool_specs.append(m["spec"])   # provenance (C5 ship assurance) attached by _map_install

    # Append flag-bearing pip installs as engine-coupled long-tail tools (P2 fix).
    # Their versions come from the same installed_packages view as the engine pip
    # path so move-to-end dedup of retry attempts still resolves to the
    # last-successful version.
    _versions = {p.get("name"): p.get("version")
                 for p in _freeze.installed_packages(spec)
                 if isinstance(p, dict)}
    for x in pip_with_flags:
        nm = x.get("name", "")
        ver = _versions.get(nm) or ""
        gen = ic.pip_install_with_flags(nm, version=ver, flags=_pip_flags(x))
        # C5: flag-bearing pip is a literal command (the flags aren't lock-representable
        # — the command IS the provenance), so it ships disclosed-unverified.
        a, v = _replay_assurance("pip", x.get("install_method") or {})
        gen["provenance"] = {"tier": "pip", "verified": v, "assurance": a}
        tool_specs.append(gen)

    eb = EnvBuild(name, version, platform=platform, engine=engine, channels=channels,
                  accelerator=accelerator, license_gated=license_gated,
                  licenses=licenses, redistributable=redistributable,
                  prebaked_lock_files=conda_lock_files,
                  apt_snapshot=apt_snapshot)

    _base_conda = plan_conda(conda_deps, non_conda)
    if source_needs_pip:
        # A source build's python CEILING lives in its pyproject (e.g. Talos:
        # requires-python <3.12), INVISIBLE to the conda solver — left to inject a
        # bare `python`, the solver grabs the newest (3.14) and `pip install -e .`
        # dies "requires a different Python". freeze deliberately drops the
        # create_conda_env python as scaffolding (freeze.py), so PIN the injected
        # python to the version the env was VALIDATED on (validated == shipped).
        _py = str(spec.get("python_version") or "").strip()
        if _py and not any(re.match(r"python($|[=<>!~ ])", s.strip()) for s in _base_conda):
            _base_conda = [f"python={_py}"] + _base_conda
    all_conda = ensure_python_for_pip(
        _base_conda,
        bool(pip_installs),
        # source_needs_pip reuses the flag-bearing-pip path (inject python + pip)
        # so a `pip install -e .` source build has a real pip in the activated env.
        has_flag_bearing_pip=bool(pip_with_flags) or source_needs_pip,
    )
    if all_conda:
        non_conda_names = {x.get("name", "") for x in non_conda}
        verify = [(t, _conda_presence_check(t)) for t in primary_tools if t not in non_conda_names]
        eb.add_conda(all_conda, verify=verify)
    if pip_installs:
        # Move-to-end dedup in installed_packages means a successful pip retry's
        # entry supersedes a failed earlier attempt for the same package; the
        # version-with-real-data wins naturally without needing to filter by rc.
        versions = {p.get("name"): p.get("version")
                    for p in _freeze.installed_packages(spec)
                    if isinstance(p, dict)}
        pip_specs, pip_verify = [], []
        for x in pip_installs:
            nm = x.get("name", "")
            v = versions.get(nm)
            pip_specs.append(f"{nm}=={v}" if v else nm)
            # functional_evidence (when the install captured a functional smoke) is the
            # VALIDATED_IN_IMAGE verify → freeze proves the pkg RAN, not just imported.
            func = (x.get("install_method") or {}).get("functional_evidence")
            pip_verify.append((nm, func or _pip_presence_check(nm)))
        eb.add_pip(pip_specs, verify=pip_verify)
    for s in tool_specs:
        eb.add_tool(s)

    result = eb.run()
    result["request_key"] = eb.request_key()
    return result


