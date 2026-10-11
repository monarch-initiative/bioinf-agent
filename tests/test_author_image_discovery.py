"""The authors' own image is found wherever they publish it, and their recipe wherever
they keep it.

Bioinformatics tools publish images on ghcr.io, Docker Hub and quay.io; a probe that asks
one registry reports "no image" for the other two. The probe asks all three for the
repository's `owner/repo`, and first asks for whatever the README declares (`docker pull
nservant/hicpro`), so the agent never guesses image names. A found image carries its tag
list so a release can be pinned without guessing either. A repository whose Dockerfile
lives below the root (`docker/Dockerfile`) is not "a workflow with nothing to install":
one tree listing finds it. Every negative is scoped to what was asked.
"""
from __future__ import annotations

import json
import urllib.error

import pytest

from agent.skills import authors_sources as A


# ---------------------------------------------------------------------------
# a fake registry: the three token flows, manifests and tag lists
# ---------------------------------------------------------------------------

class _Registry:
    """`images`: {"ghcr"|"dockerhub"|"quay": {"owner/name": [tags]}}; `down`: registries
    whose token endpoint fails."""

    def __init__(self, images: dict, down: tuple[str, ...] = ()):
        self.images, self.down, self.asked = images, down, []

    def urlopen(self, req, timeout=None):
        url = req.full_url
        self.asked.append(url)
        host = url.split("/")[2]
        registry = {"ghcr.io": "ghcr", "auth.docker.io": "dockerhub", "registry-1.docker.io": "dockerhub",
                    "quay.io": "quay"}[host]
        if registry in self.down:
            raise urllib.error.URLError("connection refused")
        if "token" in url or "/v2/auth" in url:
            ref = url.split("repository:")[1].split(":pull")[0]
            if ref not in self.images.get(registry, {}):
                raise urllib.error.HTTPError(url, 401, "denied", {}, None)
            return _Resp(json.dumps({"token": "t"}))
        ref = url.split("/v2/", 1)[1]
        if "/tags/list" in ref:
            ref = ref.split("/tags/list")[0]
            return _Resp(json.dumps({"name": ref, "tags": self.images[registry][ref]}))
        ref, tag = ref.split("/manifests/")
        if tag not in self.images[registry][ref]:
            raise urllib.error.HTTPError(url, 404, "no such tag", {}, None)
        return _Resp("")


class _Resp:
    status = 200

    def __init__(self, body: str):
        self._b = body.encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def registry(monkeypatch):
    def _make(images, down=()):
        r = _Registry(images, down)
        monkeypatch.setattr(A.urllib.request, "urlopen", r.urlopen)
        return r
    return _make


# ---------------------------------------------------------------------------
# the registry probe
# ---------------------------------------------------------------------------

def test_an_image_on_docker_hub_is_found_by_the_owner_repo_convention(registry):
    registry({"dockerhub": {"gmod/apollo": ["2.6.3", "release-2.8.1", "release-2.9.0", "latest"]}})
    found = A._default_probe_author_image("GMOD", "Apollo")
    assert found["ref"] == "gmod/apollo" and found["source"] == "dockerhub" and found["tag"] == "latest"
    assert found["tags"][-1] == "latest" and "release-2.9.0" in found["tags"] and found["tags_total"] == 4
    assert "declared_as" not in found


def test_a_quay_image_without_a_latest_tag_is_found_through_its_tag_list(registry):
    registry({"quay": {"org/tool": ["1.2", "1.3"]}})
    found = A._default_probe_author_image("org", "tool")
    assert found["source"] == "quay" and found["ref"] == "quay.io/org/tool"
    assert found["tag"] == "" and found["tags"] == ["1.2", "1.3"]


def test_the_readme_declared_image_is_asked_before_the_convention(registry):
    r = registry({"dockerhub": {"nservant/hicpro": ["3.0.0", "latest"]}})
    found = A._default_probe_author_image("nservant", "HiC-Pro", declared=("nservant/hicpro:latest",))
    assert found["ref"] == "nservant/hicpro" and found["declared_as"] == "nservant/hicpro:latest"
    assert "repository:nservant/hicpro:pull" in r.asked[0]
    assert not any("nservant/hic-pro" in u for u in r.asked), "the convention is not asked once the declared image answers"


def test_a_clean_miss_on_every_registry_is_none_and_an_outage_is_an_error(registry):
    registry({})
    assert A._default_probe_author_image("someone", "thing") is None
    registry({}, down=("dockerhub",))
    out = A._default_probe_author_image("someone", "thing")
    assert out and "Docker Hub token endpoint" in out["error"] and "URLError" in out["error"]
    registry({"quay": {"someone/thing": ["1"]}}, down=("ghcr", "dockerhub"))
    assert A._default_probe_author_image("someone", "thing")["source"] == "quay", "a hit elsewhere beats an outage"


def test_the_single_registry_probe_still_answers_for_ghcr(registry):
    registry({"ghcr": {"astral-sh/uv": ["0.9.0", "latest"]}})
    assert A._default_probe_ghcr_image("astral-sh", "uv")["ref"] == "ghcr.io/astral-sh/uv"
    assert A._default_probe_ghcr_image("astral-sh", "nothing") is None


# ---------------------------------------------------------------------------
# what a README declares
# ---------------------------------------------------------------------------

_README = """
# HiC-Pro
[![Docker](https://img.shields.io/badge/Docker-manual-yellow.svg)](https://hub.docker.com/repository/docker/nservant/hicpro)
A docker image is available on Docker Hub:

    docker pull nservant/hicpro:latest
    docker run -it -v $PWD:/data continuumio/miniconda3 bash
    singularity pull docker://nservant/hicpro:3.0.0

See https://github.com/nservant/HiC-Pro/releases and docs/MANUAL.md. Base: quay.io/biocontainers/samtools:1.9
image: ghcr.io/nservant/hicpro-ci:dev
"""


def test_declared_images_keeps_the_authors_own_and_drops_base_images_and_paths():
    assert A.declared_images(_README, "nservant") == [
        "nservant/hicpro:latest", "nservant/hicpro:3.0.0", "ghcr.io/nservant/hicpro-ci:dev", "nservant/hicpro"]
    everything = A.declared_images(_README)
    assert "continuumio/miniconda3" in everything and "quay.io/biocontainers/samtools:1.9" in everything
    assert not any("github.com" in r or "docs/MANUAL.md" in r for r in everything)


def test_split_image_ref_reads_the_three_registries_and_rejects_others():
    assert A._split_image_ref("nservant/hicpro:latest") == ("dockerhub", "nservant/hicpro", "latest")
    assert A._split_image_ref("docker.io/gmod/apollo") == ("dockerhub", "gmod/apollo", "")
    assert A._split_image_ref("quay.io/biocontainers/samtools:1.9--h1") == ("quay", "biocontainers/samtools", "1.9--h1")
    assert A._split_image_ref("ghcr.io/org/tool@sha256:" + "a" * 64) == ("ghcr", "org/tool", "")
    assert A._split_image_ref("registry.example.org/org/tool:1") == ("", "", "")
    assert A._split_image_ref("localhost:5000/tool") == ("", "", "")


# ---------------------------------------------------------------------------
# discovery: README + tree, and the verdict built on them
# ---------------------------------------------------------------------------

_TALOS_TREE = {"truncated": False, "tree": [
    {"path": "nextflow.config", "type": "blob"}, {"path": "main.nf", "type": "blob"},
    {"path": "docker/Dockerfile", "type": "blob"}, {"path": "docker/CPG_Dockerfile", "type": "blob"},
    {"path": "docker/environment.yml", "type": "blob"}, {"path": "tests/Dockerfile", "type": "blob"},
    {"path": "docker", "type": "tree"}]}
_DF = "FROM python:3.11\nRUN apt-get update && apt-get install -y build-essential libhts-dev\nRUN pip install .\n"


def _talos_io(tree=_TALOS_TREE):
    files = {"nextflow.config": "manifest { name = 'talos' }", "main.nf": "workflow {}",
             "docker/Dockerfile": _DF, "docker/CPG_Dockerfile": "FROM private/base\n",
             "README.md": "# Talos\nRun with `docker pull ghcr.io/populationgenomics/talos:v12`"}
    asked: list[str] = []

    def get_text(url, **kw):
        asked.append(url)
        return files.get(url.rsplit("/HEAD/", 1)[-1])

    def get_json(url, **kw):
        asked.append(url)
        return tree if "/git/trees/HEAD?recursive=1" in url else None
    return get_text, get_json, asked


def test_a_dockerfile_below_the_root_is_found_by_one_tree_listing():
    get_text, get_json, asked = _talos_io()
    src = A.discover_authors_sources("populationgenomics", "talos", get_text=get_text, get_json=get_json,
                                     probe_image=lambda *a, **k: None)
    assert src["recipe_search"] == "tree"
    assert src["recipe_candidates"] == ["docker/Dockerfile", "docker/CPG_Dockerfile", "tests/Dockerfile"]
    assert [r["path"] for r in src["container_recipes"]] == ["docker/Dockerfile", "docker/CPG_Dockerfile"]
    assert src["container_recipes"][0]["text"] == _DF
    assert {"path": "docker/environment.yml"} in src["env_specs"]
    assert sum("/git/trees/" in u for u in asked) == 1


def test_the_tree_is_not_listed_when_the_root_has_a_recipe():
    files = {"Dockerfile": _DF}
    asked: list[str] = []

    def get_json(url, **kw):
        asked.append(url)
        return {"tree": []}
    src = A.discover_authors_sources("o", "r", get_text=lambda u, **k: files.get(u.rsplit("/HEAD/", 1)[-1]),
                                     get_json=get_json, probe_image=lambda *a, **k: None)
    assert src["recipe_search"] == "root" and [r["path"] for r in src["container_recipes"]] == ["Dockerfile"]
    assert not any("/git/trees/" in u for u in asked)


def test_a_tree_that_cannot_be_listed_is_said_not_claimed_empty():
    src = A.discover_authors_sources("o", "r", get_text=lambda u, **k: None, get_json=lambda u, **k: None,
                                     probe_image=lambda *a, **k: None)
    assert src["recipe_search"] == "root_only" and "could not be listed" in src["recipe_search_note"]
    out = A.assess_tool_sources("r", owner="o", repo="r", sources=src)
    assert out["recipe_search"] == "root_only"
    assert "root only; the tree could not be listed" in out["recommendation"]


def test_a_workflow_repo_with_a_dockerfile_below_the_root_is_not_workflow_only():
    get_text, get_json, _ = _talos_io()
    out = A.assess_tool_sources("talos", owner="populationgenomics", repo="talos",
                                get_text=get_text, get_json=get_json, probe_image=lambda *a, **k: None)
    assert out["workflow_manifest"]["engine"] == "nextflow"
    assert out["workflow_only"] is False
    assert out["authors_recipe"]["path"] == "docker/Dockerfile"
    assert out["authors_recipe"]["alternatives"] == ["docker/CPG_Dockerfile"]
    assert out["reconstruction_incomplete"] is True
    assert "build from the authors' docker/Dockerfile" in out["recommendation"]


def test_the_readme_images_reach_the_probe_and_the_verdict_says_where_the_image_was_named():
    get_text, get_json, _ = _talos_io()
    seen = {}

    def probe(owner, repo, declared=(), **kw):
        seen["declared"] = tuple(declared)
        return {"ref": "ghcr.io/populationgenomics/talos", "source": "ghcr", "tag": "v12",
                "tags": ["v11", "v12"], "tags_total": 2, "declared_as": declared[0]}
    out = A.assess_tool_sources("talos", owner="populationgenomics", repo="talos",
                                get_text=get_text, get_json=get_json, probe_image=probe)
    assert seen["declared"] == ("ghcr.io/populationgenomics/talos:v12",)
    assert out["declared_images"] == ["ghcr.io/populationgenomics/talos:v12"]
    rec = out["recommendation"]
    assert "named in README.md" in rec and "2 tags, among them v11, v12" in rec
    assert "adopt it by digest" in rec and "pin a release tag" in rec and "UNCHECKED" not in rec


def test_a_clean_miss_names_the_registries_the_repo_and_the_readme_that_were_asked():
    get_text, get_json, _ = _talos_io()
    out = A.assess_tool_sources("talos", owner="populationgenomics", repo="talos",
                                get_text=get_text, get_json=get_json, probe_image=lambda *a, **k: None)
    rec = out["recommendation"]
    assert "ghcr.io, Docker Hub and quay.io were asked for populationgenomics/talos" in rec
    assert "image references in README.md" in rec and "UNCHECKED" in rec


def test_the_resolver_renders_the_author_image_call_with_a_release_tag_slot_and_the_tags_seen():
    from agent.skills import resolver as R
    call = R._install_call("author_image", "apollo", "", {"ref": "gmod/apollo", "tags": ["2.6.3", "release-2.9.0", "latest"]}, "")
    assert call.startswith('freeze_from_image(image="gmod/apollo:<release tag>"')
    assert "tags seen: 2.6.3, release-2.9.0" in call and "latest" not in call.split("tags seen:")[1]
