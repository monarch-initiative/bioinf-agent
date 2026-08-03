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
    return er.extract_recipe(
        {"install_steps": [
            {"tool": "seqtk", "install_method": {
                "type": "source", "name": "seqtk",
                "source": "https://github.com/lh3/seqtk",
                "commit_sha": "a" * 40, "build_command": "make", "bin_path": "seqtk"}},
            {"tool": "mosdepth", "install_method": {
                "type": "binary", "name": "mosdepth",
                "binary_url": "https://example/mosdepth", "asset_sha256": "d" * 64}},
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
