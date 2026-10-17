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
    adopt                    an existing image adopted as it is: a published image
                             pulled BY DIGEST, or a local one
    authors-dockerfile       the tool's OWN Dockerfile at a pinned source commit

Pure assembly — no docker, no network, no clock. Deterministic given its inputs.
"""

from __future__ import annotations

import re
from typing import Any, Optional

from agent.skills import freeze as _freeze
from agent.skills.env_recipe import AUTHORS_METHODS as _AUTHORS_METHODS, requested_ref
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

    FALLBACK ONLY — `render_built_commands` (the recorded transcript) is authoritative.
    This renders records that carry no transcript, and the operator-supplied
    instruction, which is not a command and has no transcript equivalent.

    Where it must guess, it states the guess; a source the record does not carry
    renders as a `<placeholder>` (`<jar_url>`, `<repo_url>`, `<binary_url>`) to fill in."""
    im = step.get("install_method") if isinstance(step.get("install_method"), dict) else {}
    name = step.get("tool") or step.get("name") or im.get("name") or "tool"
    t = (im.get("type") or step.get("type") or "").strip()

    if t == "jar":
        url = im.get("source") or im.get("jar_url") or "<jar_url>"
        # the JRE this env used: apt's default on the Debian base is Java 17, and a
        # tool built for another major version does not run on it
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
        # An artifact only the operator can obtain gets an instruction, not a command:
        # its recorded URL is a path on the machine that froze it.
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
        # the install call the recorded source needs: GitHub, Bioconductor, or CRAN
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
        spec = im.get("spec") or im.get("package") or name
        ver = str(im.get("version") or "").strip()
        # `pip_flags`: a list from install_pip_package, a string on hand-authored records
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


def _conda_block(recipe: dict, step: int) -> list[str]:
    """Step `step`, the conda/pip layer: materialized from the captured lock when there
    is one (what the build ran, `pixi install --locked`), else re-solved from the specs.
    A lock with no named specs is still a layer the later steps run inside."""
    deps = [d for d in (recipe.get("conda_deps") or []) if d]
    if recipe.get("conda_lock"):
        return [f"# {step}. materialize the env from the captured lock — this is exactly what "
                "the build ran, and it re-solves nothing:",
                *_fence(["# write pixi.toml and pixi.lock from the machine recipe's "
                         "`conda_lock` block (*.recipe.yaml), then:",
                         "pixi install --locked"]),
                "> The lock pins every package by URL+sha256, so this reproduces the "
                "identical package set on any machine — which a `conda create` re-solve "
                "does not"
                + (f" (the specs asked for: {', '.join(f'`{d}`' for d in deps)})" if deps else "")
                + ". Later steps run INSIDE this env (that is what the `pixi run` prefix does).", ""]
    if not deps:
        return []
    return [f"# {step}. create the conda/pip env (re-solved from specs):",
            *_fence([f"conda create -y -n {recipe.get('name','env')} {_channels(recipe)} \\",
                     "    " + " ".join(deps)])]


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
    # `system_packages` is the image's whole dpkg inventory, mostly the base image's own
    # contents: stated as an inventory, never as an apt-get line to run. The apt layer
    # the build ran is reproduced by the recipe's `apt_snapshot`.
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
    conda = _conda_block(recipe, 1)
    out += conda
    step = 2 if conda else 1
    # a rendered command that installs into `$PREFIX` needs it defined first
    needs_prefix = any("$PREFIX" in ln
                       for p in _freeze.non_conda_installs(recipe)
                       for ln in render_step_commands(p))
    if needs_prefix:
        out += ["Some steps below install into the environment's prefix. Set it once "
                "(this is the conda/pixi env the previous step created):", ""]
        out += _fence(['export PREFIX="$CONDA_PREFIX"   '
                       '# or the absolute path of the env you just created'])
    # the steps freeze itself replays (`non_conda_installs`), each with its install_method:
    # a raw install_step carries none (it sits under installed_packages[].install_method),
    # so a loop over raw steps would drop every non-conda step, the operator-supplied one too
    longtail = [p for p in _freeze.non_conda_installs(recipe) if render_step_commands(p)]
    if built:
        # the transcript: the exact strings emit_dockerfile baked as `RUN <command>`
        out += [f"# {step}. install the non-conda tools — these are the EXACT commands that "
                "built this image, recorded at build time and reproduced verbatim:"]
        step += 1
        for c in built:
            body = []
            if c.get("purpose"):
                body.append(f"# {c['purpose']}")
            body.append(c["command"])
            out += _fence(body)
        # an operator-supplied artifact's line reads from the build context, which the
        # reader does not have: the instruction to obtain it goes with it
        for op in operator_supplied_steps(recipe):
            out += _fence(render_step_commands(op))
    elif longtail:
        # No transcript: the commands are derived from the install methods and labelled
        # so. An install through the engine's conda/PyPI layer never has one (its lock
        # pins it); a RUN-baked tier frozen before transcripts has none either. The
        # warning states the absence and both routes, not which one this was.
        lock_note = (
            " — but an install routed through the engine's conda/PyPI layer (a plain "
            "`pip install`) never produces one: its exact bytes are pinned by the "
            "lock carried in `recipe.yaml`, which is the stronger anchor."
            if recipe.get("conda_lock") else ".")
        out += [f"# {step}. install the non-conda tools (each baked + validated in the image):",
                "",
                "> ⚠ **Derived, not recorded.** This recipe carries no verbatim build "
                "transcript for these steps, so the commands below are RECONSTRUCTED "
                "from the recorded install methods rather than quoted from the build. "
                "They describe how each tool installs; they are not proof of how THIS "
                "image was built. A freeze at current HEAD records RUN-baked install "
                "commands verbatim, so for those tiers a re-freeze captures the "
                "transcript" + lock_note, ""]
        for p in longtail:
            cmds = render_step_commands(p)
            if cmds:
                out += _fence(cmds)
        step += 1
    out += [f"# {step}. bake into an image, then convert to a cluster .sif:"]
    out += _fence([f"# Dockerfile — one RUN per command above, in order:",
                   f"#   FROM {base}",
                   f"#   RUN <command 1>",
                   f"#   RUN <command 2>   ... etc",
                   f"docker build -t {tag} ."] if built else
                  [f"# (assemble the above into a Dockerfile and: docker build -t {tag} .)"])
    out += _local_sif_block(tag)
    # how to check the rebuild is said once, by _verify_section
    return out


def _image_origin(recipe: dict, record: Optional[dict]) -> str:
    """`registry` | `local` | `built` | "" — where an adopted image came from, as the
    freeze observed it. The recipe's copy first (it travels alone), then the record's."""
    return str(recipe.get("image_origin") or (record or {}).get("image_origin") or "")


#: What a LOCAL adopt image means for the reader, said once and quoted wherever the
#: image is shown. Plain words, because this is the sentence that stops a hand-built
#: image from passing as a provenance.
_LOCAL_ORIGIN_NOTE = (
    "> **A local image whose build this record did not observe.** The image was already "
    "in the local daemon when it was frozen; nothing in this record saw how it was made, "
    "so this recipe can vouch for what the image CONTAINS (the validated tools and the "
    "SBOM below) but not for how to build it again. To record a build, use "
    "`build_env_from_authors_recipe` — with `patches=` if the authors' Dockerfile needs a "
    "fix — so the fix is written down beside the build instead of applied by hand.")


def _section_adopt(recipe: dict, record: Optional[dict]) -> list[str]:
    img = recipe.get("adopt_image") or ""
    name = recipe.get("name", "env")
    tag = f"{name}:local"
    out = ["## Option B — rebuild from scratch (adopt a published image)", ""]
    # a pull command only for a reference pinned by a registry digest
    if "@sha256:" not in img:
        pin_err = (record or {}).get("adopt_pin_error") or ""
        # the unpullable reference is not named, not even in the warning: a reader who
        # skims to the monospace text would paste it
        out += ["> **NOT PINNABLE — this image has no registry manifest digest"
                + (f": {pin_err}" if pin_err else "") + ".** It was not pulled from a "
                "registry, so no reference exists that anyone else could pull: the local "
                "tag resolves only on the machine that built it. The pull command is "
                "omitted rather than guessed. Deliver this env via the `.sif`/tarball "
                "route instead, or re-freeze against a pushed image to get a pinnable "
                "recipe.", ""]
        if _image_origin(recipe, record) == "local":
            out += [_LOCAL_ORIGIN_NOTE, ""]
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
    tag_ref = requested_ref(ds)
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

    # without a repo and commit the build cannot be pinned: the gap is stated and no
    # placeholder command is rendered
    missing = [n for n, v in (("repo", repo), ("commit", commit)) if not v]
    if missing:
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
                   + (f"   # requested as {tag_ref}" if tag_ref else "")])
    out += _patches_block(ds)
    out += _fence([" \\\n".join(build)])
    out += _local_sif_block(img)
    pin = f"> Source pin: `{repo}` commit `{commit}`"
    pin += f" (requested as `{tag_ref}`)." if tag_ref else "."
    if recipe_path:
        pin += f" Built from `{recipe_path}`."
    if build_args:
        pin += (" Build args are pinned above — without them the Dockerfile's own `ARG`"
                " defaults apply, which is a DIFFERENT image.")
    out += [pin + " The Dockerfile is included alongside this recipe for inspection.", ""]
    return out


def _patches_block(ds: dict) -> list[str]:
    """The edits applied to the authors' source before the build — one entry per patch:
    the file, the reason, and the literal substitution. Rendered whenever the source
    carries patches, so a reader rebuilding from the pinned commit knows the Dockerfile
    that was built is NOT the one at that commit, and exactly how it differs. An authors
    build with no patches renders nothing here: the pin above is then the whole story."""
    patches = [p for p in (ds.get("patches") or []) if isinstance(p, dict)]
    if not patches:
        return []
    n = len(patches)
    out = [f"### Patches to apply to the checkout before building ({n})", "",
           f"The build edited the checkout before `docker build` — {n} exact "
           f"substitution{'s' if n != 1 else ''}, written by the agent and recorded so the "
           "build is reproducible: apply each one to the pinned commit, then build. "
           "The sha256 values are of the whole file before and after the edit.", ""]
    for i, p in enumerate(patches, 1):
        file = str(p.get("file") or "(file not recorded)")
        reason = str(p.get("reason") or "(no reason recorded)")
        out += [f"**{i}. `{_md_inline(file)}`** · {_md_inline(reason)}", ""]
        out += _fence([f"--- find", str(p.get("find") or ""), f"+++ replace",
                       str(p.get("replace") or "")], lang="diff")
        sb, sa = p.get("sha256_before") or "", p.get("sha256_after") or ""
        if sb or sa:
            out += [f"sha256 before `{_short(sb)}` → after `{_short(sa)}`", ""]
    return out


# ---------------------------------------------------------------------------
# top-level render
# ---------------------------------------------------------------------------
def _installed_versions_table(recipe: dict, record: dict) -> list[str]:
    """A requested-vs-OBSERVED-installed row per primary tool, ⚠ on divergence — the
    same at-a-glance honesty the ENV report carries, so the recipe never implies the
    REQUESTED version is what shipped.

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
    # `ver` is the REQUESTED version, not an observed one: a labelled metadata row, never
    # the title; the requested-vs-installed table below carries what was observed
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
    L += [f"| **Build method** | `{method}` |"]
    origin = _image_origin(recipe, record)
    if origin:
        # One line a reader sees before any command: whether this image is something
        # anyone can pull, something this record built, or something it only found.
        origin_said = {
            "registry": "registry — pulled from a registry and pinned by its manifest digest",
            "local": "local — a local image whose build this record did not observe",
            "built": "built — built under this record from the authors' Dockerfile",
        }.get(origin, origin)
        L += [f"| **Image origin** | {origin_said} |"]
    L += [
          f"| **Platform** | `{platform}` |",
          f"| **Content digest** | `{_short(content_digest)}` |", ""]
    L += ["> This recipe is rendered **purely from the verified freeze record** — every "
          "command reflects how the environment was actually built, not a description "
          "written after the fact. There are two ways to obtain the env; prefer Option A.", ""]

    # Option A — reuse the exact bytes (always available; strongest guarantee)
    pullable = "@sha256:" in str(recipe.get("adopt_image") or "")
    L += ["## Option A — reuse the exact image (no rebuild) — preferred", "",
          "The frozen image *is* the environment. Ship the `.sif` "
          + ("(or pull the image by digest) " if pullable else
             "(or the image itself, which its image digest identifies) ")
          + "and run it — byte-identical, nothing to install:", ""]
    img = record.get("image") or (recipe.get("adopt_image") or f"{name}:{ver or 'latest'}")
    image_digest = record.get("image_digest")
    # an adopted or authors' image is addressed by its own digest; a build here also by its inputs'
    L += _fence([f"apptainer exec --cleanenv {name}.sif <command>",
                 f"# image: {img}",
                 f"# image digest: {image_digest or '(not recorded)'}"]
                + ([f"# content digest (of the build's inputs): {content_digest}"]
                   if content_digest and content_digest != image_digest else []))

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
        # the version the table above and the ENV report read, by `_resolved_version` over
        # every source it knows; "version unrecorded" when it resolves none
        from agent.skills.env_report_helpers import (
            _pkg_index, _resolved_version, _verif_index)
        _pidx = _pkg_index(record.get("resolved_packages")
                           or recipe.get("resolved_packages") or [])
        _vidx = _verif_index(record.get("verifications") or [])
        _shipped = record.get("shipped_binaries") or recipe.get("shipped_binaries") or []
        for b in sb:
            # `tool` names the tool; `install_command` is a command, never a name
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

    # IDENTITY DISCLOSURE — the tool's OWN words, self-described + UNVERIFIED.
    # Prefer the recipe's copy (travels with the reproduction bytes); fall back to the record.
    idents = _tool_identities(recipe) or _tool_identities(record)
    described = [i for i in idents if i.self_description]
    collided = [i for i in idents if i.collision is not None]
    if described or collided:
        L += ["**What each tool says it is** — *self-described, unverified.* Each line is the "
              "package's OWN one-line description from the registry it shipped from: a claim to "
              "READ, not a validated capability. It is here so a wrong-domain match is catchable "
              "(a `cellranger` that turns out to be a spreadsheet-range parser).", ""]
        for i in described:
            src = f" _({i.source})_" if i.source else ""
            L += [f"- `{i.tool}`{src}: {_md_inline(i.self_description)}"]
        # a same-named registry package from another project: the collision is named,
        # its description is not printed as this tool's
        for i in collided:
            c = i.collision
            L += [f"- `{i.tool}` _({c.registry})_: **name collision** — a same-named "
                  f"{c.registry} package exists elsewhere ({_md_inline(c.points_at)}); "
                  f"it is not {_md_inline(c.known_repo)}, so its description is withheld."]
        L += [""]

    L += _verify_section(method, content_digest, recipe)
    return "\n".join(L)


def _verify_section(method: str, content_digest: str, recipe: dict) -> list[str]:
    """"Verify what you rebuilt": what `verify_env_recipe` does for this build method.
    A container-native build is rebuilt and checked against the recorded content
    digest; an adopted image is re-pulled and its registry digest compared (no
    rebuild); an authors-Dockerfile build is not bit-reproducible, so the tool declines
    to check (`freeze.recipe_verify_unavailable`) and the page says so instead of
    sending the reader to a refusal. The wording follows the refusal
    `verify_env_recipe` composes."""
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
        n_patches = len([p for p in (ds.get("patches") or []) if isinstance(p, dict)])
        rebuild = ", ".join(
            f"{k}={v!r}" for k, v in (("repo", ds.get("repo")), ("ref", ds.get("commit")))
            if v
        )
        if rebuild and n_patches:
            rebuild += ", patches=<the recorded patches>"
        out += ["- **From-source convergence: there is no digest check for this env, and "
                "`verify_env_recipe` will say so rather than run one.** An authors-Dockerfile "
                "build is not bit-reproducible — apt mirrors, timestamps and upstream tags all "
                "move — so a digest comparison would fail for a recipe that is perfectly "
                "correct. To reproduce it by hand, re-run "
                + (f"`build_env_from_authors_recipe({rebuild})`" if rebuild
                   else "`build_env_from_authors_recipe(...)` with the repo and ref recorded above")
                + (f" — the {n_patches} recorded patch{'es' if n_patches != 1 else ''} above "
                   f"would have to be re-applied to the pinned source first" if n_patches else "")
                + " and compare the tools' evidence output."]
    else:
        out += ["- **From-source convergence:** run `verify_env_recipe` against the machine "
                "`*.recipe.yaml` — it rebuilds the image from this recipe alone and checks it "
                f"converges to the recorded content digest (`{short}`). The conda layer is "
                "REPLAYED from the pinned lock below rather than re-solved, so that layer is "
                "identical by construction; what convergence demonstrates is that the recipe "
                "is self-contained and that every other layer rebuilds the same way."]
    return out + [""]
