"""Freeze-grid regression tests for the BUILD-METHOD rows (adopt / authors-dockerfile).

Unlike the install tiers (which ride the container-native build via build_env_image),
the build-method rows ship an image WITHOUT a reconstruction: adopt a BioContainer by
manifest digest, adopt the author's OWN published image, or docker-build the author's
Dockerfile. The generator drives them through the SAME injectable executors the freeze()
MCP surface uses — freeze_from_image / build_from_authors_recipe — each of which runs the
honesty contract (check_build) internally.

These tests are HERMETIC: they monkeypatch the executors + the biocontainer resolver, so
they assert the WIRING (the right executor, the right args, the right normalization) and
the recipe SHAPE without a Docker daemon or the network. The real-bytes proof lives in the
committed docs/freeze_tier_coverage.json (measured by the opt-in generator) and the
ratchet in tests/test_freeze_tier_coverage.py.
"""
import importlib.util
from pathlib import Path

from agent.skills import env_honesty

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _ft():
    return _load("freeze_tiers")


def _gen():
    return _load("measure_freeze_tier_coverage")


# ── #A the recipe shape (pure) ─────────────────────────────────────────────────

def test_build_method_rows_are_wired_probes():
    """All three build-method rows are now REAL probes: a non-None builder (so the
    generator measures them) + a `build` recipe + a real probe tool."""
    ft = _ft()
    for name in ("adopt-biocontainer", "adopt-image", "authors-dockerfile"):
        row = ft.tier(name)
        assert row["builder"] is not None, f"{name} still declared-only"
        assert row["probe_tool"], f"{name} has no probe tool"
        assert isinstance(row.get("build"), dict), f"{name} has no build recipe"


def test_adopt_biocontainer_recipe_names_a_real_bioconda_tool():
    """The biocontainer row resolves a curated quay.io/biocontainers image for a pinned
    (tool, version) and RUNS the tool in it — evidence is shape-clean + references it."""
    b = _ft().tier("adopt-biocontainer")["build"]
    a = b["adopt"]
    assert a["kind"] == "biocontainer" and a["tool"] == "samtools" and a["version"] == "1.21"
    assert env_honesty.evidence_shape_violation(a["evidence"], "samtools") is None
    assert env_honesty._references_tool(a["evidence"], "samtools") is True
    assert b["primary_tools"] == ["samtools"]


def test_adopt_image_pins_an_immutable_versioned_author_image():
    """The author-image row adopts a tool's OWN image pinned to an explicit VERSION tag
    (never a bare :latest / moving tag — reproducibility) and RUNS the tool in it."""
    a = _ft().tier("adopt-image")["build"]["adopt"]
    assert a["kind"] == "author_image"
    img = a["image"]
    assert img.startswith("ghcr.io/astral-sh/uv:") and img.split(":")[-1] not in ("latest", "")
    assert any(c.isdigit() for c in img.split(":")[-1]), "author image tag is not version-pinned"
    assert env_honesty.evidence_shape_violation(a["evidence"], "uv") is None
    assert env_honesty._references_tool(a["evidence"], "uv") is True


def test_authors_dockerfile_pins_repo_and_ref():
    """The authors-Dockerfile row pins the repo AND a ref (a bare default branch drifts),
    names the Dockerfile, and RUNS the built tool — evidence shape-clean."""
    ad = _ft().tier("authors-dockerfile")["build"]["authors_dockerfile"]
    assert ad["repo"] == "bbuchfink/diamond"
    assert ad["ref"] and ad["ref"] != "master", "ref must be a pinned tag, not a branch"
    assert ad["recipe"] == "Dockerfile"
    assert env_honesty.evidence_shape_violation(ad["evidence"], "diamond") is None
    assert env_honesty._references_tool(ad["evidence"], "diamond") is True


# ── #B the dispatch / normalization WIRING (hermetic via monkeypatch) ──────────

def test_adopt_image_drives_freeze_from_image(monkeypatch):
    """adopt-image routes through freeze_from_image with the pinned image, the tool's
    evidence, and build_method='adopt-image' — and normalizes a proven result to ok."""
    gen = _gen()
    captured = {}

    def fake_ffi(**kw):
        captured.update(kw)
        return {"success": True, "image_digest": "sha256:aa", "content_digest": "sha256:bb"}

    monkeypatch.setattr("agent.skills.freeze_from_image.freeze_from_image", fake_ffi)
    out = gen.build_method_tier(_ft().tier("adopt-image"))
    assert out["ok"] is True and out["image_digest"] == "sha256:aa"
    assert captured["image"] == "ghcr.io/astral-sh/uv:0.11.30-debian-slim"
    assert captured["build_method"] == "adopt-image"
    assert captured["tools"] == [{"name": "uv", "evidence": "uv --version"}]


def test_adopt_biocontainer_resolves_the_digest_before_adopting(monkeypatch):
    """The biocontainer row RESOLVES the image_by_digest first, then adopts THAT digest
    reference — never the bare tool name — so the probe pulls by manifest digest."""
    gen = _gen()
    monkeypatch.setattr(
        "agent.skills.biocontainers.resolve_biocontainer",
        lambda pkgs: {"found": True,
                      "image_by_digest": "quay.io/biocontainers/samtools@sha256:dead"})
    captured = {}

    def fake_ffi(**kw):
        captured.update(kw)
        return {"success": True, "image_digest": "sha256:aa", "content_digest": "sha256:bb"}

    monkeypatch.setattr("agent.skills.freeze_from_image.freeze_from_image", fake_ffi)
    out = gen.build_method_tier(_ft().tier("adopt-biocontainer"))
    assert out["ok"] is True
    assert captured["image"] == "quay.io/biocontainers/samtools@sha256:dead"
    assert captured["tools"] == [{"name": "samtools", "evidence": "samtools --version"}]


def test_adopt_biocontainer_miss_is_a_clean_failure(monkeypatch):
    """No pre-built biocontainer → a recorded failure with a self-explaining reason, and
    freeze_from_image is NEVER called (nothing to adopt)."""
    gen = _gen()
    monkeypatch.setattr(
        "agent.skills.biocontainers.resolve_biocontainer",
        lambda pkgs: {"found": False, "reason": "no pre-built biocontainer for this version"})

    def boom(**kw):
        raise AssertionError("freeze_from_image must not be called on a biocontainer miss")

    monkeypatch.setattr("agent.skills.freeze_from_image.freeze_from_image", boom)
    out = gen.build_method_tier(_ft().tier("adopt-biocontainer"))
    assert out["ok"] is False and "no biocontainer" in out["error"]


def test_authors_dockerfile_drives_build_from_authors_recipe(monkeypatch):
    """authors-dockerfile routes through build_from_authors_recipe with the pinned repo +
    ref + Dockerfile + evidence — the executor that clones, docker-builds, then adopts."""
    gen = _gen()
    captured = {}

    def fake_bar(**kw):
        captured.update(kw)
        return {"success": True, "image_digest": "sha256:aa", "content_digest": "sha256:bb"}

    monkeypatch.setattr("agent.skills.freeze_from_image.build_from_authors_recipe", fake_bar)
    out = gen.build_method_tier(_ft().tier("authors-dockerfile"))
    assert out["ok"] is True
    assert captured["repo"] == "bbuchfink/diamond" and captured["ref"] == "v2.2.4"
    assert captured["recipe"] == "Dockerfile"
    assert captured["tools"] == [{"name": "diamond", "evidence": "diamond version"}]


def test_build_method_outcome_maps_a_refusal_to_not_ok():
    """A freeze_from_image REFUSAL (honesty violation) is a real failure — ok=False with
    the violations surfaced, no validation locus claimed."""
    gen = _gen()
    out = gen._build_method_outcome(
        {"success": False, "error": "", "honesty_violations": [{"id": "VALIDATED_IN_IMAGE"}]})
    assert out["ok"] is False and "honesty violations" in out["error"]
    assert out["validation_locus"] is None


def test_build_method_outcome_locus_tracks_the_host(monkeypatch):
    """The in-image evidence runs under --platform linux/amd64; the recorded locus is a
    faithful derivation from the host arch (emulated off-amd64, native on it)."""
    gen = _gen()
    monkeypatch.setattr(gen, "_host_arch", lambda: "arm64")
    assert gen._build_method_outcome({"success": True})["validation_locus"] == "emulated"
    monkeypatch.setattr(gen, "_host_arch", lambda: "x86_64")
    assert gen._build_method_outcome({"success": True})["validation_locus"] == "native"
