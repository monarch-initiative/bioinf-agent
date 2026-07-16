"""env_tools — research + install primitives + verify + raw install runner.

This is the install-side surface of the agent: one tool per *ecosystem*
(conda / git-source / spack / release-binary / perl / cargo / go / jar /
R / pip / pre-built BioContainer via synth) plus `search_package` and
`resolve_tool` for pre-install research, plus `run_install_command` for the
arbitrary-shell escape hatch when no primitive fits.

Each primitive auto-merges its install_step into the draft when
`pipeline_id` is supplied, so the install journey is captured
chronologically in `draft.install_steps[]`. See CLAUDE.md for the order +
when to reach for which primitive.

`_merge_simple_install` is shared across the single-package install tiers
(perl / cargo / go) — it records the install_step and caches the verify so
the derived PackageRecord satisfies I2. Helpers that live in mcp_server.py
(_shrink_stdio_for_response / _merge_simple_install / config) are reached
through `_ms.X` so test monkeypatches survive — same convention as
workflow_tools.py.
"""
from __future__ import annotations

import shlex
from typing import Annotated, Any, Optional

# IMPORT-BINDING: see workflow_tools.py — singletons go through `_ms.X`
# so test monkeypatching on mcp_server reaches us.
from agent import mcp_server as _ms
from agent.mcp_server import mcp, StrList, OptStrList  # never monkeypatched
from agent.skills.outcomes import proven, refused, broke


@mcp.tool()
def search_package(
    package_name: str,
    requested_version: str = "latest",
    pipeline_id: str = "",
) -> dict:
    """Search anaconda.org / bioconda / conda-forge / PyPI for a bioinformatics package.
    Returns channel, exact version, conda spec, install command, and brief description.

    `packages` in the final spec is rebuilt at finalize-time from the live
    conda env + install_steps' installed_packages (the source of truth),
    NOT from search_package. So this tool is query-only: use the returned
    versions/channels to choose what to actually install via
    `install_conda_packages` / `run_install_command`.

    If pipeline_id is supplied, description/homepage/input_types/output_types
    are cached on `draft.search_cache[package_name]` so the finalize pass can
    annotate the derived package record without re-querying."""
    result = _ms._pkg_search.search(package_name, requested_version)
    if pipeline_id and result.get("found"):
        name = result.get("package_name", package_name)
        cache_entry = {
            "description":   result.get("description"),
            "homepage":      result.get("home"),
            "input_types":   result.get("input_types", []),
            "output_types":  result.get("output_types", []),
            "check_command": result.get("check_command"),
        }
        cache_entry = {k: v for k, v in cache_entry.items() if v}
        ok = _ms._pipeline_state.cache_search_result(pipeline_id, name, cache_entry)
        result["pipeline_merge"] = (
            {"status": "cached", "pipeline_id": pipeline_id, "name": name}
            if ok else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result

# ---------------------------------------------------------------------------
# Environment management
# ---------------------------------------------------------------------------

@mcp.tool()
def resolve_tool(
    tool: str,
    version: str = "",
    github_repo: str = "",
    prefer: str = "",
    language: str = "",
) -> dict:
    """Decide WHICH install tier to use for `tool`, and record WHY (the
    ResolutionDecision). Probes availability independently per tier and ranks by:

        author_image > authors_recipe > conda > pip/cran/bioconductor
                     > binary > spack > synthesis > source > manual

    RELIABILITY GATE (authors-recipe-first). The two author tiers rank above conda but
    are GATED, not a fixed ladder — they only become available when the tool's OWN repo
    (explicit `github_repo`, or one extracted from pip/cran metadata) actually publishes
    an image, or when its own Dockerfile installs things a conda/pip reconstruction would
    silently DROP (compiled/vendored/system/binary deps). For a cleanly bioconda-packaged
    tool the gate stays SHUT and conda wins — conda is solver-managed, pinned, and
    containerizes small. When the gate fires, build what the authors build rather than
    reconstructing it: that is the Talos lesson (a pip reconstruction dropped a bcftools
    fork + htslib + echtvar and still imported clean).

    Returns {chosen, install_call, rationale, identity, alternatives, ambiguous, probed,
    …}: the concrete install primitive call to make, why it was chosen over the
    others, and the rejected-but-available alternatives. `install_call` names the real
    executor for the chosen tier — for the author tiers that is `freeze_from_image` /
    `build_env_from_authors_recipe`, which do the whole env (no separate freeze needed).
    If `probed` carries `authors_gate_error`, the gate FAILED to run (network/API) and the
    ranking below it is registry-only — it is not evidence the authors ship nothing.

    IDENTITY — READ THIS BEFORE INSTALLING. `identity` answers "is this registry entry the
    tool you MEANT?", which NOTHING else in this system checks. Every other gate verifies
    integrity (it builds, it runs, it ships as validated); a wrong-but-working tool passes
    all of them and ships green with a digest and an attestation. `identity.anchor` is one
    of: `bioconda-channel` / `bioconductor` (membership of a bio-only channel = identity,
    by construction) · `github_repo` (you named the repo and the metadata matches) · `repo`
    (repo-backed tier) · `domain-terms` (its self-description reads like bioinformatics) ·
    `none`. When `identity.confirmed` is false, `install_call` is PREFIXED with the reason
    and you must check `identity.self_description` — the package's own words — before
    running it. Two distinct cases: `reason='no_domain_signal'` means the entry describes
    something else (CRAN's `cellranger` parses spreadsheet cell ranges; PyPI's `talos`
    tunes Keras hyperparameters — neither is the tool a bioinformatician means), which is
    real evidence you have the wrong package; `reason='no_description'` means the tier
    published nothing to check against — missing evidence, not evidence of a problem.
    Pass `github_repo='owner/repo'` to resolve either.

    DISAMBIGUATION: bare tool names collide across registries (PyPI `ape` ≠
    CRAN's R `ape`). Pass `language` ('python'|'r') to restrict the search to one
    ecosystem; with no hint, a name found in both PyPI and CRAN comes back
    `ambiguous: true`. `prefer` forces a tier when available. `github_repo`
    ('owner/repo') unlocks the binary/source tiers AND is the strongest signal for the
    reliability gate — pass it whenever you know the tool's repo.

    Query-only: it does NOT install. Use the returned install_call with the
    matching primitive, then freeze() (except the author tiers, which freeze themselves).
    """
    return _ms._resolver.resolve(tool, version=version, github_repo=github_repo,
                             prefer=(prefer or None), language=language)


@mcp.tool()
def create_conda_env(
    env_name: str,
    python_version: str = "",
    pipeline_id: str = "",
    subdir: str = "",
) -> dict:
    """Create a new isolated conda environment.

    `subdir` pins the env's platform when a tool has no native build on the
    host arch — most commonly `subdir="osx-64"` on Apple Silicon for
    osx-64-only bioconda packages (plink, plink2, older C/C++ tools), which
    then run under Rosetta 2. The subdir is persisted to the env's .condarc so
    every subsequent install_conda_packages into this env honors it. Check
    search_package's `installable_on_current_platform` / `available_subdirs`
    first; if the host arch is missing but osx-64/linux-64 exists, create with
    that subdir.

    If pipeline_id is supplied, draft.conda_env and draft.python_version are
    set, and an entry is appended to draft.install_steps recording the env
    creation so the install journey is captured chronologically."""
    pv = python_version or _ms.config["conda"]["python_version"]
    result = _ms._env_mgr.create(env_name, python_version=pv, subdir=subdir or None)
    if pipeline_id:
        _ms._pipeline_state.set_conda_env(pipeline_id, env_name, python_version=pv)
        cmd = f"conda create --prefix envs/{env_name} python={pv}"
        if subdir:
            cmd = f"CONDA_SUBDIR={subdir} {cmd}  # + conda _ms.config --env --set subdir {subdir}"
        idx = _ms._pipeline_state.add_install_step(pipeline_id, {
            "tool":               "conda",
            "subcommand":         "create",
            "purpose":            f"Create the {env_name} conda environment" + (f" (subdir={subdir})" if subdir else ""),
            "command":            cmd,
            "returncode":         0 if result.get("success") else 1,
            "installed_packages": [{"name": "python", "version": pv, "channel": "conda-forge"}],
        })
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return _ms._shrink_stdio_for_response(result, label=f"conda.create.{env_name}")




@mcp.tool()
def install_conda_packages(
    env_name: str,
    packages: list[dict],
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install conda packages (bioconda / conda-forge / defaults) into a conda env.
    packages: list of {spec: str, channel: str}, e.g. [{spec: 'samtools=1.21', channel: 'bioconda'}]
    conda-pack is added automatically.

    If pipeline_id is supplied, an entry is appended to draft.install_steps with
    installed_packages parsed from each spec (spec='samtools=1.21' → name=samtools
    version=1.21). conda-pack is filtered out of installed_packages since it is
    infrastructure, not a user-facing package.

    Pass `step=N` to replace install_step N (for retries after a solver
    conflict, etc). Default is append — same semantics as run_install_command."""
    result = _ms._env_mgr.install(env_name, packages)
    if pipeline_id:
        # Record the env this installed into. install() auto-creates the env when
        # absent (a supported path), so an agent that skips create_conda_env would
        # otherwise leave draft.conda_env unset — breaking finalize + the guide.
        _ms._pipeline_state.set_conda_env(pipeline_id, env_name)
        from agent.skills.env_manager import parse_conda_spec
        installed: list[dict] = []
        for pkg in packages:
            parsed = parse_conda_spec(pkg.get("spec", ""))
            name = parsed["name"]
            if not name or name == "conda-pack":
                continue
            entry = {"name": name, "channel": pkg.get("channel", "")}
            if parsed["version"]:
                entry["version"] = parsed["version"]
            if parsed["constraint"] and parsed["constraint"] not in ("=", "=="):
                entry["version_constraint"] = parsed["constraint"]
            installed.append(entry)
        idx = _ms._pipeline_state.add_install_step(pipeline_id, {
            "tool":               "conda",
            "subcommand":         "install",
            "purpose":            f"Install {len(installed)} package(s) into {env_name}",
            "command":            "conda install " + " ".join(p.get("spec", "") for p in packages),
            "returncode":         result.get("returncode"),
            "installed_packages": installed,
        }, replace_step=step)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return _ms._shrink_stdio_for_response(result, label=f"conda.install.{env_name}")


@mcp.tool()
def install_git_repo(
    env_name: str,
    repo_url: str,
    tool_name: str,
    ref: str = "",
    build_command: str = "",
    verify_command: str = "",
    bin_path: str = "",
    entrypoint: str = "",
    interpreter: str = "",
    host_build: bool = True,
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Vendor a git repository as a source-installed tool (the clone-and-run
    pattern that conda/pip/jar primitives don't cover — e.g. an academic repo
    you run as `python run_thing.py …`).

    Two shapes: a COMPILED tool (pass `build_command` + `bin_path`, the relative
    path to the built executable) or a RUN-BY-PATH script collection (pass
    `entrypoint`, the repo-relative entry script, and `interpreter` e.g. `python`
    — the half-baked academic norm: no compiled binary). Either writes a PATH
    wrapper at {env}/bin/{tool_name}; freeze REPLAYS a compiled tool via the
    source generator and a run-by-path repo via the script_repo generator.

    Clones repo_url into {env}/share/{tool_name}, checks out `ref` (branch /
    tag / commit; default HEAD), resolves the commit SHA (the immutable content
    anchor), optionally runs `build_command` (e.g. `pip install -e .`, `make`)
    and a `verify_command` smoke test inside the env at the clone dir.

    If pipeline_id is supplied, records an install_step (tool=git,
    subcommand=clone) whose installed_packages entry carries
    install_method.type="source" with repo_url + commit_sha + local_path, and
    caches a verification (verify_command/verify_output) so the derived
    PackageRecord satisfies I2. The clone path + commit_sha are re-checked at
    finalize by I11 (source_install_present).

    `bin_path` (relative path to the built executable in the clone, e.g.
    "seqtk") makes a PATH wrapper at {env}/bin/{tool_name} and is recorded with
    build_command so freeze can REPLAY the build in the ship image. Omit for
    run-by-path script repos.

    `entrypoint` + `interpreter` is the SIBLING shape for run-by-path academic
    repos with no compiled binary (e.g. `HIC_ASSEMBLER/run_hicAssembler.py` run
    as `python …`). The wrapper at {env}/bin/{tool_name} execs the entry script
    via the named interpreter (`python`, `bash`, …); freeze's script_repo
    generator clones the same SHA in the ship image and writes the identical
    wrapper. Pass entrypoint AND interpreter together — omit both for compiled
    tools that use bin_path + build_command instead. The two shapes are
    mutually exclusive; install fails if both or neither are supplied.

    Pin `ref` to a tag or commit for reproducibility — a bare default branch
    drifts.

    `host_build=False` is the CROSS-ARCH escape: still clone + checkout +
    rev-parse on the host (cheap, the commit_sha anchor), but DEFER the build,
    bin_path existence check, wrapper write, and verify_command to freeze's
    in-container source-replay. Use when the host arch ≠ the freeze platform
    (e.g. x86 SIMD intrinsics under Apple Silicon clang on macOS). The shipped
    image is then the only place this tool runs — pair with
    `run_step_in_container` once frozen. The install_step still records
    build_command + bin_path + commit_sha so freeze's `_map_install` routes to
    the source generator.

    Returns: {success, clone_path, commit_sha, repo_url, ref, build_command,
    bin_path, entrypoint, interpreter, wrapper_path, verify_command,
    verify_output, host_build, log}.
    """
    result = _ms._env_mgr.install_git_repo(
        env_name       = env_name,
        repo_url       = repo_url,
        tool_name      = tool_name,
        ref            = ref,
        build_command  = build_command,
        verify_command = verify_command,
        bin_path       = bin_path,
        entrypoint     = entrypoint,
        interpreter    = interpreter,
        host_build     = host_build,
    )
    if pipeline_id:
        from urllib.parse import urlparse
        host = urlparse(repo_url).netloc or ""
        channel = "github" if "github.com" in host else "git"
        install_method = {
            "type":       "source",
            "source":     repo_url,
            "commit_sha": result.get("commit_sha"),
            "ref":        result.get("ref") or (ref or "HEAD"),
            "local_path": result.get("clone_path"),
            # Recorded so freeze can REPLAY the build on the ship platform.
            "build_command": build_command,
            "bin_path":      bin_path,
            # Run-by-path (script collection) replay fields — freeze routes these
            # to the script_repo generator instead of a compiled-source build.
            "entrypoint":    entrypoint,
            "interpreter":   interpreter,
            # The caller's smoke test (empty ⇒ freeze uses the wrapper-smoke default).
            # freeze re-runs THIS as its in-image VALIDATED_IN_IMAGE evidence — the
            # agent's chosen check is both more meaningful than a generic `--help`
            # AND lighter for import-heavy tools whose `--help` pulls a giant import
            # chain (Talos: `import talos` ~0.3s vs `talos --help` imports hail).
            "verify_command": verify_command,
        }
        ip_record = {
            "name":           tool_name,
            "channel":        channel,
            "source":         repo_url,
            "install_method": install_method,
        }
        if result.get("commit_sha"):
            ip_record["version"] = result["commit_sha"][:12]
        purpose = (
            f"Vendor {tool_name} from {host or repo_url} "
            + ("(source install, build deferred to freeze image — host_build=False)"
               if not host_build else "(source install)")
        )
        step_data = {
            "tool":        "git",
            "subcommand":  "clone",
            "purpose":     purpose,
            "command":     f"git clone {repo_url}" + (f" @ {ref}" if ref else ""),
            "returncode":  0 if result.get("success") else 1,
            "installed_packages": [ip_record],
        }
        idx = _ms._pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        # Cache the verify so the finalize-time package derivation attaches it
        # (mirrors verify_installation). Source tools don't go through the
        # registry-anchored verify() — their anchor is commit_sha + on-disk path.
        if result.get("success") and result.get("verify_output"):
            _ms._pipeline_state.cache_verification(pipeline_id, tool_name, {
                "verify_command": result.get("verify_command"),
                "verify_output":  result.get("verify_output"),
            })
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return _ms._shrink_stdio_for_response(result, label=f"git.{env_name}.{tool_name}")


@mcp.tool()
def synth_fetch(repo_url: str, ref: str = "", mode: str = "auto") -> dict:
    """Synthesis tier, call 1 of 2 — PROGRAMMATIC ground-truth fetch for a long-tail
    tool that has no conda/pip/cran/cargo/go/perl/binary/jar home. Acquires the
    source two ways (auto-detected, or force via `mode`='git'|'archive'):
      - GIT repo (any host — GitHub/GitLab/Bitbucket/self-hosted): clone, check out
        `ref` (tag/branch/commit; default HEAD), resolve the CONCRETE commit
        (`git rev-parse` — you never supply it).
      - non-git ARCHIVE (a raw release/vendor tarball or zip over http/ftp — the 'no
        GitHub' case): download, sha256-anchor, extract path-traversal-safely.
    Either way it returns the tool's OWN build files so you read them and decide how
    it installs — the universal residual path: no per-tool generator, you read the
    repo's real build instructions; provenance + the honesty contract make it safe.

    Returns {success, source_kind, files:[{path,sha256,text}], ranked_sources, ...}
    where the immutable anchor is `commit` (git) or `archive_sha256` (archive).
    `ranked_sources` orders the build files by how authoritative a recipe each is
    (Dockerfile › install.sh › CI workflow › Makefile/CMakeLists › setup.py/
    pyproject › README). READ them, then call synth_build with:
      - commands tagged source='extracted' (lifted VERBATIM from a file — pass its
        path as origin_file; PREFER this), or source='agent_authored' (composed
        from the repo's prose — every URL/remote you use MUST appear in the repo).
    synth_build re-fetches at the SAME anchor and re-verifies every command against
    these exact bytes, so an extraction you claim must really be in the file and an
    authored command must be grounded. Nothing you state from memory can ship."""
    fetch = _ms._env_mgr.fetch_build_source(
        repo_url, ref, mode=mode, is_relevant=_ms._synth.is_build_relevant)
    if not fetch.get("success"):
        return fetch
    paths = [f["path"] for f in fetch["files"]]
    fetch["ranked_sources"] = _ms._synth.rank_build_sources(paths)
    fetch["corpus_chars"] = sum(len(f.get("text", "")) for f in fetch["files"])
    return fetch


@mcp.tool()
def synth_build(
    env_name: str,
    repo_url: str,
    tool_name: str,
    commit: str = "",
    commands: list = [],
    evidence: str = "",
    ref: str = "",
    mode: str = "auto",
    archive_sha256: str = "",
    engine_coupled: bool = False,
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Synthesis tier, call 2 of 2 — record a VALIDATED, provenance-tagged install
    recipe for a long-tail tool. The UNIVERSAL residual installer: it replaces a
    per-tool generator with the agent reading the tool's own build files, gated so
    nothing is taken on faith. Works for a git repo OR a non-git archive (the anchor
    is `commit` for git, `archive_sha256` for an archive — pass whichever synth_fetch
    returned; `mode` mirrors synth_fetch).

    `commands` = the ordered install sequence, each:
      {command, source, origin_file?, engine_coupled?}
    where source is 'extracted' (the command occurs VERBATIM in origin_file — the
    file you name from synth_fetch; preferred) or 'agent_authored' (you composed it
    from the repo's prose — its URLs/remotes must appear in the repo).

    This RE-FETCHES at the SAME anchor (deterministic ground truth) and refuses if
    the anchor no longer matches (the source moved/changed), then validate_submission
    re-verifies EVERY command against the runtime's own bytes: an 'extracted' command
    must really occur in origin_file (sha256 stamped by the runtime, not you); an
    'agent_authored' command must be grounded. The source URL — and for an archive
    its sha256 — are ground truth by construction (we fetched exactly them), so a
    `git clone {url}` / `curl {url}` + `sha256sum -c` ground; every OTHER external
    reference must trace to the repo's files. Any violation → refused, no record. On
    success it records ONE install_method (type='synthesized') into the draft; freeze()
    replays it verbatim and the honesty contract proves the tool RUNS (`evidence`).

    NOTE — runtime SERVICES (MongoDB/Postgres/Redis/Spark) are NOT installed here:
    synthesis bakes the TOOL only (install-time); a service is provisioned per-run via
    start_service (Layer 2, I10). Keep synthesized commands install-time, not a
    long-running daemon.

    `evidence`: the in-image check that proves the tool works (must reference the
    tool as a real token — echo/true cheats are rejected by the contract). Default
    `command -v {tool_name}`. Returns {success, anchor, records, install_method,
    violations?, pipeline_merge?}."""
    fetch = _ms._env_mgr.fetch_build_source(
        repo_url, commit or ref, mode=mode, is_relevant=_ms._synth.is_build_relevant)
    if not fetch.get("success"):
        # merge (not kwargs) — `fetch` carries a `success` key (checked above) and
        # may carry `stage`; a dict-literal merge avoids a kwarg collision.
        return broke("install.synth_refetch_failed", **{**fetch, "stage": "refetch"})
    kind = fetch.get("source_kind", "git")
    # Anchor verification — the re-fetch must resolve the SAME immutable bytes.
    if kind == "git":
        if commit and fetch.get("commit") != commit:
            return refused("install.synth_commit_mismatch", success=False, stage="refetch",
                    error=f"re-fetch resolved {fetch.get('commit')!r}, expected {commit!r} "
                             f"— ref is not pinned to an immutable commit")
        anchor = {"commit_sha": fetch.get("commit"), "ref": ref or commit or "HEAD"}
        ground_extra = repo_url
        anchor_val = fetch.get("commit")
    else:  # archive
        if archive_sha256 and fetch.get("archive_sha256") != archive_sha256:
            return refused("install.synth_archive_sha_mismatch", success=False, stage="refetch",
                    error=f"re-download sha256 {fetch.get('archive_sha256')!r} != expected "
                             f"{archive_sha256!r} — the archive at that URL changed; re-run synth_fetch")
        anchor = {"archive_sha256": fetch.get("archive_sha256")}
        # the archive URL AND its sha256 are ground truth (we downloaded exactly it),
        # so a `curl {url}` + `sha256sum -c {sha}` grounds.
        ground_extra = repo_url + "\n" + (fetch.get("archive_sha256") or "")
        anchor_val = fetch.get("archive_sha256")
    fetch["corpus"] = _ms._synth.build_corpus(fetch["files"]) + "\n" + ground_extra
    val = _ms._synth.validate_submission(fetch, list(commands))
    if not val["ok"]:
        return refused("install.synth_provenance_violation", success=False,
                stage="validate_submission",
                violations=val["violations"],
                hint="an 'extracted' command must occur verbatim in its origin_file; "
                        "an 'agent_authored' command's URLs/remotes must appear in the repo "
                        "(the source URL and an archive's sha256 are auto-grounded)")
    records = val["records"]
    install_method = {
        "type":         "synthesized",
        "source":       repo_url,
        "tool":         tool_name,
        "evidence":     evidence or f"command -v {tool_name}",
        "engine_coupled": engine_coupled,
        "commands":     records,                       # validated, provenance-tagged
        "file_hashes":  {f["path"]: f["sha256"] for f in fetch["files"]},
        **anchor,
    }
    result: dict = proven("install.synth_recorded", success=True, source_kind=kind,
                          anchor=anchor_val, records=records, install_method=install_method)
    if pipeline_id:
        ip_record = {"name": tool_name, "channel": kind, "source": repo_url,
                     "install_method": install_method,
                     "version": (anchor_val or "")[:12]}
        step_data = {
            "tool": "git", "subcommand": "synthesize",
            "purpose": f"Synthesize {tool_name} from {repo_url} (agent-authored, gated)",
            "command": f"synth_build {tool_name} @ {(anchor_val or '')[:12]}",
            "returncode": 0, "installed_packages": [ip_record],
        }
        idx = _ms._pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id})
    return _ms._shrink_stdio_for_response(result, label=f"synth.{env_name}.{tool_name}")


@mcp.tool()
def install_spack_package(
    env_name: str,
    tool_name: str,
    package: str = "",
    spack_ref: str = "v0.22.1",
    evidence: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Declare a Spack package (the HPC from-source registry — thousands of curated
    community recipes) for a tool not on conda/pip/cran. A DECLARE primitive: it
    records the install_method (type='spack') into the draft; the actual build +
    validation happen at freeze() INSIDE the ship image (Spack on the host is
    impractical, and container-native is where 'validated == shipped' holds anyway).

    freeze() bootstraps Spack with its store under /opt/tools (so the dep-closure
    RPATHs resolve in the slim runtime), builds `package` (default = tool_name) from
    source with the build container's gcc, trims build-only deps, and symlinks the
    tool onto PATH — then the honesty contract proves the tool RUNS in the shipped
    image via `evidence`. NOTE: from-source builds are slow; this tier is practical
    on a NATIVE amd64 host. Pass an `evidence` that RUNS the tool (e.g.
    'samtools --version') — `command -v` alone can't catch a mis-relocated binary.
    `spack_ref` pins Spack (default v0.22.1; v1.0 split builtin packages out).

    Returns {success, install_method, pipeline_merge?}."""
    install_method = {
        "type":      "spack",
        "package":   package or tool_name,
        "spack_ref": spack_ref,
        "evidence":  evidence or f"command -v {tool_name}",
        "source":    f"spack:{package or tool_name}@{spack_ref}",
    }
    result: dict = proven("install.spack_declared", success=True, tool_name=tool_name,
                    install_method=install_method,
                    note="declared — Spack builds + validates at freeze() in the ship image "
                            "(best on a native amd64 host; from-source is slow under emulation)")
    if pipeline_id:
        ip_record = {"name": tool_name, "channel": "spack",
                     "source": install_method["source"], "install_method": install_method}
        step_data = {
            "tool": "spack", "subcommand": "install",
            "purpose": f"Declare {tool_name} via Spack ({package or tool_name}@{spack_ref})",
            "command": f"spack install {package or tool_name}",
            "returncode": 0, "installed_packages": [ip_record],
        }
        idx = _ms._pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id})
    return _ms._shrink_stdio_for_response(result, label=f"spack.{env_name}.{tool_name}")


@mcp.tool()
def install_release_binary(
    env_name: str,
    tool_name: str,
    url: str,
    sha256: str = "",
    binary_in_archive: str = "",
    wrapper_name: str = "",
    verify_command: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install a precompiled release binary (Tier-3): the static-binary pattern
    conda/pip/jar/source don't cover — mosdepth, somalier, slivar, sylph,
    dorado, cellranger. Downloads the asset, anchors it by sha256 (a mismatch is
    a HARD FAIL), extracts if it's a tar/zip, chmods the executable, and writes a
    PATH launcher at {env}/bin/{wrapper_name or tool_name}.

    sha256: pass the published checksum of the ASSET (the .tar.gz/.zip the
    publisher ships) to guarantee you got the exact download — verified before
    extraction and recorded as install_method.asset_sha256. The EXTRACTED binary
    is separately hashed into install_method.sha256 — that's the anchor I14
    re-hashes on disk at finalize (for an archive, the runnable binary differs
    from the tarball; for a single-binary download they coincide).

    binary_in_archive: for archive assets, the executable's path inside it
    (basename accepted); omit for single-binary downloads.

    A smoke verify runs after install ({tool} --version/--help by default, or
    your verify_command) — this is what catches a wrong-ARCHITECTURE binary that
    is present on disk (and so passes I14) but cannot execute. With pipeline_id,
    records an install_step (install_method.type="binary" + binary_url + sha256 +
    local_path) and caches the verify so the derived PackageRecord satisfies I2.

    Returns: {success, binary_path, wrapper_path, url, sha256, verify_command,
    verify_output, install_method, log}.
    """
    result = _ms._env_mgr.install_release_binary(
        env_name          = env_name,
        tool_name         = tool_name,
        url               = url,
        sha256            = sha256,
        binary_in_archive = binary_in_archive,
        wrapper_name      = wrapper_name,
    )
    if not result.get("success"):
        return result

    launcher = wrapper_name or tool_name
    vcmd = verify_command or (
        f"{launcher} --version 2>&1 || {launcher} --help 2>&1 || {launcher} -h 2>&1"
    )
    vres = _ms._env_mgr.run_in_env(env_name, vcmd, timeout=120)
    verify_ok = vres.get("returncode") == 0
    verify_output = ((vres.get("stdout") or "") + (vres.get("stderr") or "")).strip()[:200]
    result["verify_command"] = vcmd
    result["verify_output"]  = verify_output
    if not verify_ok:
        result["success"] = False
        result["verify_failed"] = True
        result["stderr"] = (result.get("stderr") or "") + (
            f"\n[verify failed: `{vcmd}` rc={vres.get('returncode')} — the binary is on disk "
            f"but did not execute; likely the wrong architecture/libc for this platform]"
        )

    if pipeline_id:
        install_method = result.get("install_method") or {
            "type": "binary", "binary_url": url,
            "sha256": result.get("sha256"), "local_path": result.get("binary_path"),
        }
        ip_record = {
            "name":           tool_name,
            "channel":        "binary",
            "source":         url,
            "install_method": install_method,
        }
        step_data = {
            "tool":        "curl",
            "subcommand":  "download",
            "purpose":     f"Install release binary {tool_name} from {url}",
            "command":     f"curl -L -o {tool_name} {url}",
            "returncode":  0 if result.get("success") else 1,
            "installed_packages": [ip_record],
        }
        if verify_ok:
            step_data["verify_command"] = vcmd
        idx = _ms._pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        if verify_ok and verify_output:
            _ms._pipeline_state.cache_verification(pipeline_id, tool_name, {
                "verify_command": vcmd,
                "verify_output":  verify_output,
            })
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return _ms._shrink_stdio_for_response(result, label=f"binary.{env_name}.{tool_name}")




@mcp.tool()
def install_perl_package(
    env_name: str,
    module: str,
    distribution: str = "",
    cpanm_flags: str = "",
    build_env: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install a Perl/CPAN module via cpanm (Tier: perl) — Ensembl VEP, BioPerl,
    and other Perl tools conda/pip don't cover. Requires perl + cpanm in the env
    (install conda packages `perl` and `perl-app-cpanminus` first).

    `module` is the Perl package name (e.g. Bio::DB::HTS) used for the
    `perl -M{module} -e1` load-or-die verify — the registry anchor for cpanm
    modules (tracked by neither conda nor pip). Set `distribution` when the CPAN
    distribution name differs from the module name. `build_env` = space-separated
    KEY=VAL exports for XS builds that link a conda C lib (e.g.
    "HTSLIB_DIR=$CONDA_PREFIX" for Bio::DB::HTS) — use $CONDA_PREFIX so freeze's
    recipe replay resolves it inside the SHIP image. With pipeline_id, records the
    install_step (install_method.type="perl", recorded for replay) + caches the
    verify (I2). freeze rebuilds the module from CPAN on the ship platform with a C
    toolchain + the conda layer's libs.

    Returns: {success, module, verify_command, verify_output, install_method, log}.
    """
    result = _ms._env_mgr.install_perl_package(env_name, module, distribution, cpanm_flags, build_env)
    if pipeline_id:
        result["pipeline_merge"] = _ms._merge_simple_install(
            pipeline_id, step, result, name=module, channel="cpan", tool="cpanm",
            command=f"cpanm {distribution or module}",
            purpose=f"Install Perl module {module}",
        )
    return _ms._shrink_stdio_for_response(result, label=f"perl.{env_name}.{module}")


@mcp.tool()
def install_cargo_tool(
    env_name: str,
    crate: str,
    version: str = "",
    binary_name: str = "",
    git_url: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install a Rust crate's binary via `cargo install` (Tier: cargo) — Rust
    tools not on bioconda. Requires rust+cargo in the env (conda: `rust`).
    Installs with --root {env} so the binary lands on the env PATH; `binary_name`
    (defaults to crate) is the cli_which anchor. `git_url` installs from a git
    repo instead of crates.io. Pin `version` for reproducibility.

    NOTE many Rust genomics tools (sylph, skani, sourmash) are ALSO on bioconda —
    prefer install_conda_packages when available; this is the fallback. With
    pipeline_id, records the install_step + caches the verify (I2).

    Returns: {success, crate, binary_name, verify_command, verify_output, install_method, log}.
    """
    result = _ms._env_mgr.install_cargo_tool(env_name, crate, version, binary_name, git_url)
    if pipeline_id:
        name = binary_name or crate
        result["pipeline_merge"] = _ms._merge_simple_install(
            pipeline_id, step, result, name=name, channel="cargo", tool="cargo",
            command=f"cargo install {git_url or crate}" + (f" --version {version}" if version else ""),
            purpose=f"Install Rust tool {name}",
        )
    return _ms._shrink_stdio_for_response(result, label=f"cargo.{env_name}.{crate}")


@mcp.tool()
def install_go_tool(
    env_name: str,
    package: str,
    version: str = "latest",
    binary_name: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install a Go tool via `go install pkg@version` (Tier: go) — Go tools not
    on bioconda. Requires go in the env (conda: `go`). Sets GOBIN={env}/bin so
    the binary lands on the env PATH; `binary_name` (defaults to the last path
    segment of `package`) is the cli_which anchor. Pin `version` (not "latest")
    for reproducibility. With pipeline_id, records the install_step + caches the
    verify (I2).

    Returns: {success, package, binary_name, verify_command, verify_output, install_method, log}.
    """
    result = _ms._env_mgr.install_go_tool(env_name, package, version, binary_name)
    if pipeline_id:
        name = binary_name or package.rstrip("/").split("/")[-1]
        result["pipeline_merge"] = _ms._merge_simple_install(
            pipeline_id, step, result, name=name, channel="go", tool="go",
            command=f"go install {package}@{version}",
            purpose=f"Install Go tool {name}",
        )
    return _ms._shrink_stdio_for_response(result, label=f"go.{env_name}.{package}")


@mcp.tool()
def install_jar_tool(
    env_name: str,
    tool_name: str,
    jar_url: str,
    java_flags: list[str] = [],
    wrapper_name: str = "",
    pipeline_id: str = "",
    step: int = 0,
) -> dict:
    """Install a Java JAR-based tool end-to-end (Exomiser, Picard, GATK, snpEff, …).

    The env must already have `openjdk` (and `unzip` if `jar_url` is a .zip).
    Downloads the JAR (curl with a progress bar — watchdog-friendly), unpacks if
    it's a distribution zip, picks the primary jar (heuristic: name contains
    `tool_name`, shortest matches first), and writes a wrapper script at
    {env}/bin/{wrapper_name or tool_name} that runs `java {java_flags} -jar JAR "$@"`.

    java_flags default: ["-Xmx4g"]. Override for memory-hungry tools (Exomiser
    typically wants -Xmx6g or higher).

    If pipeline_id is supplied, records an install_step (tool=jar, subcommand=install)
    with installed_packages=[{name=tool_name, channel=github (if URL host is github.com),
    else external, source=jar_url}], so the finalize-time package derivation picks it
    up automatically and the spec ends up with install_method.type=jar.
    """
    flags = list(java_flags) if java_flags else ["-Xmx4g"]
    result = _ms._env_mgr.install_jar_tool(
        env_name      = env_name,
        tool_name     = tool_name,
        jar_url       = jar_url,
        java_flags    = flags,
        wrapper_name  = wrapper_name,
    )
    if pipeline_id:
        from urllib.parse import urlparse
        from agent.skills.env_manager import parse_version_from_url
        host = urlparse(jar_url).netloc or ""
        channel = "github" if "github.com" in host else "external"
        version = parse_version_from_url(jar_url)
        ip_record = {
            "name":    tool_name,
            "channel": channel,
            "source":  jar_url,
            "install_method": {"type": "jar", "source": jar_url},
        }
        if version:
            ip_record["version"] = version
        step_data = {
            "tool":        "jar",
            "subcommand":  "install",
            "purpose":     f"Install {tool_name} JAR from {host}",
            "command":     f"install_jar_tool --jar-url {jar_url}",
            "returncode":  0 if result.get("success") else 1,
            "installed_packages": [ip_record],
        }
        if result.get("success"):
            step_data["installed_packages"][0]["install_method"]["jar_path"]        = result.get("jar_path")
            step_data["installed_packages"][0]["install_method"]["wrapper_script"]  = result.get("wrapper_path")
            step_data["installed_packages"][0]["install_method"]["java_flags"]      = flags
        idx = _ms._pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return _ms._shrink_stdio_for_response(result, label=f"jar.{env_name}.{tool_name}")


@mcp.tool()
def install_r_package(
    env_name: str,
    name: str,
    source: str,
    pipeline_id: str = "",
    step: int = 0,
    deps_first: list[str] = [],
    functional_check: str = "",
) -> dict:
    """Install an R package end-to-end with category-correct discovery built in.

    `source` is one of:
      cran          — install.packages("name") from CRAN
      bioconductor  — BiocManager::install("name")
      github:owner/repo  — remotes::install_github("owner/repo", dependencies=FALSE)
                          (use deps_first to pre-install undeclared transitive deps)

    `functional_check` (optional but STRONGLY preferred for tools where import ≠
    works): a SELF-CONTAINED R expression that actually RUNS the package on tiny
    inline-generated data and `stop()`s if it doesn't produce a valid result — no
    external data file (it becomes the freeze-side VALIDATED_IN_IMAGE evidence, so
    it must run inside the ship image with nothing staged). The default validation
    is only a `requireNamespace()` load-or-die = "the namespace loads"; that is NOT
    proof the tool works. The GAPIT probe confirmed the hazard: a package can
    `library()` an UNDECLARED dep inside a rarely-hit code path, install clean,
    import clean, and fail only at runtime. When `functional_check` is given it
    runs after the import check AND is recorded as install_method.functional_evidence
    so `freeze` proves the tool RAN, not merely imported — mirroring the functional
    smoke `install_release_binary`/`install_git_repo` already carry. Example (GAPIT):
    `m<-matrix(sample(0:2,600,TRUE),60); ... myGAPIT<-GAPIT(...); if(is.null(myGAPIT))stop('GAPIT produced no result')`.

    Encapsulates everything the CLAUDE.md "R tools" section used to require
    the agent to remember:
      - Library isolation via $CONDA_PREFIX/lib/R/library (R_LIBS_USER hazard)
      - Auto-installed BiocManager bootstrap if missing
      - Post-install requireNamespace() load-or-die check baked into the
        Rscript itself (catches the "install printed ERROR but rc=0" failure)
      - Auto-records install_method.type='r_install' with the source URL
    `pipeline_id`: lands an install_step automatically. Use `step=N` to replace
    a failed prior attempt (retry semantics) — note that when the previous
    attempt at slot N FAILED (returncode != 0) and this one succeeds, the new
    step is APPENDED at the end of install_steps rather than overwriting slot N
    (see pipeline_state._add_to_step_list). This keeps the replay order = the
    actual successful install order even when a missing dep was discovered
    mid-install.

    Returns the run_install_command shape; failure cases include both R
    install errors and load-failures with a clean signal. On failure, the
    return ALSO includes `missing_packages: [name, ...]` parsed from stderr —
    a structured surface for the autonomy loop. R's package-not-available
    errors are wordy ("Error : Package 'snpStats' not available after install
    attempt"; "there is no package called 'snpStats'") — parsing them here so
    the caller doesn't have to. The caller installs each missing package then
    retries this install, no string-handling round trip.
    """
    # NOTE: the executed Rscript is wrapped in `Rscript -e "..."` (outer double
    # quotes), so every R string literal inside MUST use single quotes — nested
    # double quotes terminate the outer bash string and Rscript receives the
    # package name as an unquoted symbol ("object 'X' not found").
    if source.startswith("github:"):
        owner_repo = source[len("github:"):].strip()
        # Pre-install undeclared transitive deps if requested.
        pre_lines = []
        if deps_first:
            pre = ",".join(f"'{d}'" for d in deps_first)
            pre_lines.append(
                f'BiocManager::install(c({pre}), lib=lib, ask=FALSE, update=FALSE)'
            )
        install_expr = (
            f"remotes::install_github('{owner_repo}', lib=lib, dependencies=FALSE)"
        )
        check_name = name
        install_block = "; ".join(pre_lines + [install_expr])
        channel = "github"
        source_url = f"https://github.com/{owner_repo}"
        im_source = f"remotes::install_github('{owner_repo}')"
    elif source == "cran":
        install_block = (
            f"install.packages('{name}', lib=lib, repos='https://cloud.r-project.org')"
        )
        channel = "cran"
        source_url = f"https://CRAN.R-project.org/package={name}"
        im_source = f"install.packages('{name}')"
        check_name = name
    elif source == "bioconductor":
        install_block = (
            f"BiocManager::install('{name}', lib=lib, ask=FALSE, update=FALSE)"
        )
        channel = "bioconductor"
        source_url = f"https://bioconductor.org/packages/{name}/"
        im_source = f"BiocManager::install('{name}')"
        check_name = name
    else:
        return refused("install.r_unknown_source", success=False,
                       error=f"unknown R source: {source!r} (use cran|bioconductor|github:owner/repo)")

    # Wrap with library isolation + BiocManager bootstrap + load-or-die check.
    # The load-or-die is what makes this honest: rc != 0 if the install
    # silently failed but Rscript would otherwise exit 0.
    rscript = (
        "lib <- file.path(Sys.getenv('CONDA_PREFIX'),'lib','R','library'); "
        # Pin the canonical Bioconductor mirror BEFORE any BiocManager call. A stale
        # BioC_mirror in the ambient R config (observed: a leaked mirrors.ustc.edu.cn
        # in a conda r-base base config) silently breaks BiocManager::install with a
        # 404/unreachable mirror. The freeze-side generator (ic.r_package) already
        # pins this; the host primitive must match so install==ship behaves the same.
        "options(BioC_mirror='https://bioconductor.org'); "
        "if(!requireNamespace('BiocManager',quietly=TRUE)) "
        "install.packages('BiocManager',lib=lib,repos='https://cloud.r-project.org'); "
        f"{install_block}; "
        f"if(!requireNamespace('{check_name}',quietly=TRUE,lib.loc=lib)) "
        f"stop('install reported success but {check_name} is not loadable');"
    )
    command = f"Rscript -e \"{rscript}\""
    verify_command = (
        f"Rscript -e \"if(!requireNamespace('{check_name}',quietly=TRUE)) quit(status=1); "
        f"cat(as.character(packageVersion('{check_name}')))\""
    )
    # FUNCTIONAL evidence (optional): actually RUN the package. Wrapped so the tool
    # is loaded then the agent's self-contained expression exercises it; a stop()
    # inside → non-zero → the install is judged FAILED (imports-but-doesn't-run is a
    # real failure). This command is what freeze re-runs for VALIDATED_IN_IMAGE, so
    # "validated" means "ran". Single-quote R strings only (outer double quotes).
    functional_command = ""
    if functional_check:
        functional_command = (
            f"Rscript -e \"suppressPackageStartupMessages(library('{check_name}')); "
            f"{functional_check}\""
        )

    # Delegate to run_install_command for the actual install_step plumbing.
    result = _ms._env_mgr.run_in_env(env_name, command, timeout=1800)
    # On failure, surface every package R complained was missing as a structured
    # field. R logs these in TWO distinct shapes — both load-bearing in the wild:
    #   • `Error : Package 'X' not available after install attempt` — BiocManager
    #     trying to install a dep that doesn't exist on the resolved mirror
    #     (often: foreign Bioc mirror unreachable; or a CRAN/Bioc-only dep
    #     opportunistically pulled by remotes during install_github)
    #   • `there is no package called 'X'` — load failure of a transitive dep
    #     (e.g. R CMD INSTALL byte-compiling lazy bindings that touch X)
    # Both shapes mean the same thing for the caller: install X then retry me.
    if result.get("returncode") != 0:
        import re as _re
        stderr_blob = (result.get("stderr") or "") + "\n" + (result.get("stdout") or "")
        missing: list[str] = []
        seen: set[str] = set()
        for pat in (r"Package '([A-Za-z][A-Za-z0-9._]*)' not available",
                    r"there is no package called '([A-Za-z][A-Za-z0-9._]*)'"):
            for m in _re.finditer(pat, stderr_blob):
                pkg = m.group(1)
                if pkg != check_name and pkg not in seen:
                    # don't include the package WE were trying to install — its
                    # own load-or-die failure already telegraphs that; missing_packages
                    # is specifically the OTHER packages this install depends on.
                    seen.add(pkg)
                    missing.append(pkg)
        if missing:
            result["missing_packages"] = missing
    if result.get("returncode") == 0:
        vresult = _ms._env_mgr.run_in_env(env_name, verify_command, timeout=60)
        if vresult.get("returncode") != 0:
            result["returncode"] = vresult.get("returncode") or 1
            result["success"]    = False
            result["stderr"]     = (result.get("stderr") or "") + (
                f"\n[verify failed: {vresult.get('stderr','')[-200:]}]"
            )
        else:
            r_version = (vresult.get("stdout") or "").strip()
            result["verify_command"]  = verify_command
            result["verify_output"]   = (vresult.get("stdout") or "")[:500]
            result["resolved_version"] = r_version
            # FUNCTIONAL check: import passed — now prove it RUNS. A failure here is a
            # real install failure (imports but doesn't work), surfaced loudly.
            if functional_command:
                fresult = _ms._env_mgr.run_in_env(env_name, functional_command, timeout=600)
                if fresult.get("returncode") != 0:
                    result["returncode"] = fresult.get("returncode") or 1
                    result["success"]    = False
                    result["functional_check_failed"] = True
                    result["stderr"] = (result.get("stderr") or "") + (
                        f"\n[functional check failed — {check_name} imports but did not run: "
                        f"{(fresult.get('stderr') or '')[-300:]}]")
                else:
                    result["functional_command"] = functional_command
                    result["functional_output"]  = (fresult.get("stdout") or "")[:500]

    if pipeline_id:
        im_record = {"type": "r_install", "source": im_source}
        # Carry the FUNCTIONAL evidence into install_method so freeze re-runs it for
        # VALIDATED_IN_IMAGE (validated == RAN, not just imported). Absent → freeze
        # falls back to the import-only evidence (back-compat).
        if result.get("functional_command"):
            im_record["functional_evidence"] = result["functional_command"]
        ip_record = {
            "name":    check_name,
            "channel": channel,
            "source":  im_source,
            "install_method": im_record,
        }
        if result.get("resolved_version"):
            ip_record["version"] = result["resolved_version"]
        step_data = {
            "tool":        "Rscript",
            "subcommand":  source,
            "purpose":     f"Install R package {check_name} from {source}",
            "command":     command,
            "returncode":  result.get("returncode"),
            "runtime_seconds": result.get("runtime_seconds"),
            "installed_packages": [ip_record],
        }
        if result.get("verify_command"):
            step_data["verify_command"] = result["verify_command"]
        idx = _ms._pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        # Cache the load-or-die verify so the finalize package derivation
        # attaches it (mirrors verify_installation / install_git_repo). Without
        # this the derived PackageRecord has no verify_output and fails I2,
        # forcing a redundant verify_installation call for every R package.
        if result.get("success") and result.get("verify_output"):
            _ms._pipeline_state.cache_verification(pipeline_id, check_name, {
                "verify_command": result.get("verify_command"),
                "verify_output":  result.get("verify_output"),
            })
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return _ms._shrink_stdio_for_response(result, label=f"r.{source}.{env_name}.{name}")


@mcp.tool()
def install_pip_package(
    env_name: str,
    name: str,
    version: str = "",
    pip_flags: OptStrList = None,
    pipeline_id: str = "",
    step: int = 0,
    functional_check: str = "",
) -> dict:
    """Install a pip package end-to-end with an auto-verify_command.

    Equivalent to running pip install + python -c "import name" inside the env.
    The import-check is the load-or-die: if pip says it installed but the
    package isn't importable, the step is recorded as failed.

    `functional_check` (optional but preferred where import ≠ works): a Python
    expression that actually RUNS the package (it runs as `python -c "import
    <name>; <functional_check>"`, so it can assume the module is imported) and
    raises if the result is wrong. It executes after the import check (imports-but-
    doesn't-run is a FAILED install) and is recorded as install_method.
    functional_evidence, so freeze's VALIDATED_IN_IMAGE proves the package RAN,
    not merely imported — the pip analog of install_r_package's functional_check
    (the GAPIT/snpStats lesson: import success is not run success).

    `pip_flags` (NEW): extra flags passed verbatim to pip install. Use for
    `--no-binary :all:` (force source compile), `--no-build-isolation`,
    `--index-url`, etc. PERSISTED on `install_method.pip_flags` so freeze's
    replay path emits the SAME flags inside the shipped image — otherwise
    pip's default wheel substitution would silently downgrade the validated
    source compile (the pysam-stress P2 trust violation). Pass as a list of
    tokens (`['--no-binary', ':all:']`); we shlex-quote each. Flag-bearing
    pip installs land in the freeze build as engine-coupled long-tail steps,
    not via `pixi add --pypi` (uv doesn't honor pip flags).

    Notes:
      - pip's canonical name is preserved (e.g. open-cravat stays hyphenated)
      - resolved_version is filled from `pip list --format=json` at finalize
        when no explicit version is passed
    """
    pip_flags = list(pip_flags or [])
    spec = f"{name}=={version}" if version else name
    # Module name for the import check: pip's canonical hyphenated names use
    # underscores at import time (open-cravat → import cravat or open_cravat).
    # We default to the lowercased name; agents can override with verify_command
    # post-hoc if the import path differs.
    import_check_name = name.replace("-", "_").lower()
    _flag_str = " ".join(shlex.quote(f) for f in pip_flags)
    command = " ".join(part for part in (f"pip install", _flag_str, spec) if part)
    verify_command = f"python -c 'import {import_check_name}' || pip show {name} > /dev/null"
    functional_command = ""
    if functional_check:
        functional_command = f"python -c 'import {import_check_name}; {functional_check}'"

    result = _ms._env_mgr.run_in_env(env_name, command, timeout=600)
    if result.get("returncode") == 0:
        vresult = _ms._env_mgr.run_in_env(env_name, verify_command, timeout=30)
        if vresult.get("returncode") != 0:
            result["returncode"] = vresult.get("returncode") or 1
            result["success"]    = False
            result["stderr"]     = (result.get("stderr") or "") + (
                f"\n[verify failed: cannot import {import_check_name} and pip show {name} did not find it]"
            )
        else:
            result["verify_command"] = verify_command
            result["verify_output"]  = (vresult.get("stdout") or "")[:200] or "(import succeeded)"
            # FUNCTIONAL check: import passed — now prove it RUNS (validated == ran).
            if functional_command:
                fresult = _ms._env_mgr.run_in_env(env_name, functional_command, timeout=600)
                if fresult.get("returncode") != 0:
                    result["returncode"] = fresult.get("returncode") or 1
                    result["success"]    = False
                    result["functional_check_failed"] = True
                    result["stderr"] = (result.get("stderr") or "") + (
                        f"\n[functional check failed — {name} imports but did not run: "
                        f"{(fresult.get('stderr') or '')[-300:]}]")
                else:
                    result["functional_command"] = functional_command
                    result["functional_output"]  = (fresult.get("stdout") or "")[:500]

    if pipeline_id:
        # `pip_flags` persists on install_method so freeze's replay re-emits the
        # SAME flags inside the shipped image. Without this, env_freeze would
        # reconstruct the pip spec from {name, version} alone and silently drop
        # the flags — the P2 trust violation (validated source compile →
        # shipped manylinux wheel).
        im: dict = {"type": "pip", "source": command}
        if pip_flags:
            im["pip_flags"] = list(pip_flags)
        # functional_evidence → freeze uses it as the in-image verify (validated == ran).
        if result.get("functional_command"):
            im["functional_evidence"] = result["functional_command"]
        ip_record = {
            "name":    name,
            "channel": "pip",
            "source":  command,
            "install_method": im,
        }
        if version:
            ip_record["version"] = version
        step_data = {
            "tool":               "pip",
            "subcommand":         "install",
            "purpose":            f"Install pip package {name}",
            "command":            command,
            "returncode":         result.get("returncode"),
            "runtime_seconds":    result.get("runtime_seconds"),
            "installed_packages": [ip_record],
        }
        if result.get("verify_command"):
            step_data["verify_command"] = result["verify_command"]
        idx = _ms._pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        # Cache the import-check verify so the finalize package derivation
        # attaches it — without this the derived PackageRecord has no
        # verify_output and fails I2 (same gap fixed for R packages).
        if result.get("success") and result.get("verify_output"):
            _ms._pipeline_state.cache_verification(pipeline_id, name, {
                "verify_command": result.get("verify_command"),
                "verify_output":  result.get("verify_output"),
            })
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return _ms._shrink_stdio_for_response(result, label=f"pip.{env_name}.{name}")


# download_reference_database moved to agent.mcp_tools.data_tools


# run_pipeline_step / run_step_in_container / verify_installation / run_in_env
# / validate_output (plus helpers _infer_validator_type / _stamp_i7_authority)
# moved to agent.mcp_tools.run_tools


# ---------------------------------------------------------------------------
# Docker


@mcp.tool()
def run_install_command(
    env_name: str,
    command: str,
    installed_packages: list[dict] = [],
    working_dir: str = "",
    timeout_seconds: int = 1800,
    pipeline_id: str = "",
    step: int = 0,
    tool: str = "",
    subcommand: str = "",
    purpose: str = "",
    verify_command: str = "",
) -> dict:
    """Run an install command inside a conda environment (BiocManager::install,
    remotes::install_github, pip install, downloading reference DBs, etc.).

    This is the install-side mirror of run_in_env: same shape, same semantics,
    but the resulting step lands in draft.install_steps (not pipeline_steps)
    so the environment-build journey is recorded separately from the actual
    algorithm/analysis runs.

    installed_packages should list what this command installed:
        [{name: 'GAPIT', version: '4.1.0', channel: 'github', source: '...'}, ...]
    These power the side-by-side "command → packages" rendering in the report,
    AND each entry is appended to draft.packages as a PackageRecord (with an
    install_method derived from `channel`) — closing the loop with verify_installation,
    which can then patch verify_command / verify_output onto that record.

    Pass `step=N` to replace install_step N (for retries). Default is append.

    verify_command: an optional shell command run AFTER the install command in the
    same env. If supplied AND the install command returns 0, but the verify command
    returns non-zero, the step is recorded as failed (returncode=verify exit code,
    stderr includes the verify output). This catches silent-success cases like
    `R install.packages` printing 'ERROR: lazy loading failed' while the Rscript
    process still exits 0. Recommended for R installs:
        verify_command="Rscript -e 'if(!requireNamespace(\"GAPIT\")) quit(status=1)'"
    and for pip-source installs:
        verify_command="python -c 'import mypkg'"

    Return keys: returncode, stdout, stderr, success, command, runtime_seconds,
                 inputs, detected_outputs, [verify_returncode, verify_output],
                 [pipeline_merge]."""
    result = _ms._env_mgr.run_in_env(
        env_name, command,
        working_dir=working_dir or None,
        timeout=timeout_seconds,
        inputs=[],
        watch_dir=None,
    )
    if verify_command and result.get("returncode") == 0:
        vresult = _ms._env_mgr.run_in_env(
            env_name, verify_command,
            working_dir=working_dir or None,
            timeout=120,
        )
        verify_rc  = vresult.get("returncode", 1)
        verify_out = ((vresult.get("stdout") or "") + (vresult.get("stderr") or ""))[:500]
        result["verify_command"]   = verify_command
        result["verify_returncode"] = verify_rc
        result["verify_output"]    = verify_out
        if verify_rc != 0:
            result["returncode"] = verify_rc
            result["success"]    = False
            result["stderr"]     = (result.get("stderr") or "") + (
                f"\n\n[verify_command failed: {verify_command}]\n{verify_out}"
            )
    if pipeline_id:
        step_data = {
            "tool":               tool or (command.split() or [""])[0],
            "subcommand":         subcommand or None,
            "purpose":            purpose or None,
            "command":            command,
            "returncode":         result.get("returncode"),
            "runtime_seconds":    result.get("runtime_seconds"),
            "installed_packages": installed_packages or [],
        }
        if verify_command:
            step_data["verify_command"]   = verify_command
            step_data["verify_returncode"] = result.get("verify_returncode")
        step_data = {k: v for k, v in step_data.items() if v is not None}
        idx = _ms._pipeline_state.add_install_step(pipeline_id, step_data, replace_step=step)
        # No more dual-write to draft.packages. The finalize-time package
        # builder picks up every `installed_packages` entry from successful
        # install_steps automatically — single source of truth.
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "install_step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    _label_suffix = tool or subcommand or "cmd"
    return _ms._shrink_stdio_for_response(result, label=f"install.{env_name}.{_label_suffix}")


