"""
env_recipe_render — the HUMAN-READABLE form of an env build recipe.

`env_recipe.extract_recipe` produces the MACHINE recipe (the dict a rebuild / CI /
`verify_env_recipe` consumes). This module renders that same dict into Markdown: a
runnable command sequence for the person who has to rebuild the environment by hand —
"the one dealing with a potential landmine".

Same honesty posture as every other artifact here: this is rendered PURELY from the
verified freeze record (the recipe dict + the optional EnvCache record). Nothing is
authored from an agent's memory — every command below is a deterministic function of
what freeze actually recorded. It covers every build_method a freeze can produce:

    container-native-build   conda/pip + the non-conda tiers (jar/source/binary/
                             cargo/go/perl/r/synthesized), baked into an image
    adopt                    a published BioContainer pulled BY DIGEST (pure-conda)
    authors-dockerfile       the tool's OWN Dockerfile at a pinned source commit

Pure assembly — no docker, no network, no clock. Deterministic given its inputs.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from agent.skills import freeze as _freeze
from agent.models.core_data import shipped_binaries as _shipped_binaries
from agent.models.core_data import tool_identities as _tool_identities


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def _md_inline(s: str) -> str:
    """Neutralize an UNTRUSTED registry blurb for safe INLINE Markdown. This module writes
    raw Markdown (no HTML-escaping layer, unlike the ENV report), so a registry-controlled
    package summary must not break out of plain prose. Collapse whitespace, defuse backticks
    (a code-span toggle), and backslash-escape the link/image/inline-HTML metacharacters
    `[ ] ( ) ! < >` — otherwise a Summary like `![x](http://attacker/t.png)` renders as an
    auto-loading image (viewer-IP leak) and `[click](http://attacker/x.sh)` as a live link
    in a doc we ship as reproducible-by-anyone. Backslash-escaping is valid CommonMark and
    renders the literal characters."""
    t = re.sub(r"\s+", " ", (s or "")).strip().replace("`", "'")
    return re.sub(r"([\[\]()!<>])", r"\\\1", t)


def _fence(lines: list[str], lang: str = "bash") -> list[str]:
    return [f"```{lang}", *lines, "```", ""]


def _short(digest: str, n: int = 19) -> str:
    """`sha256:7f5a3ed6…` — the readable head of a content digest."""
    if not digest:
        return "(none recorded)"
    body = digest.split(":", 1)[-1]
    return f"{digest.split(':',1)[0]}:{body[:12]}…" if ":" in digest else f"{body[:n]}…"


def _local_sif_block(image_tag: str) -> list[str]:
    """The shared 'convert the image to a cluster .sif LOCALLY' step. Never build a
    .sif on a shared head node — build it on your own machine and ship the artifact
    (see the no-head-node-image-builds rail)."""
    return _fence([
        f"# convert the docker image to an Apptainer .sif LOCALLY (never on a head node):",
        f"docker save {image_tag} -o env.tar",
        f"docker run --rm --privileged --platform linux/amd64 -v \"$PWD:/work\" -w /work \\",
        f"    kaczmarj/apptainer:1.4.4 build --force env.sif docker-archive:/work/env.tar",
        f"# ship env.sif to the cluster (scp / globus) and run:",
        f"#   apptainer exec --cleanenv env.sif <command>",
    ])


# ---------------------------------------------------------------------------
# per-install-step command rendering (mirrors env_freeze._map_install field reads)
# ---------------------------------------------------------------------------
def render_step_commands(step: dict) -> list[str]:
    """Turn ONE non-conda install_step into the human command line(s) that install it.
    Reads the SAME install_method fields env_freeze._map_install replays, so the human
    recipe matches what the build actually does. Returns fenced-block-ready lines."""
    im = step.get("install_method") if isinstance(step.get("install_method"), dict) else {}
    name = step.get("tool") or step.get("name") or im.get("name") or "tool"
    t = (im.get("type") or step.get("type") or "").strip()

    if t == "jar":
        url = im.get("source") or im.get("jar_url") or "<jar_url>"
        # WHICH JRE is not a detail a human can guess, and the two routes are not
        # interchangeable: apt's default is Java 17 on the shipped Debian base, and
        # a tool that asked for 21 will not run on it. Name the one this env used —
        # a rebuild recipe silent about the runtime is not a rebuild recipe.
        jv = str(im.get("java_version") or "").strip()
        jre = (f"conda install -c conda-forge openjdk={jv}   # the JRE this env was built with"
               if jv else
               "apt-get install -y --no-install-recommends default-jre-headless   # Java 17 on bookworm")
        return [f"# {name}: Java jar tool",
                jre,
                f"wget -O {name}.jar '{url}'",
                f"# wrapper: java {im.get('java_flags') or ''} -jar {name}.jar \"$@\""]
    if t == "source":
        repo = im.get("source") or "<repo_url>"
        ref = im.get("commit_sha") or im.get("ref") or ""
        out = [f"# {name}: source install",
               f"git clone {repo} {name}",
               (f"git -C {name} checkout {ref}" if ref else f"# (no ref pinned — pin a commit/tag!)")]
        if im.get("build_command"):
            out.append(f"cd {name} && {im.get('build_command')}")
        if im.get("entrypoint"):
            interp = (im.get("interpreter") or "").strip()
            out.append(f"# run via: {interp + ' ' if interp else ''}{name}/{im.get('entrypoint')}")
        elif im.get("bin_path"):
            out.append(f"# built binary: {name}/{im.get('bin_path')}")
        return out
    if t == "synthesized":
        cmds = im.get("commands") or []
        head = [f"# {name}: agent-synthesized from the tool's own build files"]
        if im.get("source"):
            head.append(f"#   repo {im.get('source')}"
                        + (f" @ {im.get('commit_sha')}" if im.get("commit_sha") else ""))
        return head + list(cmds)
    if t == "binary":
        sha = (im.get("asset_sha256") or "").strip()
        # AN ARTIFACT ONLY THE OPERATOR CAN OBTAIN GETS AN INSTRUCTION, NOT A COMMAND.
        # This branch used to render `curl -fL -o x.tar '<binary_url>'` unconditionally,
        # and for an operator-supplied install that URL was the path on the agent's own
        # laptop (`file:///private/tmp/.../scratchpad/...`) — a copy-pasteable line that
        # cannot work anywhere else, in a scratch dir that no longer exists. The same
        # doctrine already stated forty lines down for an unpinned source checkout
        # applies verbatim here: an instruction you can paste is worse than none,
        # because it looks like one. (The lesson existed in this file and had been
        # applied at exactly one site — the propagation defect, again.)
        if im.get("artifact_source") == "operator_supplied":
            art = im.get("artifact_name") or f"{name}-artifact"
            out = [f"# {name}: installed from an artifact THE OPERATOR SUPPLIED.",
                   f"#",
                   f"# There is no download command for this step, and that is not an",
                   f"# omission — these bytes are not ours to distribute or re-fetch.",
                   f"# Obtain `{art}` from the tool's vendor yourself (this normally means",
                   f"# accepting a licence), then:"]
            if sha:
                out.append(f"#   1. verify it:  echo '{sha}  {art}' | sha256sum -c")
                out.append(f"#   2. re-run:     install_release_binary(env, '{name}',")
                out.append(f"#                      local_path='/path/to/{art}',")
                out.append(f"#                      sha256='{sha}')")
            else:
                out.append(f"#   re-run: install_release_binary(env, '{name}', "
                           f"local_path='/path/to/{art}')")
                out.append(f"# NOTE: no asset sha256 was recorded, so a rebuild cannot prove it")
                out.append(f"# obtained the SAME artifact this image was built from.")
            return out
        url = im.get("binary_url") or "<binary_url>"
        out = [f"# {name}: precompiled release binary",
               f"curl -fL -o {name}.tar '{url}'"]
        if sha:
            out.append(f"echo '{sha}  {name}.tar' | sha256sum -c   # anchor the bytes")
        out += [f"tar xf {name}.tar && chmod +x {name}",
                f"# put {name} on PATH"]
        return out
    if t == "cargo":
        crate = im.get("crate") or name
        ver = im.get("version") or ""
        git = im.get("git_url") or ""
        if git:
            return [f"# {name}: Rust crate (git)", f"cargo install --git {git} --root $PREFIX"]
        return [f"# {name}: Rust crate",
                f"cargo install {crate}{('@' + ver) if ver else ''} --root $PREFIX"]
    if t == "go":
        pkg = im.get("package") or name
        ver = im.get("version") or "latest"
        return [f"# {name}: Go tool", f"GOBIN=$PREFIX/bin go install {pkg}@{ver}"]
    if t == "perl":
        module = im.get("module") or name
        be = (im.get("build_env") or "").strip()
        flags = im.get("cpanm_flags") or "--notest"
        line = f"cpanm {flags} {module}"
        return [f"# {name}: Perl/CPAN module",
                (f"{be} {line}" if be else line)]
    if t == "r_install":
        src = im.get("source") or ""
        return [f"# {name}: R package",
                f"Rscript -e 'install.packages(\"{name}\")'   # source: {src or 'cran'}"]
    # conda umbrella step, or an install_method we can't detail — show the purpose.
    if (t in ("", "conda")) and (step.get("purpose") or "").lower().startswith("install"):
        return []   # conda handled by the conda-create block; no per-step line
    return [f"# {name}: {step.get('purpose') or t or 'install step'} "
            f"(see machine recipe / install_method for exact commands)"]


# ---------------------------------------------------------------------------
# the three build-method rebuild sections
# ---------------------------------------------------------------------------
def _channels(recipe: dict) -> str:
    return "-c conda-forge -c bioconda"


def _conda_block(recipe: dict) -> list[str]:
    deps = [d for d in (recipe.get("conda_deps") or []) if d]
    if not deps:
        return []
    locked = bool(recipe.get("conda_lock"))
    out = [f"# 1. create the conda/pip env "
           + ("(pinned by the captured lockfile — see machine recipe)" if locked
              else "(re-solved from specs)") + ":"]
    out += _fence([f"conda create -y -n {recipe.get('name','env')} {_channels(recipe)} \\",
                   "    " + " ".join(deps)])
    if locked:
        out += ["> The machine recipe (`*.recipe.yaml`) carries the exact `conda_lock` "
                "(URL+sha256 per package). Materialising from that lock — rather than "
                "re-solving — reproduces the identical package set across machines/time.", ""]
    return out


def _apt_block(record: Optional[dict]) -> list[str]:
    if not record:
        return []
    sysp = record.get("system_packages") or []
    if not sysp:
        return []
    names = []
    for p in sysp:
        if isinstance(p, dict):
            names.append(p.get("name") or p.get("package") or "")
        elif isinstance(p, str):
            names.append(p)
    names = sorted({n for n in names if n})
    if not names:
        return []
    shown = names[:24]
    more = f"  # +{len(names)-len(shown)} more — full list in the SBOM" if len(names) > len(shown) else ""
    return [f"# system (apt) packages baked into the image ({len(names)} total):",
            *_fence([f"apt-get update && apt-get install -y \\",
                     "    " + " ".join(shown) + (" \\" + more if more else "")])]


def _section_build(recipe: dict, record: Optional[dict]) -> list[str]:
    name = recipe.get("name", "env")
    ver = recipe.get("version", "")
    tag = f"{name}:{ver}" if ver else f"{name}:latest"
    out = ["## Option B — rebuild from scratch (container-native build)", "",
           "The env was built by installing everything INTO an image and validating it "
           "there (validated == shipped). To reproduce by hand, in a Linux build context "
           f"(base image `{__import__('agent.skills.container_build', fromlist=['BASE_IMAGE']).BASE_IMAGE.split('@')[0]}` "
           "pinned by digest in the machine recipe):", ""]
    out += _apt_block(record)
    out += _conda_block(recipe)
    # RENDER THE VIEW THE BUILDER REPLAYS, not a second view of the same steps.
    # `render_step_commands` reads `install_method` off the record handed to it, and
    # this loop used to hand it the RAW install_step — where no producer writes that
    # key. Every release-binary/jar/source install lands under
    # `install_steps[].installed_packages[].install_method`, so the reader saw `{}`,
    # returned [], and the step was dropped from its own rebuild instructions with no
    # sign anything was missing. Found 2026-08-04: the operator-supplied-artifact step
    # — the ONE step a human MUST be told about, because only they can obtain it —
    # rendered as nothing at all, under a heading that says "install the non-conda
    # tools". `non_conda_installs` is the accessor freeze itself uses to decide what to
    # replay, so rendering from it makes this function's docstring claim ("the SAME
    # install_method fields env_freeze._map_install replays") true instead of aspirational.
    longtail = [p for p in _freeze.non_conda_installs(recipe) if render_step_commands(p)]
    if longtail:
        out += ["# 2. install the non-conda tools (each baked + validated in the image):"]
        for p in longtail:
            cmds = render_step_commands(p)
            if cmds:
                out += _fence(cmds)
    out += ["# 3. bake into an image, then convert to a cluster .sif:"]
    out += _fence([f"# (assemble the above into a Dockerfile and: docker build -t {tag} .)"])
    out += _local_sif_block(tag)
    out += ["> The exact, byte-for-byte build instructions are re-derivable with "
            "`verify_env_recipe` against the machine `*.recipe.yaml`, which rebuilds the "
            "image and checks it converges to the recorded content digest.", ""]
    return out


def _section_adopt(recipe: dict, record: Optional[dict]) -> list[str]:
    img = recipe.get("adopt_image") or ""
    name = recipe.get("name", "env")
    tag = f"{name}:local"
    out = ["## Option B — rebuild from scratch (adopt a published image)", ""]
    # THE HEADING PROMISES A DIGEST — SO ONLY PRINT IT WHEN WE HAVE ONE. This fell back to
    # `record["image"]`, a local tag (e.g. `talos-authors:11.0.0`) that exists on exactly
    # one machine, and printed it under "the digest guarantees identical bytes". A pull
    # command that cannot work, sold as content-addressing.
    if "@sha256:" not in img:
        pin_err = (record or {}).get("adopt_pin_error") or ""
        # Do NOT name the unpullable reference here. Printing it — even inside the warning
        # against it — hands the reader the exact copy-pasteable line the warning exists to
        # withhold, and a reader who skims to the monospace text is the reader this is for.
        out += ["> **NOT PINNABLE — this image has no registry manifest digest"
                + (f": {pin_err}" if pin_err else "") + ".** It was not pulled from a "
                "registry, so no reference exists that anyone else could pull: the local "
                "tag resolves only on the machine that built it. The pull command is "
                "omitted rather than guessed. Deliver this env via the `.sif`/tarball "
                "route instead, or re-freeze against a pushed image to get a pinnable "
                "recipe.", ""]
        return out
    out += ["This env maps to a published image, so the recipe is simply pulling it BY "
            "DIGEST (content-addressed — the digest guarantees identical bytes) and "
            "converting it to a .sif:", ""]
    out += _fence([f"docker pull {img}",
                   f"docker tag {img} {tag}"])
    out += _local_sif_block(tag)
    deps = [d for d in (recipe.get("conda_deps") or []) if d]
    if deps:
        out += ["### Alternative — build the conda env directly (no container)", "",
                "If you just want the tools in a local conda env rather than the image:", ""]
        out += _fence([f"conda create -y -n {name} {_channels(recipe)} \\",
                       "    " + " ".join(deps)])
    return out


def _section_authors(recipe: dict, record: Optional[dict]) -> list[str]:
    ds = recipe.get("dockerfile_source") or (record or {}).get("dockerfile_source") or {}
    repo = ds.get("repo") or ""
    commit = ds.get("commit") or ds.get("commit_sha") or ""
    tag_ref = ds.get("tag") or ""
    recipe_path = ds.get("recipe_path") or ""
    build_args = ds.get("build_args") or {}
    platform = ds.get("platform") or "linux/amd64"
    name = recipe.get("name", "env")
    ver = recipe.get("version", "")
    img = f"{name}:{ver}" if ver else f"{name}:latest"
    out = ["## Option B — rebuild from scratch (the tool's OWN Dockerfile)", "",
           "This env was built the authors-recipe-first way: from the tool's own Dockerfile "
           "at a pinned source commit. That is what captures the non-python / compiled "
           "pieces a conda/pip reconstruction silently drops.", ""]

    # A RECIPE THAT CANNOT BE FOLLOWED MUST SAY SO, NOT RENDER A PLACEHOLDER AS A COMMAND.
    # This used to emit `git clone <repo_url> src` / `git checkout <ref>` — copy-pasteable,
    # confidently wrong — and gate its "Source pin" note on `if commit and tag_ref`, so the
    # warning appeared only when the data was already there and vanished exactly when the
    # reader needed it. An unpinned source is the ONE fact that voids the whole document.
    missing = [n for n, v in (("repo", repo), ("commit", commit)) if not v]
    if missing:
        # As in _section_adopt: state the gap, never demonstrate it. A placeholder shown as
        # an example is still a placeholder the reader can paste.
        out += [f"> **THIS RECIPE IS NOT REPRODUCIBLE — the record is missing "
                f"`{'`, `'.join(missing)}`.** A rebuild cannot be pinned to the source this "
                f"image was actually built from, so the build commands are omitted rather "
                f"than guessed: an unpinned checkout you can paste is worse than no "
                f"instruction at all, because it looks like one. Re-freeze via "
                f"`build_env_from_authors_recipe` with an explicit tag or commit to produce "
                f"a followable recipe.", ""]
        return out

    build = [f"docker build --platform {platform}"]
    if recipe_path and recipe_path != "Dockerfile":
        build.append(f"  -f {recipe_path}")          # NOT the root Dockerfile
    for k, v in build_args.items():
        build.append(f"  --build-arg {k}={v}")       # else the ARG defaults silently win
    build.append(f"  -t {img} .")
    out += ["To reproduce:", ""]
    out += _fence([f"git clone {repo} src",
                   f"cd src && git checkout {commit}"
                   + (f"   # tag {tag_ref}" if tag_ref else ""),
                   " \\\n".join(build)])
    out += _local_sif_block(img)
    pin = f"> Source pin: `{repo}` commit `{commit}`"
    pin += f" (tag `{tag_ref}`)." if tag_ref else "."
    if recipe_path:
        pin += f" Built from `{recipe_path}`."
    if build_args:
        pin += (" Build args are pinned above — without them the Dockerfile's own `ARG`"
                " defaults apply, which is a DIFFERENT image.")
    out += [pin + " The Dockerfile is included alongside this recipe for inspection.", ""]
    return out


# ---------------------------------------------------------------------------
# top-level render
# ---------------------------------------------------------------------------
def _installed_versions_table(recipe: dict, record: dict) -> list[str]:
    """A requested-vs-OBSERVED-installed row per primary tool, ⚠ on divergence — the
    same at-a-glance honesty the ENV report carries, so the recipe never implies the
    REQUESTED version is what shipped (audit 2026-07-19, W4).

    Reads through the shared `_resolved_version` / `version_divergences` (Rule 4), so
    this table and the ENV report cite the same numbers. The observed SBOM comes from
    the record, falling back to the recipe's own `resolved_packages` so a standalone
    recipe still shows it. Installed is OBSERVED or 'not recorded' — never the request."""
    from agent.skills.env_report_helpers import (
        _pkg_index, _resolved_version, _verif_index, requested_versions,
        version_divergences)
    src = {
        "requested_tools": record.get("requested_tools") or recipe.get("primary_tools") or [],
        "request_key": record.get("request_key", ""),
        "conda_specs": record.get("conda_specs") or recipe.get("conda_deps") or [],
        "resolved_packages": (record.get("resolved_packages")
                              or recipe.get("resolved_packages") or []),
        "verifications": record.get("verifications") or [],
        "shipped_binaries": (record.get("shipped_binaries")
                             or recipe.get("shipped_binaries") or []),
    }
    tools = src["requested_tools"]
    if not tools:
        return []
    req = requested_versions(src)
    pidx = _pkg_index(src["resolved_packages"])
    vidx = _verif_index(src["verifications"])
    shipped = src["shipped_binaries"]
    diverging = {d["tool"].lower() for d in version_divergences(src)}
    rows: list[str] = []
    for t in tools:
        name = str(t).split("=")[0].strip()
        if not name:
            continue
        rq = req.get(name) or req.get(str(t).strip()) or "(any)"
        inst = _resolved_version(name, pidx.get(name.lower()), vidx.get(name.lower()), shipped)
        cell = (f"⚠ {inst} — differs from requested" if name.lower() in diverging
                else (inst or "not recorded"))
        rows.append(f"| `{name}` | {_md_inline(rq)} | {_md_inline(cell)} |")
    if not rows:
        return []
    return (["**Requested vs installed** — the requested version is what you asked for; "
             "the installed version is what the shipped image actually holds. A ⚠ marks a "
             "divergence; *not recorded* means no version was observed (e.g. a tool built "
             "from source), never a silent substitution of the request.", "",
             "| Tool | Requested | Installed (observed) |", "|---|---|---|"]
            + rows + [""])


def render_recipe_markdown(recipe: dict, record: Optional[dict] = None) -> str:
    """Render a build recipe dict into a runnable, human-readable Markdown guide.

    `recipe` is the machine recipe (env_recipe.extract_recipe output). `record` is the
    optional EnvCache entry — when present it enriches the provenance section (SBOM
    counts, shipped binaries, system packages) without which the rebuild commands still
    render fully. Deterministic and side-effect-free."""
    recipe = recipe or {}
    record = record or {}
    name = recipe.get("name") or record.get("name") or "env"
    # `ver` is the REQUESTED version arg (extract_recipe/freeze pass it straight through;
    # no path populates it from the SBOM). It must NOT lead the H1 as if it were the env's
    # version — the sibling ENV report deliberately titles with no version for exactly this
    # reason, and the observed-installed table below carries the real number (audit
    # 2026-07-20 hunt). Kept only as an explicitly-labelled "Requested version" metadata row.
    ver = recipe.get("version") or record.get("version") or ""
    platform = recipe.get("platform") or record.get("platform") or "linux/amd64"
    method = (recipe.get("build_method") or record.get("build_method")
              or ("adopt" if record.get("mode") == "adopt" else "container-native-build"))
    content_digest = recipe.get("content_digest") or record.get("content_digest") or ""
    tools = recipe.get("primary_tools") or record.get("requested_tools") or []

    L: list[str] = []
    L += [f"# Environment build recipe — {name}", ""]
    L += ["| | |", "|---|---|",
          f"| **Primary tools** | {', '.join(tools) if tools else '(none recorded)'} |"]
    if ver:
        L += [f"| **Requested version** | {_md_inline(ver)} (as requested — see the "
              f"observed-installed table below for what shipped) |"]
    L += [
          f"| **Build method** | `{method}` |",
          f"| **Platform** | `{platform}` |",
          f"| **Content digest** | `{_short(content_digest)}` |", ""]
    L += ["> This recipe is rendered **purely from the verified freeze record** — every "
          "command reflects how the environment was actually built, not a description "
          "written after the fact. There are two ways to obtain the env; prefer Option A.", ""]

    # Option A — reuse the exact bytes (always available; strongest guarantee)
    L += ["## Option A — reuse the exact image (no rebuild) — preferred", "",
          "The frozen image *is* the environment. Ship the `.sif` (or pull the image by "
          "digest) and run it — byte-identical, nothing to install:", ""]
    img = record.get("image") or (recipe.get("adopt_image") or f"{name}:{ver or 'latest'}")
    L += _fence([f"apptainer exec --cleanenv {name}.sif <command>",
                 f"# image: {img}",
                 f"# content digest: {content_digest or '(none)'}"])

    # Option B — rebuild from scratch (method-specific)
    if method == "adopt":
        L += _section_adopt(recipe, record)
    elif method in ("authors-dockerfile", "freeze-from-image"):
        L += _section_authors(recipe, record)
    else:
        L += _section_build(recipe, record)

    # Provenance
    L += ["## What's inside (provenance)", ""]
    # Requested vs OBSERVED-installed, per primary tool (W4) — leads the section so the
    # version honesty is the first thing read, matching the ENV report.
    L += _installed_versions_table(recipe, record)
    sb = _shipped_binaries(record)
    if sb:
        L += ["**Long-tail binaries baked in** (the pieces a package manager wouldn't give you):", ""]
        for b in sb:
            # `command` used to be read here as if it were the tool name, but the
            # freeze_tools producer wrote the literal shell line into that key — so
            # this rendered a whole `git clone …` command where a tool name belongs.
            # `tool` is now the tool; `install_command` is the command.
            L += [f"- `{b.tool}` "
                  + (f"({b.version})" if b.version else "(version unrecorded)")
                  + (f" — {b.provenance}" if b.provenance else "")]
        L += [""]
    rp = record.get("resolved_packages") or recipe.get("resolved_packages") or []
    syp = record.get("system_packages") or recipe.get("system_packages") or []
    if rp or syp:
        L += [f"**SBOM:** {len(rp)} resolved packages (conda/pip closure) · "
              f"{len(syp)} system (apt) packages — full versioned list in the "
              "`*.attestation.json` / `_env_cache_entry.json` (also carried in this "
              "recipe's `resolved_packages`).", ""]

    # IDENTITY DISCLOSURE (audit #8) — the tool's OWN words, self-described + UNVERIFIED.
    # Prefer the recipe's copy (travels with the reproduction bytes); fall back to the record.
    idents = _tool_identities(recipe) or _tool_identities(record)
    described = [i for i in idents if i.self_description]
    if described:
        L += ["**What each tool says it is** — *self-described, unverified.* Each line is the "
              "package's OWN one-line description from the registry it shipped from: a claim to "
              "READ, not a validated capability. It is here so a wrong-domain match is catchable "
              "(a `cellranger` that turns out to be a spreadsheet-range parser).", ""]
        for i in described:
            src = f" _({i.source})_" if i.source else ""
            L += [f"- `{i.tool}`{src}: {_md_inline(i.self_description)}"]
        L += [""]

    # Verify
    L += ["## Verify what you rebuilt", "",
          "- **Exact bytes:** the shipped `.sif`'s sha256 is recorded in the run report / "
          "delivery manifest — compare it after transfer.",
          "- **From-source convergence:** run `verify_env_recipe` against the machine "
          "`*.recipe.yaml` — it rebuilds the image and checks it converges to the recorded "
          f"content digest (`{_short(content_digest)}`).", ""]
    return "\n".join(L)
