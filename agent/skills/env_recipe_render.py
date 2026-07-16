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
                             cargo/go/perl/r/synthesized/spack), baked into an image
    adopt                    a published BioContainer pulled BY DIGEST (pure-conda)
    authors-dockerfile       the tool's OWN Dockerfile at a pinned source commit

Pure assembly — no docker, no network, no clock. Deterministic given its inputs.
"""

from __future__ import annotations

from typing import Any, Optional


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
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
        return [f"# {name}: Java jar tool",
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
        url = im.get("binary_url") or "<binary_url>"
        sha = (im.get("asset_sha256") or "").strip()
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
    if t == "spack":
        return [f"# {name}: Spack package", f"spack install {im.get('package') or name}"]
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
    steps = [s for s in (recipe.get("install_steps") or []) if isinstance(s, dict)]
    longtail = [s for s in steps if render_step_commands(s)]
    if longtail:
        out += ["# 2. install the non-conda tools (each baked + validated in the image):"]
        for s in longtail:
            cmds = render_step_commands(s)
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
    img = recipe.get("adopt_image") or (record or {}).get("image") or "<biocontainer@digest>"
    name = recipe.get("name", "env")
    tag = f"{name}:local"
    out = ["## Option B — rebuild from scratch (adopt a published BioContainer)", "",
           "This env is pure-conda and maps to a published BioContainer, so the recipe is "
           "simply pulling that image BY DIGEST (content-addressed — the digest guarantees "
           "identical bytes) and converting it to a .sif:", ""]
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
    repo = ds.get("repo") or "<repo_url>"
    commit = ds.get("commit") or ds.get("commit_sha") or ""
    tag_ref = ds.get("tag") or ""
    name = recipe.get("name", "env")
    ver = recipe.get("version", "")
    img = f"{name}:{ver}" if ver else f"{name}:latest"
    checkout = tag_ref or commit or "<ref>"
    out = ["## Option B — rebuild from scratch (the tool's OWN Dockerfile)", "",
           "This env was built the authors-recipe-first way: from the tool's own Dockerfile "
           "at a pinned source commit. That is what captures the non-python / compiled "
           "pieces a conda/pip reconstruction silently drops. To reproduce:", ""]
    out += _fence([f"git clone {repo} src",
                   f"cd src && git checkout {checkout}"
                   + (f"   # commit {commit}" if commit and checkout != commit else ""),
                   f"docker build --platform linux/amd64 -t {img} ."])
    out += _local_sif_block(img)
    if commit and tag_ref:
        out += [f"> Source pin: `{repo}` tag `{tag_ref}` (commit `{commit}`). "
                "The Dockerfile is included alongside this recipe for inspection.", ""]
    return out


# ---------------------------------------------------------------------------
# top-level render
# ---------------------------------------------------------------------------
def render_recipe_markdown(recipe: dict, record: Optional[dict] = None) -> str:
    """Render a build recipe dict into a runnable, human-readable Markdown guide.

    `recipe` is the machine recipe (env_recipe.extract_recipe output). `record` is the
    optional EnvCache entry — when present it enriches the provenance section (SBOM
    counts, shipped binaries, system packages) without which the rebuild commands still
    render fully. Deterministic and side-effect-free."""
    recipe = recipe or {}
    record = record or {}
    name = recipe.get("name") or record.get("name") or "env"
    ver = recipe.get("version") or record.get("version") or ""
    platform = recipe.get("platform") or record.get("platform") or "linux/amd64"
    method = (recipe.get("build_method") or record.get("build_method")
              or ("adopt" if record.get("mode") == "adopt" else "container-native-build"))
    content_digest = recipe.get("content_digest") or record.get("content_digest") or ""
    tools = recipe.get("primary_tools") or record.get("requested_tools") or []

    L: list[str] = []
    L += [f"# Environment build recipe — {name}" + (f" {ver}" if ver else ""), ""]
    L += ["| | |", "|---|---|",
          f"| **Primary tools** | {', '.join(tools) if tools else '(none recorded)'} |",
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
    sb = record.get("shipped_binaries") or []
    if sb:
        L += ["**Long-tail binaries baked in** (the pieces a package manager wouldn't give you):", ""]
        for b in sb:
            if isinstance(b, dict):
                L += [f"- `{b.get('command','?')}` "
                      + (f"({b.get('version')})" if b.get("version") else "")
                      + (f" — {b.get('provenance')}" if b.get("provenance") else "")]
        L += [""]
    rp = record.get("resolved_packages") or []
    syp = record.get("system_packages") or []
    if rp or syp:
        L += [f"**SBOM:** {len(rp)} resolved packages (conda/pip closure) · "
              f"{len(syp)} system (apt) packages — full versioned list in the "
              "`*.attestation.json` / `_env_cache_entry.json`.", ""]

    # Verify
    L += ["## Verify what you rebuilt", "",
          "- **Exact bytes:** the shipped `.sif`'s sha256 is recorded in the run report / "
          "delivery manifest — compare it after transfer.",
          "- **From-source convergence:** run `verify_env_recipe` against the machine "
          "`*.recipe.yaml` — it rebuilds the image and checks it converges to the recorded "
          f"content digest (`{_short(content_digest)}`).", ""]
    return "\n".join(L)
