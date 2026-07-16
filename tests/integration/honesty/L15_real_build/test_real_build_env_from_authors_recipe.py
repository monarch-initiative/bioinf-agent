"""L15 — REAL validated==shipped for the AUTHORS'-DOCKERFILE path.

The sibling `test_real_freeze_from_image` certifies freeze_from_image on real bytes when
handed an already-built image. This certifies the DECLARATIVE executor one rung up:
`build_from_authors_recipe` — clone the tool's OWN repo at a pinned ref, `docker build`
its Dockerfile, then freeze the built image. That whole chain (git clone → docker buildx
→ freeze_from_image → honesty contract → four deliverables) is the path a
`resolve_tool` `authors_recipe` verdict routes to, and until now it had only a
crash-safety guard test — never been exercised end-to-end on real bytes.

Self-contained + network-free: the "authors' repo" is a throwaway git repo built in a
tmp dir and cloned over `file://` (that's why the executor accepts a file:// URL — a
local mirror is a legitimate source, and it makes this test hermetic). The image is a
tiny non-conda debian-slim, so it also exercises the apt-SBOM path. Opt-in via
`-m integration_docker`; skipped when no Docker daemon / buildx is present.
"""
from __future__ import annotations

import shutil
import subprocess

import pytest

pytestmark = pytest.mark.integration_docker

_DOCKERFILE = (
    "FROM debian:bookworm-slim\n"
    "RUN printf '#!/bin/sh\\necho \"mytool 1.0 ok\"\\n' > /usr/local/bin/mytool "
    "&& chmod +x /usr/local/bin/mytool\n"
)


def _docker_up() -> bool:
    if not shutil.which("docker") or not shutil.which("git"):
        return False
    try:
        if subprocess.run(["docker", "info"], capture_output=True, timeout=15).returncode != 0:
            return False
        # the executor uses `docker buildx build --load`; skip if buildx is absent
        return subprocess.run(["docker", "buildx", "version"], capture_output=True, timeout=15).returncode == 0
    except Exception:
        return False


class _Cache:
    def __init__(self):
        self.registered = {}

    def register(self, key, record):
        self.registered[key] = record


def _make_authors_repo(root, dockerfile: str = _DOCKERFILE, tag: str = "v1"):
    """Build a throwaway git repo with a Dockerfile at a pinned tag; return its file:// URL."""
    repo = root / "authors_repo"
    repo.mkdir()
    (repo / "Dockerfile").write_text(dockerfile)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    for argv in (["git", "init", "-q"], ["git", "add", "Dockerfile"],
                 ["git", "commit", "-q", "-m", "init"], ["git", "tag", tag]):
        r = subprocess.run(argv, cwd=repo, capture_output=True, text=True, timeout=60,
                           env={**__import__("os").environ, **env})
        assert r.returncode == 0, r.stderr
    return f"file://{repo}", repo


def test_real_build_from_authors_recipe_clones_builds_and_freezes(tmp_path):
    """The full authors'-Dockerfile chain on real bytes: a pinned clone + docker build +
    a freeze that satisfies the honesty contract and records the pinned source."""
    if not _docker_up():
        pytest.skip("no Docker daemon / buildx / git — L15 real-build tier is opt-in")
    from agent.skills import freeze_from_image as F
    from agent.skills.env_honesty import check_build

    url, _ = _make_authors_repo(tmp_path, tag="v1")
    reports = tmp_path / "reports"
    cache = _Cache()
    out = F.build_from_authors_recipe(
        repo=url, ref="v1", name="l15_bar", version="1.0",
        tools=[{"name": "mytool", "evidence": "mytool"}],   # evidence that RUNS the tool
        env_cache=cache, reports_dir=reports)
    if out["outcome"] == "broke" and "build" in out.get("code", ""):
        pytest.skip(f"docker build unavailable in this env: {out.get('error', '')[:200]}")

    # proven, registered by digest, and stamped as the authors-dockerfile path
    assert out["outcome"] == "proven", out
    assert out["build_method"] == "authors-dockerfile"
    assert out["image_digest"].startswith("sha256:"), out
    assert cache.registered, "a passing build must register an env"
    rec = list(cache.registered.values())[0]

    # the honesty contract returns ZERO violations on the REAL record
    assert check_build(rec) == [], check_build(rec)
    vs = rec["verifications"]
    assert vs and all(v["passed"] for v in vs) and any(v["tool"] == "mytool" for v in vs), vs

    # the pinned source is recorded from the REAL clone — a resolved 40-char commit, the tag,
    # and the Dockerfile verbatim (so the build is reproducible from the record alone)
    src = rec["dockerfile_source"]
    assert src["tag"] == "v1"
    assert len(src["commit"]) == 40, src
    assert "debian:bookworm-slim" in src["dockerfile"]

    # all four deliverables render from the verified record; the human recipe records the path
    for f in ("l15_bar.ENV.html", "l15_bar.attestation.json",
              "l15_bar.recipe.yaml", "l15_bar.recipe.md"):
        assert (reports / f).is_file(), f"missing deliverable {f}"
    md = (reports / "l15_bar.recipe.md").read_text()
    assert "git checkout v1" in md


def test_real_build_from_authors_recipe_refuses_non_running_evidence(tmp_path):
    """Same real clone+build, but evidence that references a tool NOT in the image must be
    REFUSED by the honesty contract — the guard a shallow reconstruction slips past."""
    if not _docker_up():
        pytest.skip("no Docker daemon / buildx / git — L15 real-build tier is opt-in")
    from agent.skills import freeze_from_image as F

    url, _ = _make_authors_repo(tmp_path, tag="v1")
    cache = _Cache()
    out = F.build_from_authors_recipe(
        repo=url, ref="v1", name="l15_bar_bad", version="1.0",
        tools=[{"name": "notinstalled", "evidence": "notinstalled --version"}],
        env_cache=cache, reports_dir=tmp_path / "reports")
    if out["outcome"] == "broke" and "build" in out.get("code", ""):
        pytest.skip(f"docker build unavailable in this env: {out.get('error', '')[:200]}")
    assert out["outcome"] == "refused", out
    assert out["code"] == "freeze_from_image.honesty_violation"
    assert not cache.registered, "a tool that doesn't run in-image must NOT register"


def test_build_from_authors_recipe_bad_repo_is_refused(tmp_path):
    """A clone that can't resolve fails cleanly as broke (no crash) — the hostile-input path,
    testable without Docker: a file:// URL to a directory that isn't a git repo."""
    from agent.skills import freeze_from_image as F
    out = F.build_from_authors_recipe(
        repo=f"file://{tmp_path}/not_a_repo", ref="", name="x",
        tools=[{"name": "t", "evidence": "t"}],
        env_cache=_Cache(), reports_dir=tmp_path / "reports")
    assert out["outcome"] == "broke" and out["code"] == "authors_recipe.clone_failed", out
