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
# the call-signature contract between resolver and authors_sources
# ---------------------------------------------------------------------------
def test_resolver_call_into_assess_tool_sources_is_signature_compatible():
    """resolver.py calls probe_authors_sources(tool, owner=, repo=, timeout=). Pin that
    exact shape against the real callee.

    This is the guard for the audit-2026-07-16 defect in its most direct form: the call
    carried a `timeout` kwarg the callee didn't accept, so every invocation raised
    TypeError into a bare `except: pass` and the reliability gate never fired for any
    tool — invisibly, because the test doubles declared the signature the CALLER wanted.
    A double can lie about a signature; inspect.bind against the real function cannot.
    """
    import inspect
    assert R.probe_authors_sources is A.assess_tool_sources
    inspect.signature(A.assess_tool_sources).bind(
        "talos", owner="populationgenomics", repo="talos", timeout=12)


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


def _stub_authors_io(monkeypatch, *, dockerfile: str = "", ghcr_package: str = ""):
    """Stub the authors-probe at its I/O SEAM (_default_get_text/_default_get_json), never
    by replacing probe_authors_sources itself.

    This is load-bearing. These tests used to monkeypatch R.probe_authors_sources with a
    `lambda tool, owner="", repo="", timeout=12: ...` double — inventing the `timeout` kwarg
    the real assess_tool_sources never had. The double satisfied the caller; production
    raised TypeError into a bare `except: pass`, so the ENTIRE reliability gate was dead
    for every tool across five commits while these tests stayed green (audit 2026-07-16).

    Stubbing the I/O keeps the real resolve → assess_tool_sources call — signature and all —
    under test. If the call signature drifts again, these tests fail.
    """
    def _get_text(url, timeout=12):
        if dockerfile and url.endswith("/Dockerfile"):
            return dockerfile
        return None
    def _get_json(url, timeout=12):
        # the ghcr probe lists an owner's container packages; a package NAMED FOR THE REPO
        # is the "authors publish an image" signal.
        if ghcr_package and "package_type=container" in url:
            return [{"name": ghcr_package}]
        return []
    monkeypatch.setattr(A, "_default_get_text", _get_text)
    monkeypatch.setattr(A, "_default_get_json", _get_json)


def test_resolve_prefers_authors_recipe_when_reconstruction_incomplete(monkeypatch):
    # tool hits PyPI (would normally be 'pip'), but its repo Dockerfile is incomplete →
    # the gate fires and authors_recipe outranks pip.
    _stub_registries(monkeypatch, pip=True, pip_repo="populationgenomics/talos")
    _stub_authors_io(monkeypatch, dockerfile=_INCOMPLETE_DF)
    d = R.resolve("talos")
    assert "authors_gate_error" not in d["probed"], d["probed"].get("authors_gate_error")
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
    _stub_authors_io(monkeypatch, dockerfile=_SAFE_DF)
    d = R.resolve("mytool")
    assert d["chosen"] == "conda", d["chosen"]
    assert "authors_gate_error" not in d["probed"], d["probed"].get("authors_gate_error")


def test_resolve_adopts_author_image_over_everything(monkeypatch):
    _stub_registries(monkeypatch, conda=True, pip=True, pip_repo="org/tool")
    _stub_authors_io(monkeypatch, ghcr_package="tool")   # ghcr.io/org/tool exists
    d = R.resolve("tool")
    assert "authors_gate_error" not in d["probed"], d["probed"].get("authors_gate_error")
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
