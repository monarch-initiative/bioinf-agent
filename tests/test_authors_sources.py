"""
Tests for the authors'-own-resources reliability gate.

Two layers:
  • authors_sources.analyze_recipe_completeness — PURE; grounded against the REAL Talos
    Dockerfile (must flag incomplete) + trivial cases (must stay registry-routable).
  • resolver.resolve integration — the gate fires the authors' path ONLY when the
    reconstruction is incomplete; conda still wins for cleanly-packaged tools.

The whole point (the reliability gate, NOT a fixed ladder): follow the authors' machinery
when a conda/pip reconstruction would silently drop deps — but let conda win when it's
demonstrably complete. See [[feedback-prioritize-authors-own-env-recipe]].
"""

from __future__ import annotations

from agent.skills import authors_sources as A
from agent.skills import resolver as R


# --- a realistic "incomplete reconstruction" Dockerfile (Talos-shaped) ---
_INCOMPLETE_DF = """FROM python:3.11-slim AS base
RUN apt-get update && apt-get install -y --no-install-recommends \\
        gcc make libbz2-dev zlib1g-dev
RUN wget https://github.com/samtools/htslib/releases/download/1.21/htslib-1.21.tar.bz2 && \\
    tar -xf htslib-1.21.tar.bz2 && cd htslib-1.21 && ./configure && make && make install && \\
    git clone https://github.com/populationgenomics/bcftools.git && cd bcftools && make
RUN wget -O /bin/echtvar https://github.com/brentp/echtvar/releases/download/v0.2.2/echtvar
RUN pip install .
"""

# --- a trivial "reconstruction-safe" Dockerfile ---
_SAFE_DF = """FROM python:3.11-slim
RUN apt-get update && apt-get install -y ca-certificates
RUN pip install --no-cache-dir mytool==1.2.3
ENTRYPOINT ["mytool"]
"""


# ---------------------------------------------------------------------------
# PURE completeness analysis
# ---------------------------------------------------------------------------
def test_incomplete_recipe_is_flagged_with_signals():
    r = A.analyze_recipe_completeness(_INCOMPLETE_DF, self_repo="populationgenomics/talos")
    assert r["reconstruction_safe"] is False
    kinds = {s["kind"] for s in r["signals"]}
    assert "system_packages" in kinds
    assert "compiled_from_source" in kinds
    assert "vendored_dependency" in kinds        # clones bcftools (≠ talos)
    assert "fetched_binary" in kinds             # echtvar / htslib assets


def test_trivial_pip_recipe_is_reconstruction_safe():
    r = A.analyze_recipe_completeness(_SAFE_DF, self_repo="me/mytool")
    assert r["reconstruction_safe"] is True
    assert r["signals"] == []


def test_self_clone_is_not_a_vendored_signal():
    df = "FROM python:3.11-slim\nRUN git clone https://github.com/me/mytool.git && pip install ./mytool\n"
    r = A.analyze_recipe_completeness(df, self_repo="me/mytool")
    assert all(s["kind"] != "vendored_dependency" for s in r["signals"])


def test_assess_combines_into_gate_verdict():
    sources = {"container_recipes": [{"path": "Dockerfile", "text": _INCOMPLETE_DF}],
               "env_specs": [], "build_scripts": [], "author_image": None}
    v = A.assess_tool_sources("talos", owner="populationgenomics", repo="talos", sources=sources)
    assert v["reconstruction_incomplete"] is True
    assert "don't reconstruct" in v["recommendation"] or "authors'" in v["recommendation"]


def test_assess_author_image_wins():
    sources = {"container_recipes": [], "env_specs": [], "build_scripts": [],
               "author_image": {"ref": "ghcr.io/org/tool", "source": "ghcr"}}
    v = A.assess_tool_sources("tool", owner="org", repo="tool", sources=sources)
    assert v["author_image"]["ref"] == "ghcr.io/org/tool"
    assert "adopt it" in v["recommendation"]


# ---------------------------------------------------------------------------
# resolver integration — the gate changes routing correctly
# ---------------------------------------------------------------------------
def _stub_registries(monkeypatch, *, conda=False, pip=False, pip_repo=""):
    monkeypatch.setattr(R, "probe_conda", lambda n, t=12: {"available": conda, "latest": "1.0", "channel": "bioconda"})
    urls = {"Source": f"https://github.com/{pip_repo}"} if pip_repo else {}
    monkeypatch.setattr(R, "probe_pypi", lambda n, t=12: {"available": pip, "latest": "1.0",
                                                          "home_page": "", "project_urls": urls, "package_url": ""})
    monkeypatch.setattr(R, "probe_cran", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_bioconductor", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_spack", lambda n, t=12: {"available": False})


def test_resolve_prefers_authors_recipe_when_reconstruction_incomplete(monkeypatch):
    # tool hits PyPI (would normally be 'pip'), but its repo Dockerfile is incomplete →
    # the gate fires and authors_recipe outranks pip.
    _stub_registries(monkeypatch, pip=True, pip_repo="populationgenomics/talos")
    monkeypatch.setattr(R, "probe_authors_sources",
                        lambda tool, owner="", repo="", timeout=12: A.assess_tool_sources(
                            tool, owner=owner, repo=repo,
                            sources={"container_recipes": [{"path": "Dockerfile", "text": _INCOMPLETE_DF}],
                                     "env_specs": [], "build_scripts": [], "author_image": None}))
    d = R.resolve("talos")
    assert d["chosen"] == "authors_recipe", d["chosen"]
    # the rendered guidance must match the REAL executor signature (an agent copies it):
    # required name=, tools as [{name, evidence}] dicts — NOT a phantom `env` positional
    # or bare-string tools that the executor would reject.
    call = d["install_call"]
    assert call.startswith("build_env_from_authors_recipe(")
    assert "name=" in call and 'tools=[{"name":' in call and '"evidence":' in call
    assert "(env," not in call


def test_resolve_keeps_conda_when_reconstruction_is_safe(monkeypatch):
    # tool on conda AND pip, repo Dockerfile is trivial → gate stays shut, conda wins.
    _stub_registries(monkeypatch, conda=True, pip=True, pip_repo="me/mytool")
    monkeypatch.setattr(R, "probe_authors_sources",
                        lambda tool, owner="", repo="", timeout=12: A.assess_tool_sources(
                            tool, owner=owner, repo=repo,
                            sources={"container_recipes": [{"path": "Dockerfile", "text": _SAFE_DF}],
                                     "env_specs": [], "build_scripts": [], "author_image": None}))
    d = R.resolve("mytool")
    assert d["chosen"] == "conda", d["chosen"]


def test_resolve_adopts_author_image_over_everything(monkeypatch):
    _stub_registries(monkeypatch, conda=True, pip=True, pip_repo="org/tool")
    monkeypatch.setattr(R, "probe_authors_sources",
                        lambda tool, owner="", repo="", timeout=12: A.assess_tool_sources(
                            tool, owner=owner, repo=repo,
                            sources={"container_recipes": [], "env_specs": [], "build_scripts": [],
                                     "author_image": {"ref": "ghcr.io/org/tool", "source": "ghcr"}}))
    d = R.resolve("tool")
    assert d["chosen"] == "author_image", d["chosen"]
    call = d["install_call"]
    assert call.startswith("freeze_from_image(")
    assert "name=" in call and 'tools=[{"name":' in call and '"evidence":' in call
    assert "(env," not in call


def test_resolve_no_repo_no_gate_conda_wins(monkeypatch):
    # no repo derivable from metadata → author tiers never available → conda as before.
    _stub_registries(monkeypatch, conda=True)
    called = {"n": 0}
    def _spy(*a, **k):
        called["n"] += 1
        return {}
    monkeypatch.setattr(R, "probe_authors_sources", _spy)
    d = R.resolve("samtools")
    assert d["chosen"] == "conda"
    assert called["n"] == 0     # gate not even probed without a repo
