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
from agent.skills.env_recipe import AUTHORS_METHODS as _AUTHORS_METHODS
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
def render_built_commands(recipe: dict) -> list[dict]:
    """THE TRANSCRIPT: the literal commands that built the shipped image, as recorded
    by `ContainerBuild.run_install` and baked by `emit_dockerfile` as `RUN <command>`.

    This is the authoritative source for the rebuild section. `render_step_commands`
    below re-derives the same lines from `install_method` and is a strictly weaker
    FALLBACK: it answers "what would we run today", which diverges from "what built
    this image" the moment a generator changes. Prefer this; say so when it is empty
    (see `_section_build`) rather than presenting the derived form as the record."""
    return [c for c in (recipe.get("built_commands") or [])
            if isinstance(c, dict) and c.get("command")]


def operator_supplied_steps(recipe: dict) -> list[dict]:
    """The installs whose bytes only the OPERATOR can supply. Their transcript line is
    real but references the build context (`/opt/staged/...`), which the reader does not
    have — so the artifact instruction must accompany the command rather than be
    replaced by it. Returns the install_method dicts, in recipe order."""
    out = []
    for p in _freeze.non_conda_installs(recipe):
        im = p.get("install_method") or {}
        if im.get("artifact_source") == "operator_supplied":
            out.append({"name": p.get("name", ""), "install_method": im})
    return out


def render_step_commands(step: dict) -> list[str]:
    """DERIVE the install line(s) for ONE non-conda step from its `install_method`.

    FALLBACK ONLY — `render_built_commands` is authoritative. This function is a second
    author of a string `install_commands` already wrote, and the two drifted in
    production (see `env_recipe.extract_recipe`'s `built_commands` note). It survives
    for records frozen before the transcript was captured, and for the operator-supplied
    instruction, which is not a command at all and has no transcript equivalent.

    Where it must guess, it states the guess. It must never emit a line that fails when
    pasted: that doctrine is stated at the `binary` branch below and was, for a long
    time, applied only there."""
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
            return [f"# {name}: Rust crate (git)",
                    f'cargo install --git {git} --root "$PREFIX"']
        return [f"# {name}: Rust crate",
                f'cargo install {crate}{("@" + ver) if ver else ""} --root "$PREFIX"']
    if t == "go":
        pkg = im.get("package") or name
        ver = im.get("version") or "latest"
        return [f"# {name}: Go tool", f'GOBIN="$PREFIX/bin" go install {pkg}@{ver}']
    if t == "perl":
        module = im.get("module") or name
        be = (im.get("build_env") or "").strip()
        flags = im.get("cpanm_flags") or "--notest"
        line = f"cpanm {flags} {module}"
        return [f"# {name}: Perl/CPAN module",
                (f"{be} {line}" if be else line)]
    if t == "r_install":
        # THE PRINTED LINE MUST BE THE LINE THAT WORKS. This rendered
        # `install.packages("X")` for EVERY R package and demoted the real source to a
        # trailing comment — so for a Bioconductor package (`BiocGenerics`) the reader
        # was handed a CRAN call that fails, with `BiocManager::install` visible only as
        # prose. Same for a github package. Route on the recorded source instead; the
        # comment now carries provenance, not the working command.
        src = (im.get("source") or "").strip()
        low = src.lower()
        if low.startswith("github:") or "github.com" in low:
            repo = src.split(":", 1)[-1] if low.startswith("github:") else src
            return [f"# {name}: R package (GitHub)",
                    "Rscript -e 'if (!requireNamespace(\"remotes\", quietly=TRUE)) "
                    "install.packages(\"remotes\")'",
                    f"Rscript -e 'remotes::install_github(\"{repo}\")'"]
        if "bioconductor" in low or "biocmanager" in low:
            return [f"# {name}: R package (Bioconductor)",
                    "Rscript -e 'if (!requireNamespace(\"BiocManager\", quietly=TRUE)) "
                    "install.packages(\"BiocManager\")'",
                    f"Rscript -e 'BiocManager::install(\"{name}\")'"]
        return [f"# {name}: R package (CRAN)",
                f"Rscript -e 'install.packages(\"{name}\", repos=\"https://cloud.r-project.org\")'"]
    if t in ("pip", "pip_install"):
        # THERE WAS NO PIP BRANCH AT ALL — the commonest long-tail tier fell through to
        # the generic "see machine recipe" line, i.e. the rebuild section named the tool
        # and gave the reader nothing to run.
        spec = im.get("spec") or im.get("package") or name
        ver = str(im.get("version") or "").strip()
        # `pip_flags` is a LIST from install_pip_package (`["--no-deps", ...]`) but a
        # plain string on some hand-authored records. Accept both rather than assuming.
        _pf = im.get("pip_flags") or ""
        flags = " ".join(str(f) for f in _pf) if isinstance(_pf, (list, tuple)) else str(_pf).strip()
        if "==" not in spec and ver:
            spec = f"{spec}=={ver}"
        return [f"# {name}: pip package",
                " ".join(x for x in ["pip install", flags, spec] if x)]
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
    locked = bool(recipe.get("conda_lock"))
    if not deps and locked:
        # THE ENV LAYER EXISTS BUT NOBODY ASKED FOR IT BY NAME. `conda_deps` is the
        # REQUEST; `conda_lock` is what was GOT. An env whose conda layer was solved
        # from an install primitive's own specs (a pip-with-flags tool, say) records an
        # empty conda_deps and a full pixi.toml/pixi.lock — so this early-returned [] and
        # the recipe rendered NO step 1 at all, while step 2 went on to say
        # `pixi run bash -c '...'`. The reader was told to run a command inside an
        # environment the document never created. Absence of the request read as absence
        # of the layer; the lock is the proof it is there.
        #
        # And materializing the lock is not a reconstruction — it is literally what the
        # build ran (`engine.materialize_lines()` → `pixi install --locked`), so this is
        # the conda layer's half of the transcript.
        return ["# 1. materialize the env from the captured lock — this is exactly what "
                "the build ran, and it re-solves nothing:",
                *_fence(["# write pixi.toml and pixi.lock from the machine recipe's "
                         "`conda_lock` block (*.recipe.yaml), then:",
                         "pixi install --locked"]),
                "> The lock pins every package by URL+sha256, so this reproduces the "
                "identical package set on any machine — which a `conda create` re-solve "
                "does not. The commands in step 2 run INSIDE this env (that is what the "
                "`pixi run` prefix does).", ""]
    if not deps:
        return []
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
    # THIS WAS NOT A RUNNABLE COMMAND, AND IT WAS NOT A USEFUL ONE EITHER.
    #
    # Not runnable: it truncated at 24 names and appended ` \  # +85 more …` — a line
    # continuation followed by a comment, which breaks the continuation, so pasting it
    # ran an apt-get that ended mid-list.
    #
    # Not useful: `system_packages` is the image's FULL dpkg inventory, so the list was
    # led by `base-files`, `dpkg`, `bash`, `coreutils` — the base image's own contents.
    # Nobody installs those; they arrive with `FROM debian:bookworm-slim`. Presenting
    # them as a build step invites the reader to run a large, wrong command.
    #
    # An inventory is not an instruction. State it as an inventory, point at the SBOM
    # for the full list, and let the transcript carry the apt lines the build ACTUALLY
    # ran (they are baked into the image via emit_dockerfile's own `_apt` block, and the
    # recipe's `apt_snapshot` is what makes them reproducible).
    return [f"> **System (apt) packages — INVENTORY, not a build step.** The shipped image "
            f"carries {len(names)} dpkg packages, most of them the base image's own "
            f"contents rather than anything this build requested. Do not install these by "
            f"hand: the apt layer is reproduced by the machine recipe's `apt_snapshot` "
            f"(a snapshot.debian.org pin), and versions are in the SBOM "
            f"(`*.attestation.json`).", "",
            "<details><summary>full package list</summary>", "",
            *_fence(names, lang="text"),
            "</details>", ""]


def _section_build(recipe: dict, record: Optional[dict]) -> list[str]:
    name = recipe.get("name", "env")
    ver = recipe.get("version", "")
    tag = f"{name}:{ver}" if ver else f"{name}:latest"
    base = __import__("agent.skills.container_build",
                      fromlist=["BASE_IMAGE"]).BASE_IMAGE.split("@")[0]
    built = render_built_commands(recipe)
    out = ["## Option B — rebuild from scratch (container-native build)", "",
           "The env was built by installing everything INTO an image and validating it "
           "there (validated == shipped). To reproduce by hand, in a Linux build context "
           f"(base image `{base}` pinned by digest in the machine recipe):", ""]
    out += _apt_block(record)
    out += _conda_block(recipe)
    # `$PREFIX` appeared in the cargo/go lines and was DEFINED NOWHERE in the document —
    # a paste that silently installs to the wrong place (or fails under `set -u`). If a
    # rendered command references it, the recipe has to say what it is.
    needs_prefix = any("$PREFIX" in ln
                       for p in _freeze.non_conda_installs(recipe)
                       for ln in render_step_commands(p))
    if needs_prefix:
        out += ["Some steps below install into the environment's prefix. Set it once "
                "(this is the conda/pixi env the previous step created):", ""]
        out += _fence(['export PREFIX="$CONDA_PREFIX"   '
                       '# or the absolute path of the env you just created'])
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
    if built:
        # THE TRANSCRIPT WINS. These are the exact strings this image was built from —
        # emit_dockerfile baked each one as `RUN <command>` — so the reader runs what ran
        # rather than a per-tier paraphrase of it. See extract_recipe's `built_commands`.
        out += ["# 2. install the non-conda tools — these are the EXACT commands that "
                "built this image, recorded at build time and reproduced verbatim:"]
        for c in built:
            body = []
            if c.get("purpose"):
                body.append(f"# {c['purpose']}")
            body.append(c["command"])
            out += _fence(body)
        # An operator-supplied artifact's transcript line is real but reads from the
        # build context (`/opt/staged/…`), which the reader does not have. The command
        # is not wrong; it is not SUFFICIENT — so the instruction accompanies it.
        for op in operator_supplied_steps(recipe):
            out += _fence(render_step_commands(op))
    elif longtail:
        # No transcript on this record (frozen before it was captured). Render the
        # DERIVED form and label it as derived — a reader must be able to tell a
        # recorded command from a reconstructed one, because only the first is
        # evidence of how these bytes came to exist.
        out += ["# 2. install the non-conda tools (each baked + validated in the image):",
                "",
                "> ⚠ **Derived, not recorded.** This env was frozen before the build "
                "transcript was captured, so the commands below are RECONSTRUCTED from "
                "the recorded install methods rather than quoted from the build. They "
                "describe how this tool installs; they are not proof of how THIS image "
                "was built. Re-freeze to get the verbatim transcript.", ""]
        for p in longtail:
            cmds = render_step_commands(p)
            if cmds:
                out += _fence(cmds)
    out += ["# 3. bake into an image, then convert to a cluster .sif:"]
    # "assemble the above into a Dockerfile" left the reader to work out the one thing
    # this step is for. With the transcript recorded, the mapping is exact and can be
    # stated: each command in step 2 is one RUN line, in the order shown.
    out += _fence([f"# Dockerfile — one RUN per command above, in order:",
                   f"#   FROM {base}",
                   f"#   RUN <command 1>",
                   f"#   RUN <command 2>   ... etc",
                   f"docker build -t {tag} ."] if built else
                  [f"# (assemble the above into a Dockerfile and: docker build -t {tag} .)"])
    out += _local_sif_block(tag)
    # The "how do I check this rebuilt correctly" sentence is emitted ONCE, method-aware,
    # by _verify_section at the end of the document. A second copy lived here and said the
    # same thing in different words — and this codebase's stated failure mode is one claim
    # in two places.
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
    elif method in _AUTHORS_METHODS:
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
        # ONE READING OF "WHAT VERSION IS THIS TOOL". This list read `b.version` alone
        # while the requested-vs-installed table eight lines above read the shared
        # `_resolved_version` leaf — and they disagreed IN THE SAME DOCUMENT: the table
        # said `nanoq | 0.10.0 | 0.10.0` and this list said `nanoq (version unrecorded)`,
        # because a cargo/go/pip binary's version lands in the SBOM, not on the
        # ShippedBinary row. A reader auditing "did I get what I asked for" was given two
        # answers and no way to tell which was the artifact.
        #
        # `_resolved_version` is the authority (it is what the ENV report cites), so ask
        # it first and keep `b.version` as the fallback for a binary the SBOM never saw.
        # Absence still reads "version unrecorded" — the point is that it now means
        # nobody observed one, rather than that this renderer looked in one place.
        from agent.skills.env_report_helpers import (
            _pkg_index, _resolved_version, _verif_index)
        _pidx = _pkg_index(record.get("resolved_packages")
                           or recipe.get("resolved_packages") or [])
        _vidx = _verif_index(record.get("verifications") or [])
        _shipped = record.get("shipped_binaries") or recipe.get("shipped_binaries") or []
        for b in sb:
            # `command` used to be read here as if it were the tool name, but the
            # freeze_tools producer wrote the literal shell line into that key — so
            # this rendered a whole `git clone …` command where a tool name belongs.
            # `tool` is now the tool; `install_command` is the command.
            ver = (_resolved_version(b.tool, _pidx.get(b.tool.lower()),
                                     _vidx.get(b.tool.lower()), _shipped) or "").strip()
            L += [f"- `{b.tool}` "
                  + (f"({ver})" if ver else "(version unrecorded)")
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

    L += _verify_section(method, content_digest, recipe)
    return "\n".join(L)


def _verify_section(method: str, content_digest: str, recipe: dict) -> list[str]:
    """"Verify what you rebuilt" — SAYING WHAT verify_env_recipe WILL ACTUALLY DO TO THIS ENV.

    This section used to be unconditional, and it told every reader that
    `verify_env_recipe` "rebuilds the image and checks it converges to the recorded content
    digest". Measured 2026-08-07 by rendering one recipe of each method and grepping the
    output: all four carried the sentence, and for two of the three methods it is false.

      * **authors-dockerfile** — `verify_env_recipe` runs NOTHING. It returns
        `refused / freeze.recipe_verify_unavailable`, `success: false`, "no check was run",
        because a Dockerfile build is not bit-reproducible so digest convergence would fail
        for a CORRECT recipe. Declining to check is the right call; telling the reader to
        run a check that will decline is not. The rendered document contained no caveat of
        any kind — a scan for "refus", "not verifiable", "cannot be verified", "no check"
        and "does not apply" found none of them.
      * **adopt** — the branch does run, but it `docker pull`s and compares a registry
        manifest digest. It does not rebuild anything. The branch's own return string says
        so: "Not a from-source rebuild."

    The build recipe is one of the three artifacts that ARE this project's acceptance
    criterion — a human must be able to rebuild from it. A recipe that instructs its reader
    to run a verification which will refuse is a recipe that lies to them, and it shipped:
    the sentence is on disk in `talos_authors.recipe.md:72` and `multiqc.recipe.md:61`.

    So the bullet is selected off `method`, right where every other method-specific choice
    in this file is made. The wording tracks the refusal that `verify_env_recipe` itself
    composes, so the two surfaces cannot drift into describing one behaviour two ways.
    """
    out = ["## Verify what you rebuilt", "",
           "- **Exact bytes:** the shipped `.sif`'s sha256 is recorded in the run report / "
           "delivery manifest — compare it after transfer."]
    short = _short(content_digest)
    if method == "adopt":
        out += ["- **Re-pull convergence:** run `verify_env_recipe` against the machine "
                "`*.recipe.yaml` — it re-pulls the published image and checks the registry "
                f"manifest digest still matches what was recorded (`{short}`). This is a "
                "content-address check on identical bytes, NOT a from-source rebuild."]
    elif method in _AUTHORS_METHODS:
        ds = recipe.get("dockerfile_source") or {}
        rebuild = ", ".join(
            f"{k}={v!r}" for k, v in (("repo", ds.get("repo")), ("ref", ds.get("commit")))
            if v
        )
        out += ["- **From-source convergence: there is no digest check for this env, and "
                "`verify_env_recipe` will say so rather than run one.** An authors-Dockerfile "
                "build is not bit-reproducible — apt mirrors, timestamps and upstream tags all "
                "move — so a digest comparison would fail for a recipe that is perfectly "
                "correct. To reproduce it by hand, re-run "
                + (f"`build_env_from_authors_recipe({rebuild})`" if rebuild
                   else "`build_env_from_authors_recipe(...)` with the repo and ref recorded above")
                + " and compare the tools' evidence output."]
    else:
        out += ["- **From-source convergence:** run `verify_env_recipe` against the machine "
                "`*.recipe.yaml` — it rebuilds the image from this recipe alone and checks it "
                f"converges to the recorded content digest (`{short}`). The conda layer is "
                "REPLAYED from the pinned lock below rather than re-solved, so that layer is "
                "identical by construction; what convergence demonstrates is that the recipe "
                "is self-contained and that every other layer rebuilds the same way."]
    return out + [""]
