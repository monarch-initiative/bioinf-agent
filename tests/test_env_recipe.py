"""
Tests for env_recipe — the self-contained build recipe + rebuild-from-recipe check.
extract_recipe is pure; rebuild_from_recipe's build is injected so the match-logic is
testable without Docker. The honest claim is COMPLETENESS + LOCAL DETERMINISM only.
"""

from __future__ import annotations

from agent.skills import env_recipe as er


def _draft():
    return {"install_steps": [
        {"tool": "git", "subcommand": "synthesize",
         "installed_packages": [{"name": "hic_assembler", "channel": "git",
                                 "install_method": {"type": "synthesized", "commit_sha": "abc",
                                                    "commands": [{"command": "make",
                                                                  "provenance": {"source": "extracted"}}]}}]},
        {"tool": "conda", "subcommand": "install", "installed_packages": [{"name": "samtools"}]},
    ]}


def test_extract_recipe_is_self_contained():
    r = er.extract_recipe(_draft(), name="hic", version="1.0",
                          conda_deps=["samtools=1.21"], primary_tools=["hic_assembler"],
                          platform="linux/amd64", content_digest="sha256:deadbeef")
    assert r["recipe_version"] == er.RECIPE_VERSION
    assert r["name"] == "hic" and r["conda_deps"] == ["samtools=1.21"]
    assert r["primary_tools"] == ["hic_assembler"]
    assert r["content_digest"] == "sha256:deadbeef"
    # the non-conda install_method (with synthesized commands+provenance) is carried verbatim
    im = r["install_steps"][0]["installed_packages"][0]["install_method"]
    assert im["type"] == "synthesized" and im["commands"][0]["provenance"]["source"] == "extracted"


def test_extract_recipe_handles_no_draft():
    r = er.extract_recipe(None, name="x", conda_deps=["bwa"], primary_tools=["bwa"])
    assert r["install_steps"] == [] and r["conda_deps"] == ["bwa"]


def test_rebuild_passes_recipe_through_to_build_and_matches():
    captured = {}

    def fake_build(spec, **kw):
        captured["spec"] = spec
        captured["kw"] = kw
        return {"success": True, "content_digest": "sha256:deadbeef", "stage": None,
                "honesty_violations": []}

    recipe = er.extract_recipe(_draft(), name="hic", conda_deps=["samtools=1.21"],
                               primary_tools=["hic_assembler"], content_digest="sha256:deadbeef")
    res = er.rebuild_from_recipe(recipe, build_fn=fake_build)
    assert res["success"] and res["content_digest_match"] is True
    # the rebuild fed build_env_image the recipe's OWN install_steps + conda_deps (no draft)
    assert captured["spec"]["install_steps"] == recipe["install_steps"]
    assert captured["kw"]["conda_deps"] == ["samtools=1.21"]
    assert captured["kw"]["name"] == "hic"


def test_rebuild_flags_content_digest_mismatch():
    def fake_build(spec, **kw):
        return {"success": True, "content_digest": "sha256:DIFFERENT", "stage": None}

    recipe = er.extract_recipe(_draft(), name="hic", conda_deps=[], primary_tools=["x"],
                               content_digest="sha256:deadbeef")
    res = er.rebuild_from_recipe(recipe, build_fn=fake_build)
    assert res["success"] is True and res["content_digest_match"] is False
    assert res["rebuilt_content_digest"] == "sha256:DIFFERENT"


def test_rebuild_no_expected_digest_is_not_a_match():
    # a recipe with no recorded content_digest can't be a "match" (nothing to match against)
    def fake_build(spec, **kw):
        return {"success": True, "content_digest": "sha256:whatever"}

    recipe = er.extract_recipe(_draft(), name="x", conda_deps=[], primary_tools=["x"])
    res = er.rebuild_from_recipe(recipe, build_fn=fake_build)
    assert res["content_digest_match"] is False
