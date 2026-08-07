"""
Tests for env_recipe_render — the HUMAN-READABLE build recipe.

The renderer is pure and deterministic: same recipe dict → same Markdown, no clock /
network / docker. It must cover EVERY build_method a freeze can produce (container-
native-build / adopt / authors-dockerfile) so a recipe always renders regardless of
how the env was installed, and every command must come from the tracked record — never
authored text.
"""

from __future__ import annotations

from agent.skills import env_recipe as er
from agent.skills import env_recipe_render as R
from env_records import shipped_binary as _shipped_binary


# ---------------------------------------------------------------------------
# build_method coverage — a recipe renders for every install path
# ---------------------------------------------------------------------------
def _build_recipe():
    # `install_method` NESTED under installed_packages — the shape every install
    # primitive in agent/mcp_tools/env_tools.py actually writes (six producers, all
    # via an `ip_record`). This fixture used to put it at the STEP level, which no
    # producer has ever written, and the renderer read it there: so these assertions
    # passed against a shape that only existed in this file, while the real
    # "Option B — rebuild from scratch" section shipped EMPTY for every env ever
    # built. Confirmed 2026-08-04 on artifacts on disk — tier_onehop, the env whose
    # whole purpose was proving five non-conda tiers, listed none of them.
    return er.extract_recipe(
        {"install_steps": [
            {"tool": "seqtk", "returncode": 0, "installed_packages": [
                {"name": "seqtk", "channel": "source", "install_method": {
                    "type": "source", "name": "seqtk",
                    "source": "https://github.com/lh3/seqtk",
                    "commit_sha": "a" * 40, "build_command": "make",
                    "bin_path": "seqtk"}}]},
            {"tool": "curl", "returncode": 0, "installed_packages": [
                {"name": "mosdepth", "channel": "binary", "install_method": {
                    "type": "binary", "name": "mosdepth",
                    "binary_url": "https://example/mosdepth",
                    "asset_sha256": "d" * 64}}]},
        ]},
        name="demo", version="1.0", conda_deps=["samtools=1.21", "bcftools=1.21"],
        primary_tools=["seqtk", "mosdepth", "samtools"], content_digest="sha256:" + "ab" * 32,
        conda_lock={"pixi.lock": "..."})


def test_build_method_renders_all_tiers():
    md = R.render_recipe_markdown(_build_recipe(),
                                  {"system_packages": [{"name": "libcurl4"}], "resolved_packages": [1, 2]})
    assert "container-native build" in md
    # the SOURCE tier's clone+build shows the exact pinned repo + build command
    assert "git clone https://github.com/lh3/seqtk" in md and "make" in md
    # the BINARY tier's download + sha256 anchor shows
    assert "mosdepth" in md and "sha256sum -c" in md
    # conda specs surface, and the lock is disclosed
    assert "samtools=1.21" in md and "conda_lock" in md
    # apt layer from the record
    assert "libcurl4" in md


def test_adopt_method_renders_pull_by_digest():
    r = er.extract_recipe(None, name="samtools", version="1.21",
                          conda_deps=["samtools=1.21"], primary_tools=["samtools"],
                          content_digest="sha256:" + "cd" * 32,
                          build_method="adopt",
                          adopt_image="quay.io/biocontainers/samtools@sha256:" + "ef" * 32)
    md = R.render_recipe_markdown(r, {"image": "quay.io/biocontainers/samtools@sha256:" + "ef" * 32})
    assert "adopt a published image" in md
    assert "docker pull quay.io/biocontainers/samtools@sha256:" in md
    # the conda-direct alternative is offered
    assert "conda create" in md


def test_adopt_without_a_registry_digest_refuses_rather_than_print_a_local_tag():
    """The heading promises "the digest guarantees identical bytes", so a recipe with no
    digest must not print a pull command. This fell back to `record["image"]` — for the
    real talos_authors record that is `talos-authors:11.0.0`, a tag that resolves on
    exactly one machine on earth, rendered under a content-addressing claim."""
    r = er.extract_recipe(None, name="talos_authors", version="11.0.0", conda_deps=[],
                          primary_tools=["talos"], content_digest="sha256:" + "7f" * 32,
                          build_method="adopt", adopt_image="")
    md = R.render_recipe_markdown(r, {"image": "talos-authors:11.0.0",
                                      "adopt_pin_error": "no registry manifest digest"})
    assert "NOT PINNABLE" in md
    assert "docker pull talos-authors:11.0.0" not in md
    assert "no registry manifest digest" in md


def test_authors_dockerfile_method_renders_pinned_source():
    r = er.extract_recipe(None, name="talos_authors", version="11.0.0",
                          conda_deps=[], primary_tools=["talos"],
                          content_digest="sha256:" + "7f" * 32,
                          build_method="authors-dockerfile",
                          dockerfile_source={"repo": "https://github.com/populationgenomics/talos",
                                             "tag": "v11.0.1", "commit": "c" * 40})
    md = R.render_recipe_markdown(
        r, {"shipped_binaries": [_shipped_binary(
            tool="bcftools", version="9cef4057",
            provenance="populationgenomics/bcftools csq fork")]})
    assert "the tool's OWN Dockerfile" in md
    assert "git clone https://github.com/populationgenomics/talos" in md
    # PIN TO THE COMMIT, name the tag. A tag is mutable — the repo owner can move v11.0.1
    # tomorrow — so `git checkout v11.0.1` is not a pin, it just looks like one.
    assert f"git checkout {'c' * 40}" in md
    assert "v11.0.1" in md
    # provenance surfaces the long-tail binary the reconstruction would drop
    assert "bcftools" in md and "9cef4057" in md


def test_authors_dockerfile_renders_the_dockerfile_path_and_build_args_it_actually_used():
    """The executor USES `-f {recipe}` and `--build-arg`, then dropped both at the disk
    seam — so every rendered recipe said `docker build .`, rebuilding the ROOT Dockerfile
    and letting the Dockerfile's own ARG defaults silently win. Talos pins
    `ARG BCFTOOLS_VERSION` and `ARG ECHTVAR_VERSION` that way: a human follows the recipe
    and gets a different image, with nothing flagging it."""
    r = er.extract_recipe(None, name="tool_env", version="2.0", conda_deps=[],
                          primary_tools=["tool"], content_digest="sha256:" + "aa" * 32,
                          build_method="authors-dockerfile",
                          dockerfile_source={"repo": "https://github.com/o/r",
                                             "commit": "d" * 40, "tag": "v2.0",
                                             "recipe_path": "docker/Dockerfile.gpu",
                                             "build_args": {"BCFTOOLS_VERSION": "1.23.1"},
                                             "platform": "linux/arm64"})
    md = R.render_recipe_markdown(r, {})
    assert "-f docker/Dockerfile.gpu" in md          # NOT the root Dockerfile
    assert "--build-arg BCFTOOLS_VERSION=1.23.1" in md
    assert "--platform linux/arm64" in md            # not hardcoded amd64
    assert "ARG" in md and "DIFFERENT image" in md   # says WHY the args matter


def test_an_unpinned_authors_source_refuses_instead_of_emitting_a_placeholder():
    """The real on-disk talos_authors record's dockerfile_source is {note, repo, version}
    — no commit, no tag. Re-rendered from that record, this emitted a copy-pasteable
    `git checkout <ref>`, and suppressed its own "Source pin" note precisely because the
    pin was missing (it was gated on `if commit and tag_ref`)."""
    r = er.extract_recipe(None, name="x", version="1", conda_deps=[], primary_tools=["x"],
                          content_digest="sha256:" + "bb" * 32,
                          build_method="authors-dockerfile",
                          dockerfile_source={"repo": "https://github.com/o/r", "note": "hand-made"})
    md = R.render_recipe_markdown(r, {})
    assert "NOT REPRODUCIBLE" in md
    assert "missing `commit`" in md
    assert "<ref>" not in md and "git checkout" not in md


# ---------------------------------------------------------------------------
# actuator model — output is a pure function of the record
# ---------------------------------------------------------------------------
def test_render_is_deterministic():
    r = _build_recipe()
    assert R.render_recipe_markdown(r, {}) == R.render_recipe_markdown(r, {})


def test_every_method_offers_exact_bytes_reuse():
    # Option A (ship the .sif, no rebuild) must be present for every build_method —
    # it's the strongest reproducibility guarantee and always available.
    for r in (_build_recipe(),
              er.extract_recipe(None, name="a", conda_deps=["x"], primary_tools=["x"],
                                build_method="adopt", adopt_image="img@sha256:" + "0" * 64),
              er.extract_recipe(None, name="b", conda_deps=[], primary_tools=["y"],
                                build_method="authors-dockerfile",
                                dockerfile_source={"repo": "r", "commit": "c"})):
        md = R.render_recipe_markdown(r, {})
        assert "Option A — reuse the exact image" in md
        assert "apptainer exec --cleanenv" in md


def test_step_command_renderer_covers_each_tier():
    tiers = {
        "jar": {"type": "jar", "name": "picard", "source": "https://x/picard.jar"},
        "cargo": {"type": "cargo", "name": "rc", "crate": "rc", "version": "1.2"},
        "go": {"type": "go", "name": "gt", "package": "ex.com/gt", "version": "v1"},
        "perl": {"type": "perl", "name": "pm", "module": "Foo::Bar"},
        "synthesized": {"type": "synthesized", "name": "s", "commands": ["./configure", "make"]},
    }
    for tier, im in tiers.items():
        lines = R.render_step_commands({"tool": im["name"], "install_method": im})
        assert lines, f"{tier} rendered nothing"
        joined = "\n".join(lines)
        assert im["name"] in joined


def test_jar_recipe_names_the_jre_it_was_actually_built_with():
    """A jar tool is useless without a JRE, and the two routes are not swappable:
    apt's default is 17, and a tool that asked for 21 will not run on it. The rebuild
    recipe has to say WHICH — it used to mention java only inside a wrapper comment."""
    apt = "\n".join(R.render_step_commands(
        {"tool": "picard", "install_method": {"type": "jar", "name": "picard",
                                              "source": "https://x/picard.jar"}}))
    assert "default-jre-headless" in apt and "openjdk=" not in apt

    conda = "\n".join(R.render_step_commands(
        {"tool": "exomiser", "install_method": {"type": "jar", "name": "exomiser",
                                                "source": "https://x/e.zip",
                                                "java_version": "21"}}))
    assert "openjdk=21" in conda and "default-jre" not in conda


# ---------------------------------------------------------------------------
# the transcript — the recipe quotes what BUILT the image, it does not re-author it
# ---------------------------------------------------------------------------
def _transcript_recipe(built_commands):
    """A container-native recipe whose install_method would DERIVE one command while
    the build actually ran another — the production divergence, in miniature."""
    return er.extract_recipe(
        {"install_steps": [
            {"tool": "cargo", "returncode": 0, "installed_packages": [
                {"name": "nanoq", "channel": "cargo", "install_method": {
                    "type": "cargo", "name": "nanoq", "crate": "nanoq",
                    "version": "0.10.0"}}]},
        ]},
        name="demo", conda_deps=["python=3.11"], primary_tools=["nanoq"],
        content_digest="sha256:" + "cd" * 32, built_commands=built_commands)


def test_the_recipe_quotes_the_command_that_built_the_image():
    """THE POINT OF THE WHOLE SECTION. `render_step_commands` re-derives an install
    line from `install_method`; the build recorded the line it actually exec'd. They
    diverged in production — the image was built by `cargo install nanoq --version
    0.10.0` while the recipe handed the reader `cargo install nanoq@0.10.0 --root
    $PREFIX`, a different flag spelling against an undefined variable.

    When a transcript exists it is the recipe. The derived form must not appear."""
    md = R.render_recipe_markdown(_transcript_recipe(
        [{"tool": "nanoq", "purpose": "Install Rust tool nanoq",
          "command": "cargo install nanoq --version 0.10.0"}]), None)

    assert "cargo install nanoq --version 0.10.0" in md, "the recorded command must be quoted"
    assert "nanoq@0.10.0" not in md, \
        "the DERIVED line must not appear alongside the recorded one — two spellings of " \
        "one command is the defect this replaced"
    assert "Derived, not recorded" not in md, "a recipe WITH a transcript is not derived"


def test_a_recipe_without_a_transcript_says_the_commands_are_reconstructed():
    """Absence stated, never rounded up (the `test_data_integrity: unanchored` rule).
    An env frozen before the transcript was captured still renders useful commands —
    but the reader must be able to tell a quoted command from a reconstructed one,
    because only the first is evidence of how these bytes came to exist."""
    md = R.render_recipe_markdown(_transcript_recipe([]), None)

    assert "Derived, not recorded" in md
    assert "nanoq" in md, "the fallback still has to tell the reader how to install it"


def test_the_derived_r_command_routes_on_the_recorded_source():
    """It rendered `install.packages("X")` for EVERY R package and demoted the real
    source to a trailing comment — so a Bioconductor package's recipe led with a CRAN
    call that FAILS. An instruction you can paste is worse than none (this file's own
    doctrine, previously applied at exactly one branch)."""
    bioc = "\n".join(R.render_step_commands(
        {"tool": "BiocGenerics", "install_method": {
            "type": "r_install", "name": "BiocGenerics", "source": "bioconductor"}}))
    assert "BiocManager::install" in bioc
    assert 'install.packages("BiocGenerics")' not in bioc, \
        "the CRAN call fails for a Bioconductor package — it must not be the printed line"

    cran = "\n".join(R.render_step_commands(
        {"tool": "ape", "install_method": {
            "type": "r_install", "name": "ape", "source": "cran"}}))
    assert 'install.packages("ape"' in cran and "BiocManager" not in cran


def test_pip_renders_a_command_at_all():
    """There was no pip branch: the commonest long-tail tier fell through to the
    generic 'see machine recipe' line, naming the tool and giving nothing to run."""
    lines = R.render_step_commands({"tool": "pyfaidx", "install_method": {
        "type": "pip", "name": "pyfaidx", "spec": "pyfaidx", "version": "0.8.1.4"}})
    joined = "\n".join(lines)
    assert "pip install" in joined and "pyfaidx==0.8.1.4" in joined
    assert "see machine recipe" not in joined


def test_no_rendered_command_references_an_undefined_variable():
    """`$PREFIX` appeared in the cargo/go lines and was defined NOWHERE in the
    document — a paste that installs somewhere else, or fails under `set -u`."""
    md = R.render_recipe_markdown(er.extract_recipe(
        {"install_steps": [{"tool": "cargo", "returncode": 0, "installed_packages": [
            {"name": "nanoq", "channel": "cargo", "install_method": {
                "type": "cargo", "name": "nanoq", "crate": "nanoq", "version": "0.10.0"}}]}]},
        name="d", conda_deps=["python=3.11"], primary_tools=["nanoq"]), None)
    if "$PREFIX" in md:
        assert "export PREFIX=" in md, "a variable a command depends on must be defined"


def test_the_apt_inventory_is_not_presented_as_a_runnable_command():
    """It rendered `apt-get install -y \\` + 24 names + `\\  # +85 more …` — a line
    continuation followed by a comment, which BREAKS the continuation, so pasting it
    ran an apt-get that ended mid-list. It was also led by the base image's own
    contents (`bash`, `dpkg`, `base-files`), which nobody installs."""
    md = R.render_recipe_markdown(
        er.extract_recipe(None, name="d", conda_deps=[], primary_tools=["x"]),
        {"system_packages": [{"name": n} for n in ("bash", "dpkg", "libcurl4")]})
    assert "libcurl4" in md, "the inventory is still disclosed"
    assert "apt-get install" not in md, \
        "an inventory of the base image's own packages is not a build step"


def test_a_lock_only_env_still_gets_an_environment_step():
    """`conda_deps` is the REQUEST; `conda_lock` is what was GOT. An env solved from an
    install primitive's own specs records an EMPTY conda_deps and a full pixi lock — and
    the conda block early-returned on the empty request, so the recipe rendered no step 1
    while step 2 said `pixi run bash -c '...'`. The reader was told to run a command
    inside an environment the document never created.

    Caught on real bytes: env `recipe_transcript_probe`, freeze 2026-08-06."""
    md = R.render_recipe_markdown(er.extract_recipe(
        {"install_steps": [{"tool": "pip", "returncode": 0, "installed_packages": [
            {"name": "pyfaidx", "channel": "pip", "install_method": {
                "type": "pip", "name": "pyfaidx", "spec": "pyfaidx"}}]}]},
        name="d", conda_deps=[], primary_tools=["pyfaidx"],
        conda_lock={"pixi.toml": "...", "pixi.lock": "..."},
        built_commands=[{"tool": "pyfaidx", "purpose": "pyfaidx",
                         "command": "pixi run bash -c 'python -m pip install pyfaidx'"}]), None)

    assert "pixi install --locked" in md, \
        "an env the reader must run commands INSIDE has to be created by the recipe"
    assert md.index("pixi install --locked") < md.index("pixi run bash"), \
        "and created BEFORE the commands that run inside it"


# ---------------------------------------------------------------------------
# "Verify what you rebuilt" — the section must describe THIS env's actual check
# ---------------------------------------------------------------------------
#
# This section used to be emitted unconditionally, ~65 lines after the branch that
# already knew the build_method, and it told every reader that `verify_env_recipe`
# "rebuilds the image and checks it converges to the recorded content digest".
# Measured 2026-08-07 by rendering one recipe of each method and grepping the output:
# all of them carried the sentence, and for two of the three methods it is false.
#
#   authors-dockerfile -> verify_env_recipe runs NOTHING. It returns
#                         `refused / freeze.recipe_verify_unavailable`, success=False,
#                         "no check was run". The rendered document contained no caveat
#                         of any kind: a scan for "refus", "not verifiable", "cannot be
#                         verified", "no check" and "does not apply" found none of them.
#   adopt              -> it `docker pull`s and compares a registry manifest digest. It
#                         does not rebuild anything; the branch's own return string says
#                         "Not a from-source rebuild."
#
# The build recipe is one of the three artifacts that ARE this project's acceptance
# criterion — a human must be able to rebuild from it. A recipe instructing its reader to
# run a verification that will refuse is a recipe that lies to them, and it shipped: the
# sentence is on disk in talos_authors.recipe.md:72 and multiqc.recipe.md:61.

def _verify_block(md: str) -> str:
    return md.split("## Verify what you rebuilt", 1)[1]


def test_the_container_native_recipe_promises_a_real_rebuild():
    md = R.render_recipe_markdown(_build_recipe(), {})
    v = _verify_block(md)
    assert "rebuilds the image from this recipe alone" in v
    # …and is honest about which layer that does NOT cover. The replay runs
    # `pixi install --locked` — no solve — so conda agreement is by construction.
    assert "REPLAYED from the pinned lock" in v and "rather than re-solved" in v


def test_the_adopt_recipe_says_re_pull_not_rebuild():
    r = er.extract_recipe(None, name="samtools", version="1.21",
                          conda_deps=["samtools=1.21"], primary_tools=["samtools"],
                          content_digest="sha256:" + "cd" * 32, build_method="adopt",
                          adopt_image="quay.io/biocontainers/samtools@sha256:" + "ef" * 32)
    v = _verify_block(R.render_recipe_markdown(r, {}))
    assert "re-pulls the published image" in v
    assert "NOT a from-source rebuild" in v
    assert "rebuilds the image" not in v


def test_the_authors_dockerfile_recipe_does_not_send_the_reader_to_a_refusal():
    """The load-bearing one. It must say the check does not exist for this env, and point
    at the thing that DOES reproduce it."""
    r = er.extract_recipe(None, name="talos_authors", version="11.0.0", conda_deps=[],
                          primary_tools=["talos"], content_digest="sha256:" + "7f" * 32,
                          build_method="authors-dockerfile",
                          dockerfile_source={"repo": "https://github.com/populationgenomics/talos",
                                             "commit": "c" * 40})
    v = _verify_block(R.render_recipe_markdown(r, {}))
    assert "there is no digest check for this env" in v
    assert "will say so rather than run one" in v
    assert "build_env_from_authors_recipe" in v
    assert "populationgenomics/talos" in v, "the rebuild instruction must carry the pin"


def test_the_verify_instruction_appears_exactly_once():
    """A near-duplicate of this sentence lived inside _section_build as well, so a
    container-native recipe carried the claim twice in different words — one claim in two
    places is this codebase's stated failure mode."""
    md = R.render_recipe_markdown(_build_recipe(), {})
    assert md.count("verify_env_recipe") == 1
