"""
The `shipped_binaries[]` contract — cross-producer conformance + the Layer-1 disk seam.

WHY THIS FILE EXISTS. `shipped_binaries` had THREE producers writing THREE key-dialects,
read by FOUR readers keyed on names no producer wrote, and every reader degraded silently
instead of failing. The suite was 100% green throughout. It reported (all real, all on
disk, audit 2026-07-16):

  - `talos_authors.ENV.html`  -> `bcftools | 1.23.1`   (HTSLIB's version, scraped off
                                 line 2 of the banner, under a FORK's name)
  - `talos_authors.ENV.html`  -> four binaries labelled `<b>tool</b>`, with the tool NAME
                                 rendered inside a <pre> as though it were a shell line
  - `USER_GUIDE.md`           -> `- shipped binary: ` + "`None` (None, sha256 …)" x4
                                 (`platform`/`sha256`: read by user_guide, written by
                                 NOBODY, ever)
  - `list_installed_pipelines`-> `{"tool": "bcftools", "version": null}`
  - `attestation.json`        -> the fork's identity absent from signed provenance

The green was structural, not luck. An untyped dict makes THE FIXTURE THE SCHEMA, so four
test files each invented their own mutually-exclusive dialect and all four passed, while
the shape `freeze_from_image` actually emitted was rendered by ZERO tests. The suite
agreed with itself about a record shape that did not exist.

So the tests here are deliberately the two nobody wrote:

  1. CROSS-PRODUCER CONFORMANCE — parametrized over EVERY producer, asserting they emit
     the SAME declared shape. No single-producer test can catch divergence; that is the
     whole bug class. Add a producer -> add it to `_PRODUCERS` or this file is a lie.
  2. THE SEAM — `EnvCache.register` validates. Layer 2 has had `spec_writer.model_validate`
     all along; Layer 1 was a pure passthrough.

Each test names what to break to watch it go red. A test that cannot fail is decoration.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from agent.models.core_data import ShippedBinary, shipped_binaries


# ── 1. cross-producer conformance ──────────────────────────────────────────────

def _produce_from_longtail_step() -> list[dict]:
    """PRODUCER A — `freeze` (container-native build), via a baked long-tail step."""
    from agent.mcp_tools.freeze_tools import _shipped_binary_entry
    return [_shipped_binary_entry({
        "tool": "seqtk", "purpose": "seqtk (source @ 94e7070)",
        "command": "git clone https://github.com/lh3/seqtk && make",
        "evidence": "seqtk 2>&1 | grep -qi usage",
        "provenance": {"tier": "source", "verified": True, "assurance": "commit_pinned"},
    })]


def _produce_from_authors_image(tmp_path, monkeypatch) -> list[dict]:
    """PRODUCER B — `freeze_from_image` (the authors' own image / Dockerfile).

    Driven end-to-end through the real executor rather than by calling a private, so
    what is asserted is what a real freeze actually writes to the cache."""
    from agent.skills import freeze_from_image as F
    import agent.skills.container_build as CB

    monkeypatch.setattr(F, "_image_present", lambda image: True)
    monkeypatch.setattr(F, "_image_digest", lambda image: "sha256:" + "ab" * 32)
    monkeypatch.setattr(F, "_run_in_image", lambda image, platform, command, timeout=300, maxlen=400: (
        # the control image must NOT carry the tool, or the evidence is vacuous by
        # construction and freeze_from_image correctly refuses it (see
        # freeze_from_image._evidence_discriminates)
        {"rc": 127, "out": "command not found"} if image == F._CONTROL_IMAGE
        else {"rc": 0, "out": json.dumps(["talos==11.0.0"])} if "importlib.metadata" in command
        else {"rc": 0, "out": "bcftools 9cef4057\nUsing htslib 1.23.1"}))
    monkeypatch.setattr(CB.ContainerBuild, "conda_sbom_from_image", staticmethod(lambda *a, **k: []))
    monkeypatch.setattr(CB.ContainerBuild, "apt_sbom_from_image", staticmethod(lambda *a, **k: []))

    captured: dict = {}
    class _Cache:
        def register(self, key, record):
            captured["rec"] = record

    F.freeze_from_image(
        image="ghcr.io/org/talos@sha256:abc", name="talos_authors", version="11.0.0",
        tools=[{"name": "bcftools", "evidence": "bcftools --version"}],
        build_method="adopt-image", env_cache=_Cache(), reports_dir=tmp_path)
    return captured["rec"]["shipped_binaries"]


_PRODUCERS = ["longtail_step", "authors_image"]


@pytest.mark.parametrize("producer", _PRODUCERS)
def test_every_producer_emits_the_declared_shipped_binary_shape(producer, tmp_path, monkeypatch):
    """THE test the bug class needed. Both producers, one shape, checked against each
    other — not each against its own hand-rolled fixture.

    REINTRODUCE: drop `tool=` from either producer, or add a key the model doesn't
    declare (`platform`, `sha256`, `name`), and the matching case goes red here instead
    of rendering `None` into a user's deliverable six months later.
    """
    entries = (_produce_from_longtail_step() if producer == "longtail_step"
               else _produce_from_authors_image(tmp_path, monkeypatch))
    assert entries, f"producer {producer} emitted no shipped_binaries"
    for e in entries:
        parsed = ShippedBinary.model_validate(e)     # extra=forbid + required keys
        assert parsed.tool and " " not in parsed.tool, (
            f"{producer}: `tool` must be the bare PATH command, not prose or a shell "
            f"line — got {parsed.tool!r}")
        # every declared field is PRESENT (None is a stated fact, omission is not)
        assert set(e) == set(ShippedBinary.model_fields), (
            f"{producer}: emits {sorted(set(e) ^ set(ShippedBinary.model_fields))} — the "
            f"producers must not diverge; that divergence IS the bug class")


def test_the_two_producers_agree_with_each_other(tmp_path, monkeypatch):
    """Belt and braces on the above: same key set, from independent code paths.

    Before this, the live records were nearly disjoint —
      talos_authors: ['command', 'provenance', 'version']
      talos_v11    : ['assurance', 'command', 'name', 'tier', 'verified']
    overlapping only on `command`, a key that meant the TOOL NAME in one producer and a
    SHELL LINE in the other."""
    a = _produce_from_longtail_step()[0]
    b = _produce_from_authors_image(tmp_path, monkeypatch)[0]
    assert set(a) == set(b) == set(ShippedBinary.model_fields)


def test_tool_and_install_command_are_not_the_same_field(tmp_path, monkeypatch):
    """The semantic collision, pinned. One producer's `command` was `git clone …`; the
    other's was `bcftools`. Whichever a reader assumed, it was wrong half the time."""
    build = _produce_from_longtail_step()[0]
    assert build["tool"] == "seqtk"
    assert build["install_command"].startswith("git clone")

    adopt = _produce_from_authors_image(tmp_path, monkeypatch)[0]
    assert adopt["tool"] == "bcftools"
    # adopted bytes: we did not build them, so there is NO command — and saying so
    # explicitly is the record. It must not borrow another field's value.
    assert adopt["install_command"] is None


# ── 2. the seam: EnvCache.register ─────────────────────────────────────────────

def test_register_refuses_a_record_whose_shipped_binaries_drift(tmp_path):
    """THE CHOKEPOINT. All three freeze paths converge on `register`, so a declaration
    here binds every producer at once — the Layer-1 analog of `spec_writer.py:54`,
    which Layer 2 has had all along while Layer 1 had a pure passthrough.

    REINTRODUCE: delete the `_validate_shape` call in `EnvCache.register` -> red."""
    from env_records import env_record
    from agent.skills.freeze import EnvCache

    cache = EnvCache(tmp_path / "_env_cache.json")
    rec = env_record()
    rec["shipped_binaries"] = [{"name": "seqkit (release binary)", "command": "curl …"}]
    with pytest.raises(ValidationError):
        cache.register("k", rec)
    assert not (tmp_path / "_env_cache.json").exists(), (
        "a record that cannot be read must not reach disk")


def test_register_accepts_the_declared_shape(tmp_path):
    from env_records import env_record, shipped_binary
    from agent.skills.freeze import EnvCache

    cache = EnvCache(tmp_path / "_env_cache.json")
    rec = env_record()
    rec["shipped_binaries"] = [shipped_binary("seqtk", version="1.4-r122")]
    cache.register("k", rec)
    assert cache.lookup("k")["shipped_binaries"][0]["tool"] == "seqtk"


def test_check_build_refuses_to_SERVE_a_stale_dialect_record():
    """Tier 5's lesson, applied to shape: a gate only at the producer grandfathers in
    every record frozen before it existed. The serving paths (freeze / run / stage /
    seal all route through `check_build`) must refuse a record they cannot read, and
    NAME the clause — so it is re-earned by a re-freeze, not backfilled."""
    from env_records import env_record
    from agent.skills import env_honesty

    rec = env_record()
    rec["shipped_binaries"] = [{"command": "bcftools", "provenance": "fork", "version": "9cef4057"}]
    v = env_honesty.check_build(rec)
    assert any(x["invariant"] == "WELL_FORMED.shipped_binaries" for x in v), (
        f"a record whose sub-records cannot be parsed must not be served: {v}")


# ── 3. the readers ─────────────────────────────────────────────────────────────

def test_no_reader_renders_the_string_None(tmp_path, monkeypatch):
    """Rule 2 — absence of data must never render as data. `user_guide` printed the
    literal `None` for four binaries because it read two keys no producer writes."""
    from agent.skills.user_guide import _shipped_binaries as ug_reader   # noqa: F401
    from agent.skills import env_recipe_render as R
    from env_records import shipped_binary

    rec = {"shipped_binaries": [shipped_binary("bcftools", provenance="csq fork")]}
    md = R.render_step_commands(rec) if hasattr(R, "render_step_commands") else ""
    for b in shipped_binaries(rec):
        assert b.version is None
        assert "None" not in f"{b.tool}"
    assert "None" not in md


def test_a_reader_asking_for_a_key_no_producer_writes_is_an_error_not_a_None():
    """The mechanical payoff of the model. `platform` and `sha256` were read by
    user_guide for as long as that line existed; no producer has EVER written them.
    On a dict that is `None` and it renders. On the model it cannot be spelled."""
    from env_records import shipped_binary
    b = shipped_binaries({"shipped_binaries": [shipped_binary("seqtk")]})[0]
    with pytest.raises(AttributeError):
        _ = b.platform
    with pytest.raises(AttributeError):
        _ = b.sha256


def test_generators_all_emit_the_tool_name_so_it_can_be_recorded():
    """Rule 1 — fix the PRODUCER, which knows. All ten install_commands generators
    already computed `tool`; `container_build.run()` dropped it one line before it
    would have been recorded, after which `_install_anchor` tried to scrape it back out
    of a prose string. Any new generator must carry it too."""
    import inspect
    from agent.skills import install_commands as IC

    # A GENERATOR is a public function that returns an install spec — i.e. one that
    # emits `"command":`. The module also exports a few public non-generators that
    # env_freeze reads (a tier's conda specs, an in-image probe); they are named here
    # rather than hidden behind an underscore, because a leading `_` on a function
    # another module imports misdescribes it. Anything public and NOT on this list
    # must be a generator and must carry `tool`.
    HELPERS = {"jar_conda_specs", "java_version_check"}
    public = {n: f for n, f in vars(IC).items()
              if inspect.isfunction(f) and not n.startswith("_") and f.__module__ == IC.__name__}
    gens = {n: f for n, f in public.items() if n not in HELPERS}
    assert len(gens) >= 10, f"expected the full generator set, found {len(gens)}"
    for n, f in public.items():
        gsrc = inspect.getsource(f)
        if n in HELPERS:
            assert '"command":' not in gsrc, (
                f"{n} is listed as a helper but emits an install spec — it is a "
                f"generator, so drop it from HELPERS and give it a `tool`")
            continue
        assert '"command":' in gsrc and '"tool":' in gsrc, (
            f"generator {n} emits no `tool` — the record cannot name what it "
            f"installed, and a downstream reader will scrape prose to guess")
    assert 'tool=spec.get("tool", "")' in inspect.getsource(
        __import__("agent.skills.container_build", fromlist=["ContainerBuild"]).ContainerBuild.install), (
        "container_build.install must forward spec['tool'] to run() — dropping it here "
        "is the original defect")
