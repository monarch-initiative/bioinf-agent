"""The licence-gated route: an artifact the OPERATOR supplies, not one we fetch.

Every other long-tail tier ships by replaying its download inside the container
build. That is structurally impossible for a tool whose vendor hands the bytes to
a human who accepted a licence — Cell Ranger, ANNOVAR, GeneMark, bcl-convert,
SignalP. Those bytes exist only on the operator's disk.

WHAT WENT WRONG BEFORE THIS EXISTED, measured 2026-08-04 on a real drive: the only
way in was a `file://` URL, and it worked — all the way through. `resolve_linux_asset`
found it, `sha256_of_url` hashed it, and freeze awarded
`assurance: authenticated / verified: True` (the TOP tier) to a file in a scratch
directory, having "verified" it by reading the same file twice against a hash the
caller computed itself. The rebuild recipe rendered a paste-able
`curl -fL -o x.tar 'file:///private/tmp/.../scratchpad/...'`. The only thing that
stopped it shipping was the Dockerfile dying on `curl file://...` inside a container
with no such path — a late accident, not a check.

So these tests pin three separate claims, because the old behaviour failed all three:
  1. the dishonest spelling is REFUSED, and the refusal names the honest route;
  2. self-hashing does not buy the `authenticated` tier;
  3. the artifact is CARRIED into the build (COPY), never re-fetched, and the recipe
     says so instead of rendering a command that cannot work anywhere else.
"""
from __future__ import annotations

import hashlib
import tarfile
from pathlib import Path

import pytest

from agent.skills import install_commands as ic
from agent.skills.container_build import emit_dockerfile, PixiEngine
from agent.skills.env_freeze import _map_install_spec
from agent.skills.env_recipe_render import render_step_commands


ARTIFACT = "faketool_linux_amd64.tar.gz"


def _artifact(tmp_path: Path, inner: str = "faketool") -> tuple[Path, str]:
    """A real .tar.gz containing one executable, plus its sha256."""
    binp = tmp_path / inner
    binp.write_bytes(b"#!/bin/sh\necho faketool 1.0\n")
    tgz = tmp_path / ARTIFACT
    with tarfile.open(tgz, "w:gz") as tf:
        tf.add(binp, arcname=inner)
    return tgz, hashlib.sha256(tgz.read_bytes()).hexdigest()


def _im(tgz: Path, sha: str, **over) -> dict:
    im = {
        "type": "binary",
        "binary_url": None,
        "sha256": "deadbeef",
        "asset_sha256": sha,
        "asset_authenticated": False,
        "local_path": "/opt/tools/faketool/faketool",
        "artifact_source": "operator_supplied",
        "artifact_name": tgz.name,
        "artifact_local_path": str(tgz),
    }
    im.update(over)
    return im


def _explode(*_a, **_k):  # pragma: no cover - must never be reached
    raise AssertionError("an operator-supplied artifact must not touch the network")


# ---------------------------------------------------------------------------
# 1. the dishonest spelling is refused
# ---------------------------------------------------------------------------

def test_a_file_url_is_refused_and_names_the_honest_route(tmp_path):
    """`file://` in `url` is the SAME REQUEST as local_path, spelled to look
    fetchable. Refused — and the message must name `local_path`, because a refusal
    that doesn't say what to do instead just relocates the dead end."""
    from agent.skills.env_manager import EnvManager
    em = EnvManager({"paths": {"conda_envs_prefix": str(tmp_path / "envs")}})
    (tmp_path / "envs" / "e").mkdir(parents=True)
    tgz, sha = _artifact(tmp_path)

    r = em.install_release_binary("e", "faketool", url=f"file://{tgz}", sha256=sha)
    assert r["success"] is False
    assert r["code"] == "env_manager.binary_url_is_local_file"
    assert "local_path" in r["error"]


def test_url_and_local_path_together_are_refused(tmp_path):
    """They record different provenance and ship by different mechanisms, so
    there is no sensible merge — and silently preferring one would make the
    record disagree with what the caller asked for."""
    from agent.skills.env_manager import EnvManager
    em = EnvManager({"paths": {"conda_envs_prefix": str(tmp_path / "envs")}})
    (tmp_path / "envs" / "e").mkdir(parents=True)
    tgz, _ = _artifact(tmp_path)

    r = em.install_release_binary("e", "faketool", url="https://x/y.tar.gz",
                                  local_path=str(tgz))
    assert r["success"] is False
    assert r["code"] == "env_manager.binary_source_ambiguous"


def test_neither_url_nor_local_path_is_refused(tmp_path):
    from agent.skills.env_manager import EnvManager
    em = EnvManager({"paths": {"conda_envs_prefix": str(tmp_path / "envs")}})
    (tmp_path / "envs" / "e").mkdir(parents=True)

    r = em.install_release_binary("e", "faketool")
    assert r["success"] is False
    assert r["code"] == "env_manager.binary_source_missing"


# ---------------------------------------------------------------------------
# 2. self-hashing does not buy the `authenticated` tier
# ---------------------------------------------------------------------------

def test_a_caller_hash_over_a_local_file_does_not_earn_authenticated(tmp_path):
    """THE LOAD-BEARING ONE. `asset_authenticated` used to be `bool(sha256)`, so
    hashing your own local file and handing back the hash earned the top assurance
    tier — the record agreeing with itself, which is the I5 laundering shape.

    A caller sha256 is meaningful only over a FETCH, where a hash known beforehand
    would have caught a swapped upload. Over a local file it crosses nothing.

    Mutation guard: restore `asset_authenticated = bool(sha256)` and this fails.
    """
    from agent.skills.env_manager import EnvManager
    em = EnvManager({"paths": {"conda_envs_prefix": str(tmp_path / "envs")}})
    (tmp_path / "envs" / "e").mkdir(parents=True)
    tgz, sha = _artifact(tmp_path)

    r = em.install_release_binary("e", "faketool", local_path=str(tgz),
                                  sha256=sha, binary_in_archive="faketool")
    assert r["success"] is True, r
    im = r["install_method"]
    assert im["asset_authenticated"] is False, \
        "a hash the caller computed over its own file is not authentication"
    assert im["artifact_source"] == "operator_supplied"
    assert im["binary_url"] is None, "there is no URL; absence must be STATED, not implied"
    assert im["asset_sha256"] == sha, "the hash is still recorded — it anchors WHAT the bytes are"


def test_a_wrong_hash_still_hard_fails_on_the_local_route(tmp_path):
    """Declining to call it `authenticated` must not weaken the check itself:
    the hash still says WHAT the bytes are, and a mismatch is still a hard fail."""
    from agent.skills.env_manager import EnvManager
    em = EnvManager({"paths": {"conda_envs_prefix": str(tmp_path / "envs")}})
    (tmp_path / "envs" / "e").mkdir(parents=True)
    tgz, _ = _artifact(tmp_path)

    r = em.install_release_binary("e", "faketool", local_path=str(tgz),
                                  sha256="00" * 32, binary_in_archive="faketool")
    assert r["success"] is False
    assert "sha256" in (r.get("error") or "").lower()


# ---------------------------------------------------------------------------
# 3. carried into the build, never re-fetched
# ---------------------------------------------------------------------------

def test_freeze_maps_an_operator_artifact_without_touching_the_network(tmp_path):
    """The network resolvers are injected as landmines. The old code path called
    BOTH on the way to awarding `authenticated`; this branch must not reach them."""
    tgz, sha = _artifact(tmp_path)
    out = _map_install_spec({"name": "faketool", "type": "binary",
                             "install_method": _im(tgz, sha)},
                            "linux/amd64",
                            resolve_linux_asset=_explode, sha256_of_url=_explode)
    spec = out["spec"]
    assert spec["stage_artifact"] == str(tgz), "the bytes must be carried, by path"
    assert "curl" not in spec["command"], "an operator artifact is never downloaded"
    assert ic.STAGED in spec["command"], "it is read from where staging put it"
    assert sha in spec["command"], "the COPY is still proved by hash, in-image"

    prov = spec["provenance"]
    assert prov["assurance"] == "human_supplied"
    assert prov["verified"] is False, "nothing independent vouches for these bytes"
    assert prov["asset_url"] is None


def test_freeze_refuses_when_the_operators_artifact_is_gone(tmp_path):
    """These bytes are not re-fetchable, so a missing artifact is a dead stop, and
    the refusal has to SAY that — otherwise the reader goes looking for a download
    that does not exist."""
    tgz, sha = _artifact(tmp_path)
    tgz.unlink()
    out = _map_install_spec({"name": "faketool", "type": "binary",
                             "install_method": _im(tgz, sha)},
                            "linux/amd64",
                            resolve_linux_asset=_explode, sha256_of_url=_explode)
    assert out.get("code") == "build.local_artifact_missing"
    assert "not re-fetchable" in out["error"]


def test_the_dockerfile_copies_staged_artifacts_into_the_builder():
    """Without the COPY the RUN has nothing to read. Pinned because the generator
    and the Dockerfile agree only by convention (`ic.STAGED`), and a silent
    disagreement fails deep inside a build."""
    df = emit_dockerfile("debian:bookworm-slim", engine=PixiEngine("linux/amd64"),
                         has_env_layer=False, longtail_steps=[],
                         staged_artifacts=[ARTIFACT])
    assert f"COPY {ARTIFACT} {ic.STAGED}/{ARTIFACT}" in df
    # ...and only in the BUILDER stage: the runtime stage COPYs /opt/tools, so a
    # gated tarball must not ride into the shipped image as a copy of itself.
    builder, _, runtime = df.partition("# ---- runtime image (shipped) ----")
    assert ARTIFACT in builder and ARTIFACT not in runtime


def test_no_copy_line_when_nothing_was_staged():
    df = emit_dockerfile("debian:bookworm-slim", engine=PixiEngine("linux/amd64"),
                         has_env_layer=False, longtail_steps=[])
    assert ic.STAGED not in df


# ---------------------------------------------------------------------------
# 4. the recipe states an instruction, never a command that cannot work
# ---------------------------------------------------------------------------

def test_the_rebuild_recipe_gives_an_instruction_not_a_paste_able_curl(tmp_path):
    """It used to render `curl -fL -o x.tar '<binary_url>'`, and for this route that
    URL was a path inside the agent's own scratch directory. This file's own doctrine
    — stated for an unpinned source checkout forty lines away — is that an
    instruction you can paste is worse than none, because it looks like one."""
    tgz, sha = _artifact(tmp_path)
    lines = render_step_commands({"tool": "faketool", "install_method": _im(tgz, sha)})
    text = "\n".join(lines)

    assert "curl" not in text, "there is no download for bytes we cannot distribute"
    assert str(tgz) not in text, "a path on the build machine helps nobody else"
    assert "local_path=" in text, "it must name the primitive that consumes the artifact"
    assert sha in text, "and the hash, so a rebuild can prove it got the SAME artifact"


def test_a_normal_url_binary_recipe_is_unchanged(tmp_path):
    """The honest-instruction branch must not swallow the fetchable case: a real
    release URL still renders the curl it always did."""
    lines = render_step_commands({"tool": "mosdepth", "install_method": {
        "type": "binary",
        "binary_url": "https://example.org/mosdepth.tar.gz",
        "asset_sha256": "ab" * 32}})
    text = "\n".join(lines)
    assert "curl -fL" in text and "https://example.org/mosdepth.tar.gz" in text


def test_the_rebuild_section_renders_installs_the_builder_would_replay(tmp_path):
    """A PRE-EXISTING drop, found by this feature and not caused by it.

    Producers write a non-conda install_method at
    `install_steps[].installed_packages[].install_method`; `_section_build` handed
    `render_step_commands` the RAW STEP, where no producer writes that key. So the
    reader saw `{}`, returned [], and the step vanished from its own rebuild
    instructions — under a heading that says "install the non-conda tools", with
    nothing to indicate anything was missing. Every release-binary / jar / source
    install was affected, not just this route.

    Fixed by rendering from `freeze.non_conda_installs` — the same accessor freeze
    uses to decide what to replay, which is what the function's docstring already
    claimed it read.
    """
    from agent.skills.env_recipe_render import render_recipe_markdown
    tgz, sha = _artifact(tmp_path)
    recipe = {
        "name": "demo", "build_method": "container-native", "conda_deps": [],
        "install_steps": [{
            "tool": "operator", "returncode": 0,
            "installed_packages": [
                {"name": "faketool", "channel": "binary",
                 "install_method": _im(tgz, sha)},
            ],
        }],
    }
    md = render_recipe_markdown(recipe, None)
    assert "install the non-conda tools" in md
    assert "THE OPERATOR SUPPLIED" in md, \
        "the one step only a human can perform must not be silently omitted"
    assert sha in md


def test_the_recipe_says_so_when_no_hash_was_recorded(tmp_path):
    """Absence stated, never rounded up: without an asset hash a rebuild cannot
    prove it obtained the same artifact, and the recipe must not imply it can."""
    tgz, sha = _artifact(tmp_path)
    lines = render_step_commands({"tool": "faketool",
                                  "install_method": _im(tgz, sha, asset_sha256="")})
    text = "\n".join(lines)
    assert "cannot prove" in text
