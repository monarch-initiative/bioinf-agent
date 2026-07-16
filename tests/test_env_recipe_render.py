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
    assert "adopt a published BioContainer" in md
    assert "docker pull quay.io/biocontainers/samtools@sha256:" in md
    # the conda-direct alternative is offered
    assert "conda create" in md


def test_authors_dockerfile_method_renders_pinned_source():
    r = er.extract_recipe(None, name="talos_authors", version="11.0.0",
                          conda_deps=[], primary_tools=["talos"],
                          content_digest="sha256:" + "7f" * 32,
                          build_method="authors-dockerfile",
                          dockerfile_source={"repo": "https://github.com/populationgenomics/talos",
                                             "tag": "v11.0.1", "commit": "c" * 40})
    md = R.render_recipe_markdown(
        r, {"shipped_binaries": [{"command": "bcftools", "version": "9cef4057",
                                  "provenance": "populationgenomics/bcftools csq fork"}]})
    assert "the tool's OWN Dockerfile" in md
    assert "git clone https://github.com/populationgenomics/talos" in md
    assert "git checkout v11.0.1" in md
    # provenance surfaces the long-tail binary the reconstruction would drop
    assert "bcftools" in md and "9cef4057" in md


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
