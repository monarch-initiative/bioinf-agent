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
def _stub_registries(monkeypatch, *, conda=False, pip=False, pip_repo="", conda_repo="",
                     pip_summary="a genomics variant caller for VCF files"):
    """What ANCHORS a repo (so the gate may run on it) is now ONLY a curated conda `dev_url`
    (`conda_repo`) or an explicit `github_repo` passed to resolve(). A repo merely SCRAPED
    from pip/cran metadata is a candidate, never an anchor.

    Phase 2 (2026-07-17) deleted the 86-word domain word-list that used to anchor a scraped
    pip repo whenever its summary read like bioinformatics — that conflated "did the authors
    publish this" with "is this a bio tool", and live it pointed the gate at `Mucephie/DORADO`
    (astronomy) for `dorado` and `ethereum/trinity` for `trinity`. So `pip_summary` no longer
    anchors anything; identity is the ride's judgment now. See test_repo_provenance.py."""
    cd = {"available": conda, "latest": "1.0", "channel": "bioconda",
          "summary": "a genomics tool"}
    if conda_repo:
        cd["repo"] = conda_repo
        cd["repo_field"] = "dev_url"
    monkeypatch.setattr(R, "probe_conda", lambda n, t=12: dict(cd))
    urls = {"Source": f"https://github.com/{pip_repo}"} if pip_repo else {}
    monkeypatch.setattr(R, "probe_pypi", lambda n, t=12: {"available": pip, "latest": "1.0",
                                                          "summary": pip_summary,
                                                          "home_page": "", "project_urls": urls, "package_url": ""})
    monkeypatch.setattr(R, "probe_cran", lambda n, t=12: {"available": False})
    monkeypatch.setattr(R, "probe_bioconductor", lambda n, t=12: {"available": False})
    # The GitHub side of the same page. Unstubbed, these tests called api.github.com
    # for real — under the unauthenticated 60/hr quota, which is exactly the rate-limit
    # this repo's resolver docs describe sliding a pick from conda to binary. A gate
    # test that can be flipped by someone else's quota is not testing the gate.
    monkeypatch.setattr(R, "probe_github", lambda repo, t=12: {
        "repo_exists": True, "has_release_assets": False, "assets": [],
        "is_fork": False, "parent": "", "upstream": "",
        "full_name": repo, "default_branch": "main"})
    monkeypatch.setattr(R, "_canon_repo",
                        lambda repo, t=12: ((repo or "").strip().strip("/").lower(), "ok"))


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
        return []
    def _probe_image(owner, repo, tag="latest", timeout=12):
        # The author-image signal is "can we PULL ghcr.io/{owner}/{repo}?", answered by
        # the registry. It used to be "is a package listed under this org?", answered by
        # api.github.com/orgs/{owner}/packages — which returns 401 for EVERY org, public
        # ones included, because that API always requires auth. No token was ever plumbed
        # and the 401 was swallowed, so this tier was 100% dead while reporting "the
        # authors ship no image" (audit 2026-07-16 Tier 6).
        if ghcr_package and ghcr_package.lower() == repo.lower():
            return {"ref": f"ghcr.io/{owner}/{repo}", "source": "ghcr", "tag": tag}
        return None
    monkeypatch.setattr(A, "_default_get_text", _get_text)
    monkeypatch.setattr(A, "_default_get_json", _get_json)
    monkeypatch.setattr(A, "_default_probe_ghcr_image", _probe_image)


def test_resolve_prefers_authors_recipe_when_reconstruction_incomplete(monkeypatch):
    # tool hits PyPI (would normally be 'pip'), but its repo's Dockerfile is incomplete →
    # the gate fires and authors_recipe outranks pip. Phase 2 (2026-07-17): the gate runs on
    # an ANCHORED repo — here the explicit github_repo the ride confirmed. This IS the Talos
    # flow: the LLM knows populationgenomics/talos is the rare-disease pipeline (its
    # Dockerfile compiles a bcftools fork + htslib + echtvar a pip reconstruction drops), not
    # PyPI's Keras tuner, so it passes the repo and the authors' recipe path fires.
    _stub_registries(monkeypatch, pip=True, pip_repo="populationgenomics/talos")
    _stub_authors_io(monkeypatch, dockerfile=_INCOMPLETE_DF)
    d = R.resolve("talos", github_repo="populationgenomics/talos")
    assert "authors_gate_error" not in d["probed"], d["probed"].get("authors_gate_error")
    assert d["chosen"] == "authors_recipe", d["chosen"]
    # the rendered guidance must match the REAL executor signature (an agent copies it):
    # required name=, tools as [{name, evidence}] dicts — NOT a phantom `env` positional
    # or bare-string tools that the executor would reject.
    call = d["install_call"]
    assert call.startswith("build_env_from_authors_recipe(")
    assert "name=" in call and 'tools=[{"name":' in call and '"evidence":' in call
    assert "(env," not in call


def test_a_bare_pip_scraped_repo_does_not_auto_fire_the_authors_path(monkeypatch):
    """Phase 2 SAFETY PROPERTY. Without an explicit github_repo, a repo merely scraped from
    pip metadata is a candidate the ride must confirm — it is NOT handed to the author tiers
    (which outrank conda). This is the guard, now enforced without a word-list, that keeps a
    bare `resolve('dorado')` from adopting an astronomy repo's image at the top tier: the
    scraped repo is disclosed as NOT ASSESSED, and routing falls back to the registry pick."""
    _stub_registries(monkeypatch, pip=True, pip_repo="populationgenomics/talos")
    _stub_authors_io(monkeypatch, dockerfile=_INCOMPLETE_DF)
    d = R.resolve("talos")
    assert d["chosen"] == "pip", d["chosen"]
    assert d["probed"].get("authors_gate_not_assessed"), \
        "a scraped pip repo must be disclosed as NOT ASSESSED, not silently skipped"
    assert "authors_recipe" not in d["probed"] and "author_image" not in d["probed"]
    # the ride is told exactly how to unlock the authors' path: confirm the repo
    assert "AUTHORS-PATH NOT ASSESSED" in d["install_call"]


def test_resolve_keeps_conda_when_reconstruction_is_safe(monkeypatch):
    # tool on conda AND pip, repo Dockerfile is trivial → gate stays shut, conda wins.
    _stub_registries(monkeypatch, conda=True, pip=True, pip_repo="me/mytool",
                     conda_repo="me/mytool")
    _stub_authors_io(monkeypatch, dockerfile=_SAFE_DF)
    d = R.resolve("mytool")
    assert d["chosen"] == "conda", d["chosen"]
    assert "authors_gate_error" not in d["probed"], d["probed"].get("authors_gate_error")


def test_resolve_adopts_author_image_over_everything(monkeypatch):
    _stub_registries(monkeypatch, conda=True, pip=True, pip_repo="org/tool",
                     conda_repo="org/tool")
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
    monkeypatch.setattr(R, "_resolve_biocontainer", lambda pkgs, timeout=12: {"found": False})
    d = R.resolve("samtools")
    assert d["chosen"] == "conda"
    assert called["n"] == 0     # gate not even probed without a repo


# ---------------------------------------------------------------------------
# THE OVER-FIRE FIX (2026-07-17): a Dockerfile BUILD never beats a clean conda
# package. authors_recipe ranks BELOW conda, so a cleanly-bioconda tool goes to
# conda even when its own Dockerfile is flagged reconstruction-incomplete.
# ---------------------------------------------------------------------------
def test_clean_conda_beats_authors_recipe_even_when_recipe_is_incomplete(monkeypatch):
    """The gatk4/miniprot over-fire, as a regression guard.

    Before: `analyze_recipe_completeness` flagged self-build `make` + a self-source tarball
    as "incomplete", authors_recipe outranked conda, and cleanly-bioconda tools were routed
    to a HEAVY Dockerfile build instead of `conda install`. That contradicts the whole point
    — a package the ecosystem already built + containerized should win.

    Here the recipe is GENUINELY incomplete (Talos-shaped: vendored fork + system + fetched),
    AND the tool is cleanly on conda. Conda still wins: a clean bioconda package includes its
    deps, and if it somehow didn't, freeze's VALIDATED_IN_IMAGE catches it. The gate still RAN
    (authors_recipe is available in `probed`) — it just no longer OUTRANKS a clean package."""
    _stub_registries(monkeypatch, conda=True, pip=True, pip_repo="org/tool",
                     conda_repo="org/tool")
    _stub_authors_io(monkeypatch, dockerfile=_INCOMPLETE_DF)
    monkeypatch.setattr(R, "_resolve_biocontainer", lambda pkgs, timeout=12: {"found": False})
    d = R.resolve("tool")
    assert d["chosen"] == "conda", d["chosen"]
    # the gate still ran and still SEES the recipe as incomplete — it's just outranked:
    assert d["probed"].get("authors_recipe", {}).get("available") is True
    assert R.TIER_ORDER.index("conda") < R.TIER_ORDER.index("authors_recipe")


def test_authors_recipe_still_fires_when_there_is_NO_clean_conda_package(monkeypatch):
    """The demotion must not break Talos. With no conda hit (pip-only), authors_recipe still
    outranks pip — the reconstruction case the gate exists for."""
    _stub_registries(monkeypatch, conda=False, pip=True, pip_repo="populationgenomics/talos")
    _stub_authors_io(monkeypatch, dockerfile=_INCOMPLETE_DF)
    d = R.resolve("talos", github_repo="populationgenomics/talos")
    assert d["chosen"] == "authors_recipe", d["chosen"]
    assert R.TIER_ORDER.index("authors_recipe") < R.TIER_ORDER.index("pip")


# ---------------------------------------------------------------------------
# The unified "is there a pullable image?" fact (pull-don't-build).
# ---------------------------------------------------------------------------
def test_pullable_image_surfaces_the_biocontainer_for_a_conda_pick(monkeypatch):
    """A clean conda pick carries a pullable BioContainer (adopt by digest — pull, no build),
    so an agent sees the shortcut. Provenance is the curated quay.io/biocontainers namespace,
    never a third party's image."""
    _stub_registries(monkeypatch, conda=True)
    digest_ref = "quay.io/biocontainers/samtools@sha256:" + "a" * 64
    monkeypatch.setattr(R, "_resolve_biocontainer", lambda pkgs, timeout=12: {
        "found": True, "image": "quay.io/biocontainers/samtools:1.21--h0",
        "image_by_digest": digest_ref, "digest": "sha256:" + "a" * 64})
    d = R.resolve("samtools")
    assert d["chosen"] == "conda"
    pull = d["pullable_image"]
    assert pull["found"] is True and pull["source"] == "biocontainer"
    assert pull["image_by_digest"] == digest_ref
    assert 'build_method="adopt-image"' in pull["adopt_call"]
    assert "biocontainers" in pull["provenance"]
    # the pull-by-digest shortcut is named in the human-readable rationale too:
    assert digest_ref in d["rationale"]


def test_pullable_image_reports_the_authors_own_image_when_available(monkeypatch):
    """When the authors publish their own image it IS the pick (author_image tier); the
    pullable_image fact reports source=author_image so the unified check is truly unified."""
    _stub_registries(monkeypatch, conda=True, pip=True, pip_repo="org/tool", conda_repo="org/tool")
    _stub_authors_io(monkeypatch, ghcr_package="tool")
    monkeypatch.setattr(R, "_resolve_biocontainer", lambda pkgs, timeout=12: {"found": False})
    d = R.resolve("tool")
    assert d["chosen"] == "author_image"
    assert d["pullable_image"]["found"] is True
    assert d["pullable_image"]["source"] == "author_image"


def test_pullable_image_is_not_fabricated_from_a_scraped_unanchored_repo(monkeypatch):
    """PROVENANCE GUARDRAIL. A bare name whose only repo is scraped from pip metadata does NOT
    yield a pullable AUTHOR image (the author tiers never run on an unanchored repo — the
    cellranger-third-party-image trap). Any pullable image here can only be a biocontainer,
    from the curated namespace — never a stranger's image claiming to be the tool."""
    _stub_registries(monkeypatch, pip=True, pip_repo="Mucephie/dorado")   # astronomy squat
    _stub_authors_io(monkeypatch, ghcr_package="dorado")   # a ghcr image EXISTS under that repo
    monkeypatch.setattr(R, "_resolve_biocontainer", lambda pkgs, timeout=12: {"found": False})
    d = R.resolve("dorado")
    # the scraped repo was NOT assessed, so no author image is adopted from it:
    assert d["pullable_image"]["found"] is False
    assert "author_image" not in d["probed"]


# ---------------------------------------------------------------------------
# The author-image probe: it must ask the REGISTRY, and it must not report a
# broken probe as a negative finding. (audit 2026-07-16 Tier 6)
# ---------------------------------------------------------------------------

def test_author_image_probe_asks_the_registry_not_the_github_package_index():
    """The probe must be answerable ANONYMOUSLY, so it must hit ghcr's own token+manifest
    flow — never `api.github.com/orgs/{owner}/packages`.

    That endpoint returns 401 for every org on earth (public ones included: verified live
    against nf-core, bioconda, astral-sh); the packages REST API always requires auth, and
    no token was ever plumbed. So the top-ranked tier of the reliability gate could not
    have worked even in principle — and its 401 was swallowed in `_default_get_text`, two
    layers below the error recorder, so it reported "the authors ship no image".

    It was also the wrong question: package *listing* needs auth and excludes user-owned
    repos (`/orgs/` only), while "can I pull this?" is what we actually need — and an image
    we can't pull anonymously is one we could never adopt anyway."""
    import ast
    import inspect
    import textwrap
    # Read the CODE, not the prose: the docstring deliberately names api.github.com to
    # record why it's gone, and a naive substring check would flag its own explanation.
    tree = ast.parse(textwrap.dedent(inspect.getsource(A._default_probe_ghcr_image)))
    fn = tree.body[0]
    if ast.get_docstring(fn):
        fn.body = fn.body[1:]
    code = "\n".join(ast.unparse(n) for n in fn.body)
    assert "api.github.com" not in code, (
        "the author-image probe must not use GitHub's package index — it 401s for every "
        "org, so the tier would be dead again")
    assert "ghcr.io/token" in code and "/manifests/" in code


def test_author_image_probe_reports_a_denial_as_a_true_negative(monkeypatch):
    """ghcr DENYING an anonymous pull scope means the image is private or absent — a real
    answer ("no adoptable image"), not a probe failure. Reporting it as an error would
    stamp a scary NB on every cleanly-packaged tool and train the reader to skip it."""
    import urllib.error
    def _denied(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 403, "denied", {}, None)
    monkeypatch.setattr(A.urllib.request, "urlopen", _denied)
    assert A._default_probe_ghcr_image("someone", "private-thing") is None


def test_author_image_probe_failure_is_disclosed_never_read_as_no_image(monkeypatch):
    """R2: absence of data must not render as data. A network failure must surface as
    `author_image_error` AND poison the recommendation text — because every sentence the
    verdict emits otherwise assumes `author_image: None` was an observation."""
    import urllib.error
    def _broken(req, timeout=None):
        raise urllib.error.URLError("network down")
    monkeypatch.setattr(A.urllib.request, "urlopen", _broken)
    probed = A._default_probe_ghcr_image("owner", "repo")
    assert probed and probed.get("error"), probed

    out = A.assess_tool_sources("tool", owner="owner", repo="repo", sources={
        "container_recipes": [], "env_specs": [], "build_scripts": [],
        "author_image": None, "author_image_error": "URLError: network down"})
    assert out["author_image_error"] == "URLError: network down"
    assert "UNCHECKED" in out["recommendation"], out["recommendation"]


def test_a_clean_ghcr_miss_does_not_claim_the_authors_ship_no_image():
    """R2 on the SUCCESS path, not just the error path.

    The probe checks ghcr.io and nothing else, yet the verdict said "no authoring
    image/recipe found" — a claim about every registry on earth drawn from one. The blind
    spot is not incidental: bioinformatics publishes on Docker Hub and quay (biocontainers),
    so the registries we skip are exactly the ones our tools live on. The gate is green in
    its own test case (uv, a Rust tool that happens to be on ghcr) and blind across the
    actual domain — and it fails as a CONFIDENT NEGATIVE, so "the gate returns results"
    passes while the answer is fiction.

    Asserted as substring-absence on OUR OWN string (per the corpus row
    `dorado-author-image-invisible-ghcr-only`), never against live registry text."""
    out = A.assess_tool_sources("dorado", owner="nanoporetech", repo="dorado", sources={
        "container_recipes": [], "env_specs": [], "build_scripts": [],
        "author_image": None})          # a CLEAN miss: probe ran, ghcr had nothing
    rec = out["recommendation"]
    assert "no authoring image/recipe found" not in rec, rec
    assert "ghcr.io" in rec and "UNCHECKED" in rec, rec
    assert "Docker Hub" in rec and "quay" in rec, rec
    # ...and it must NOT masquerade as a probe failure — nothing broke here
    assert "author_image_error" not in out


def test_a_found_author_image_carries_no_unchecked_caveat():
    """The caveat must stay scarce: when the authors DO ship an image, nothing is unchecked
    that matters, and a warning bolted on regardless is the noise that trains a reader to
    skip the real one."""
    out = A.assess_tool_sources("uv", owner="astral-sh", repo="uv", sources={
        "container_recipes": [], "env_specs": [], "build_scripts": [],
        "author_image": {"ref": "ghcr.io/astral-sh/uv", "source": "ghcr", "tag": "latest"}})
    assert "UNCHECKED" not in out["recommendation"]
    assert "adopt it by digest" in out["recommendation"]


def test_a_broken_gate_poisons_the_install_call_not_just_probed(monkeypatch):
    """A FAILED reliability gate must reach the field an agent copies.

    `authors_gate_error` was recorded by the Tier-0 fix and then read by NOBODY: resolve()
    returned a clean, confident `chosen: conda` + a paste-ready install_call, with the
    failure parked in `probed` where nothing looks. For Talos that is precisely the
    silent-reconstruction bug the gate exists to prevent, served with full confidence.

    Worse, NOTHING FAILED when the fix was reverted to `except: pass` — verified during
    the re-audit: the whole suite stayed green. The fix for the silent gate was itself
    silent and untested. This test is that missing guard."""
    _stub_registries(monkeypatch, conda=True, pip=True, pip_repo="populationgenomics/talos",
                     conda_repo="populationgenomics/talos")

    def _boom(tool, owner="", repo="", timeout=12):
        raise OSError("github unreachable")
    monkeypatch.setattr(R, "probe_authors_sources", _boom)

    d = R.resolve("talos")
    assert d["chosen"] == "conda"                       # registry routing still works
    assert d["probed"].get("authors_gate_error")        # ...and is recorded
    # the load-bearing part: it must be IMPOSSIBLE to copy the install_call without
    # reading that the authors' path was never checked
    assert "AUTHORS-PATH GATE FAILED" in d["install_call"], d["install_call"]
    assert "AUTHORS-PATH GATE FAILED" in d["rationale"]


def test_a_healthy_gate_leaves_the_install_call_clean(monkeypatch):
    """The pair: no error ⇒ no noise. A warning that fires on healthy tools is a warning
    readers learn to strip — the calibration lesson from the identity campaign."""
    _stub_registries(monkeypatch, conda=True)
    _stub_authors_io(monkeypatch)
    d = R.resolve("samtools", github_repo="samtools/samtools")
    assert d["chosen"] == "conda"
    assert "authors_gate_error" not in d["probed"]
    assert "GATE FAILED" not in d["install_call"], d["install_call"]
    assert d["install_call"].startswith("install_conda_packages("), d["install_call"]
