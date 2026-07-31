"""
freeze_from_image — freeze an env from an EXISTING image (the authors' own, or one we
built from their Dockerfile), instead of reconstructing it from conda/pip.

This is the executor for the authors-recipe-first path (see [[feedback-prioritize-authors-
own-env-recipe]] + [[project-env-recipe-always]]). A human handed a tool that ships its
own image or Dockerfile would USE it; this makes the agent do the same as a first-class
primitive rather than a hand-driver (it generalizes the Talos ENV-report hand-driver).

The honesty contract is UNCHANGED — the image still has to earn its registration:
  BUILT               the image + digest resolve in the local daemon
  VALIDATED_IN_IMAGE  every requested tool's evidence command RUNS green IN the image
                      (and references the tool as a real token — echo/print cheats
                      rejected by env_honesty.evidence_shape_violation). The caller
                      supplies evidence that EXERCISES the tool, not merely imports it —
                      the exact gap that let a reconstruction pass unit tests yet not run.
  POLICY_CLEAN        accelerator + license firewall (I12/I13)

Two entry modes, one code path:
  • adopt-image        — an author-published image, adopted by digest (no build).
  • authors-dockerfile — an image we built from the tool's own Dockerfile at a pinned
                         commit (build_env_from_authors_recipe builds it, then calls here).

Deliverables are rendered PURELY from the verified record, same as freeze(): ENV.html +
attestation.json + recipe.yaml + recipe.md (the recipe records the authors' image/source,
so anyone can reproduce it). Docker required.
"""

from __future__ import annotations

import json
import re
import shlex
import subprocess
from pathlib import Path
from typing import Any, Optional

_shq = shlex.quote

from agent.models.core_data import ShippedBinary as _ShippedBinary
from agent.skills import env_recipe, env_recipe_render
from agent.skills.container_build import BASE_IMAGE as _CB_BASE_IMAGE
from agent.skills.outcomes import proven, refused, broke, degraded


def _sh(argv: list[str], timeout: int = 300) -> dict:
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
        return {"rc": r.returncode, "out": r.stdout or "", "err": r.stderr or ""}
    except subprocess.TimeoutExpired as e:
        return {"rc": 124, "out": "", "err": f"timed out after {e.timeout}s"}
    except FileNotFoundError as e:
        return {"rc": 127, "out": "", "err": str(e)}


def _image_present(image: str) -> bool:
    return _sh(["docker", "image", "inspect", image], timeout=60)["rc"] == 0


def _image_digest(image: str) -> str:
    r = _sh(["docker", "image", "inspect", "--format", "{{index .Id}}", image], timeout=60)
    return r["out"].strip() if r["rc"] == 0 else ""


def _run_in_image(image: str, platform: str, command: str, timeout: int = 300,
                  maxlen: int = 400) -> dict:
    """Run a command in the image, return rc + captured output. Uses `bash -c`, NOT
    `bash -lc`: a login shell sources /etc/profile which can CLOBBER the image's own
    `ENV PATH` (e.g. a uv/conda venv baked at the front of PATH) — so `-lc` would fail to
    find the very tools we're validating. `-c` respects the image's environment.

    TWO ATTEMPTS, because a tool image's ENTRYPOINT decides how `bash -c cmd` is read:
      1. NATURAL — `docker run IMAGE bash -c cmd`. With no entrypoint this runs a shell;
         with an env-ACTIVATING entrypoint (micromamba/conda images that `exec "$@"` after
         activating the env) it runs THROUGH it, so the tool lands on PATH — which we want.
         So the natural form is tried first and its success is kept verbatim.
      2. ENTRYPOINT-OVERRIDDEN — `docker run --entrypoint bash IMAGE -c cmd`. The very common
         `ENTRYPOINT ["<tool>"]` pattern (diamond, many biocontainers) EATS `bash -c cmd` as
         the tool's own arguments and never starts a shell, so attempt 1 fails with a tool
         usage error; overriding the entrypoint runs our command directly. This is ALSO how
         the tool is invoked under `apptainer exec` on HPC (Singularity ignores the Docker
         ENTRYPOINT), so it is the more delivery-faithful of the two. The image ENV (PATH)
         applies either way, so a baked venv/conda PATH is still honored.
    A green from EITHER attempt means the command runs in the shipped image; a double failure
    returns the natural attempt's output (the canonical invocation's error).

    `maxlen` caps the returned output (evidence snippets stay short); pass a large value
    when the CALLER must parse the whole output (e.g. an SBOM JSON list) — truncating that
    mid-list would break the parse."""
    base = ["docker", "run", "--rm", "--platform", platform]
    r = _sh(base + [image, "bash", "-c", command], timeout=timeout)
    if r["rc"] != 0:
        r2 = _sh(base + ["--entrypoint", "bash", image, "-c", command], timeout=timeout)
        if r2["rc"] == 0:
            r = r2
    return {"rc": r["rc"], "out": (r["out"] or r["err"] or "").strip()[:maxlen]}


#: The CONTROL image for the discriminating-evidence check below: a stock Debian that
#: carries none of the tools we freeze. Pinned by digest for the same reason the build
#: base is — a control that drifts is not a control.
_CONTROL_IMAGE = _CB_BASE_IMAGE


def _evidence_discriminates(platform: str, evidence: str) -> tuple:
    """Does this evidence command actually distinguish "the tool is here" from "it is not"?

    Returns `(verdict, detail)` where verdict is:
      "discriminating"  — the evidence FAILED in a control image that lacks the tool, so
                          its pass in the real image means something.
      "vacuous"         — it PASSED in the control image too. It would pass anywhere; it
                          proves nothing about what we are shipping.
      "unchecked"       — the control could not be run (no docker / no network / the
                          control image would not start). NOT a pass: absence of a check
                          is recorded as absence, never as compliance.

    WHY AN EXPERIMENT AND NOT ANOTHER REGEX. `evidence` on this path is authored by the
    agent, and until now the only defense was `env_honesty.evidence_shape_violation`
    reading that string. A string rule loses this game by construction: the audit walked
    straight through it with `true || samtools`, `true # samtools`, `[ -n "samtools" ]`
    and `test -f /etc/hosts # samtools` — each references the tool as a word-boundary
    token, none is a bare echo, all four passed, and `evidence_depth` rated the first
    one `functional`, the strongest class. Every one of those is caught here without
    reading the string at all, because none of them can tell the two images apart.

    The shape rule stays as cheap defense-in-depth (it needs no container and it catches
    the lazy shapes before a build is even attempted); this is the load-bearing check.

    Cost: one extra ~0.3s container run per tool against an image the freeze has usually
    already pulled — the same trade the adopt-path validation made, and the same
    justification: the busiest path was the unvalidated one."""
    try:
        if not _image_present(_CONTROL_IMAGE):
            pl = _sh(["docker", "pull", "--platform", platform, _CONTROL_IMAGE], timeout=600)
            if pl["rc"] != 0 or not _image_present(_CONTROL_IMAGE):
                return ("unchecked", f"control image unavailable: {pl['err'][:160]}")
        res = _run_in_image(_CONTROL_IMAGE, platform, evidence, timeout=120)
    except Exception as e:                      # docker absent / daemon down
        return ("unchecked", f"control run failed: {type(e).__name__}: {e}")
    if res["rc"] == 0:
        return ("vacuous",
                f"this command also exits 0 in a stock {_CONTROL_IMAGE.split('@')[0]} that "
                f"does not contain the tool, so passing it in the shipped image proves "
                f"nothing about the shipped image")
    return ("discriminating", f"fails in a control image without the tool (rc={res['rc']})")


#: A self-reported version/identity token: a semver (1.21, 0.7.17-r1188) OR an unversioned
#: fork's commit (9cef4057). Alnum lead, then alnum + . _ - ; MUST contain a digit — which
#: is what separates a real version from a connective word like "version" or "release".
_SELF_VER_TOKEN = re.compile(r"[0-9A-Za-z][0-9A-Za-z._-]*$")


def _parse_self_report(tool: str, out: str) -> Optional[str]:
    """Extract a tool's OWN self-reported version from its `--version` output, or None.

    This is the adopt-path answer to the SBOM's blind spot: a source-built / release
    binary baked into someone else's image carries no package metadata, so the only
    observation of its version is what the binary itself prints. Talos ships a private
    `bcftools` fork with no release — `bcftools --version` prints exactly:

        bcftools 9cef4057            <- the fork's own identity (a commit, not a semver)
        Using htslib 1.23.1          <- a DIFFERENT project's version

    and the earlier whole-blob scrape returned `1.23.1` (htslib's) under bcftools' name.
    Two guards make that lie STRUCTURALLY impossible here, not merely unlikely:

      1. FIRST LINE ONLY. Line 2 ('Using htslib …') is never read, so a dependency's
         version can never be captured no matter what shape it has.
      2. FIRST TOKEN MUST BE THE TOOL. The line must START with the tool's own name
         (basename, case-insensitive) — the tool identifying itself — so a usage banner
         ('Usage: …') or a wrapper's preamble is rejected rather than mined for a number.

    Then the FIRST digit-bearing token after the name is the version — `9cef4057` for the
    fork, `1.21` for samtools, `0.2.2` for echtvar. The digit requirement skips a
    connective word ('mytool version 1.2.3' → '1.2.3', never 'version'). A miss returns
    None = UNRECORDED (the reader renders absence), never a guess: this REPLACES the
    None-always default with a captured fact when one is legibly present, and keeps the
    honest absence when it is not."""
    first = (out or "").strip().split("\n", 1)[0]
    toks = first.split()
    if not toks:
        return None
    head = toks[0].rsplit("/", 1)[-1].rstrip(":").lower()
    if head != (tool or "").lower():
        return None  # not the tool's own line — do not mine it
    for tok in toks[1:]:
        cand = tok.lstrip("vV")
        if any(c.isdigit() for c in cand) and _SELF_VER_TOKEN.fullmatch(cand):
            return cand
    return None


def _self_reported_version(image: str, platform: str, tool: str) -> Optional[str]:
    """Run `<tool> --version` in the image and parse the tool's own first-line self-report
    (see `_parse_self_report`). A non-zero exit or an unparseable banner yields None —
    absence, honestly, never a scraped guess. The one dedicated probe per shipped binary
    is what turns the adopt path's blanket `version: None` into a captured fact when the
    binary states one."""
    res = _run_in_image(image, platform, f"{tool} --version 2>&1", timeout=60)
    if res["rc"] != 0:
        return None
    return _parse_self_report(tool, res["out"])


def freeze_from_image(
    *,
    image: str,
    tools: list[dict],
    name: str,
    env_cache,
    reports_dir: str | Path,
    version: str = "",
    platform: str = "linux/amd64",
    build_method: str = "adopt-image",
    dockerfile_source: Optional[dict] = None,
    request_key: str = "",
    accelerator: Optional[dict] = None,
    gated: bool = False,
    licenses: Optional[list[str]] = None,
    pull_if_absent: bool = True,
) -> dict[str, Any]:
    """Register an EnvCache entry from an existing `image`, gated by the honesty contract.

    `tools`: [{name, evidence}] — each evidence command must RUN the tool in-image and
    exit 0 (this is VALIDATED_IN_IMAGE; the caller owns making it exercise, not import).
    `build_method`: 'adopt-image' | 'authors-dockerfile'. `dockerfile_source`: the pinned
    source {repo, commit, tag, dockerfile?} when built from the authors' Dockerfile — it
    is embedded in the recipe so the build is reproducible.

    Returns proven(...) with the record + deliverable paths, or refused/broke on a missing
    image / honesty violation. Docker required."""
    if not tools:
        return refused("freeze_from_image.no_tools",
                       error="declare at least one tool with an evidence command that RUNS it in-image")
    reports_dir = Path(reports_dir)
    reports_dir.mkdir(parents=True, exist_ok=True)

    # -- BUILT: ensure the image resolves locally (pull if allowed) --
    if not _image_present(image):
        if not pull_if_absent:
            return broke("freeze_from_image.image_absent",
                         error=f"image {image!r} is not in the local daemon and pull is disabled")
        pl = _sh(["docker", "pull", "--platform", platform, image], timeout=1800)
        if pl["rc"] != 0 or not _image_present(image):
            return broke("freeze_from_image.pull_failed",
                         error=f"could not pull {image!r}: {pl['err'][:300]}")
    digest = _image_digest(image)

    # -- VALIDATED_IN_IMAGE: run each tool's evidence IN the image --
    verifications: list[dict] = []
    vacuous: list[str] = []
    for t in tools:
        tname = (t.get("name") or "").strip()
        ev = (t.get("evidence") or "").strip()
        if not tname or not ev:
            return refused("freeze_from_image.bad_tool",
                           error=f"each tool needs a name + evidence command (got {t!r})")
        res = _run_in_image(image, platform, ev)
        from agent.skills import env_honesty as _eh
        # THE DISCRIMINATING CHECK — only meaningful for evidence that PASSED. A failing
        # evidence already refuses via the contract, and paying for a control run to learn
        # why a failure failed buys nothing.
        control, control_note = (_evidence_discriminates(platform, ev)
                                 if res["rc"] == 0 else ("not_applicable", "evidence did not pass"))
        if control == "vacuous":
            vacuous.append(f"{tname}: {ev!r} — {control_note}")
        verifications.append({"label": tname, "tool": tname, "check": ev,
                              "passed": res["rc"] == 0, "rc": res["rc"],
                              "out": res["out"],
                              # DISCLOSURE (not a gate): how deeply this evidence exercises
                              # the tool. 'version'/'import'/'help' prove presence; only
                              # 'smoke'/'functional' prove it RUNS. Surfaced so a shallow
                              # proof can't masquerade as a functional one.
                              "depth": _eh.evidence_depth(ev, tname),
                              # ...and whether it distinguishes this image from any image.
                              "control": control, "control_note": control_note})
    if vacuous:
        # REFUSE, do not degrade. `unchecked` (we could not run the control) is the state
        # that degrades; this is the state where we RAN the experiment and it came back
        # negative — the evidence is proven not to be evidence, and registering a green
        # on it would be the precise thing the honesty contract exists to prevent.
        return refused("freeze_from_image.vacuous_evidence",
                       error="evidence that would pass with or without the tool installed — "
                             "it cannot prove the image carries what you asked for: "
                             + "; ".join(vacuous),
                       vacuous_evidence=vacuous, verifications=verifications)

    # -- SBOM captured FROM the shipped image (can't be faked) --
    # A capture FAILURE must not look like an image with no dependencies: an empty
    # resolved_packages renders as "0 along for the ride" in the ENV report and as an
    # empty package list in the attestation — absence of data reading as data. So each
    # probe records WHY it came back empty, the same way the authors gate records
    # authors_gate_error and cluster accounting records sacct_error. Not a refusal: some
    # images legitimately carry no conda prefix and no apt layer.
    resolved_packages: list[dict] = []
    system_packages: list[dict] = []
    sbom_errors: list[str] = []
    from agent.skills.container_build import ContainerBuild as _CB
    try:
        resolved_packages = _CB.conda_sbom_from_image(image, platform) or []
    except Exception as e:
        sbom_errors.append(f"conda: {type(e).__name__}: {e}")
    try:
        # separate try — an apt failure must not also discard a good conda closure
        system_packages = _CB.apt_sbom_from_image(image, platform) or []
    except Exception as e:
        sbom_errors.append(f"apt: {type(e).__name__}: {e}")
    # pip-only / venv images (no conda prefix) — best-effort importlib.metadata SBOM,
    # as a single-line `python -c` (a heredoc through `docker run bash -c` is fragile).
    if not resolved_packages:
        probe = ("import importlib.metadata as m,json;"
                 "print(json.dumps(sorted(set(d.metadata['Name']+'=='+d.version "
                 "for d in m.distributions() if d.metadata['Name']))))")
        pj = _run_in_image(image, platform, f"python -c {_shq(probe)}", timeout=120, maxlen=200_000)
        if pj["rc"] == 0 and "[" in pj["out"]:
            try:
                lst = json.loads(pj["out"][pj["out"].index("["):])
                # Emit the STANDARD SBOM shape {name, version, kind} that every other
                # producer follows (_parse_prefix_scan / conda_sbom_from_image). The old
                # {name: "pkg==ver", manager: "pip"} shape put the version inside the name
                # and omitted `kind` (a dead key `manager` nobody reads), so every consumer
                # keyed on name/kind — tool_identity.capture, attestation._purl, the env
                # report's per-tool version — silently failed on venv/pip author images.
                resolved_packages = []
                for s in lst:
                    nm, _, vr = s.partition("==")
                    if nm:
                        resolved_packages.append({"name": nm, "version": vr, "kind": "pypi"})
            except (ValueError, IndexError):
                pass

    # -- assemble the record + run the honesty contract --
    primary = tools[0]["name"]
    version = version or ""
    rkey = request_key or f"{primary}={version or (digest.split(':')[-1][:12])}|{platform}|none"
    mode = "adopt" if build_method == "adopt-image" else "build"
    from agent.skills import freeze as _freeze
    record = _freeze.freeze_record(
        request_key=rkey, content_digest=digest, mode=mode,
        image=image, image_digest=digest, platform=platform, gated=gated)
    record["name"] = name
    record["version"] = version
    record["build_method"] = build_method
    record["requested_tools"] = [t["name"] for t in tools]
    record["verifications"] = verifications
    record["resolved_packages"] = resolved_packages
    record["system_packages"] = system_packages
    if sbom_errors and not resolved_packages:
        # only surfaced when the SBOM is ACTUALLY empty — a conda probe that failed on an
        # image the importlib fallback then read successfully is not a gap worth flagging.
        record["sbom_error"] = "; ".join(sbom_errors)
    record["accelerator"] = accelerator
    record["licenses"] = list(licenses or [])
    record["redistributable"] = not gated
    if dockerfile_source:
        record["dockerfile_source"] = dict(dockerfile_source)
    # The authors' image: we ADOPTED these bytes, we did not build them — so there is no
    # `install_command` to show and no tier assurance to disclose, and saying so explicitly
    # is the record. But the SBOM cannot see a source-built / release binary the authors
    # baked in, so its ONLY observable version is what the binary prints about ITSELF: we
    # probe `<tool> --version` and capture the tool's own first-line token
    # (`_self_reported_version`) — `9cef4057` for Talos's unversioned bcftools fork, `0.2.2`
    # for echtvar. Its two guards (first line only; first token must be the tool) make the
    # old htslib-under-bcftools lie STRUCTURALLY impossible; a miss stays None = "unrecorded",
    # a captured fact when the binary states one and honest absence when it does not.
    record["shipped_binaries"] = [
        _ShippedBinary(
            tool=t["name"],
            version=_self_reported_version(image, platform, t["name"]),
            provenance=f"validated in the {build_method} image (evidence: {t['evidence'][:60]})",
            install_command=None,
            tier=None, verified=None, assurance=None,
        ).model_dump()
        for t in tools]

    # IDENTITY DISCLOSURE (audit #8): what each requested tool says it IS, read from the
    # registry the shipped package came from (matched via the image's own SBOM). Agent-
    # asserted, best-effort — captured BEFORE check_build so the checked record is the
    # registered one; a probe miss yields self_description=None and never fails the freeze.
    from agent.skills import tool_identity as _ti
    try:
        record["tool_identities"] = _ti.capture(record["requested_tools"], resolved_packages)
    except Exception:
        record["tool_identities"] = []

    from agent.skills import env_honesty
    contract = env_honesty.evaluate_build(record)
    violations = contract.violations
    if violations:
        return refused("freeze_from_image.honesty_violation",
                       error=f"the image failed the honesty contract ({len(violations)} violation(s)) — "
                             "not registered",
                       honesty_violations=violations, verifications=verifications)

    # -- register + deliverables (rendered purely from the record) --
    env_cache.register(rkey, record)
    # ADOPT MUST RECORD WHAT SOMEONE ELSE CAN PULL, NOT WHAT WE HAPPEN TO CALL IT.
    # This passed `image` through verbatim — a MUTABLE TAG — while the rendered recipe
    # printed it under "pulling that image BY DIGEST (content-addressed — the digest
    # guarantees identical bytes)". The tag moves; the sentence doesn't. `freeze()`'s adopt
    # path has always done this correctly (`adopt.get("image_by_digest", image)`), so this
    # was one concept with two implementations and the wrong one under the top tier.
    # `.Id` is NOT the answer either: it is the daemon's LOCAL content id, not a pullable
    # reference (see container_build.registry_manifest_digest — the same confusion already
    # shipped once and reported "recipe not reproduced" for every adopt recipe on any
    # overlay2 daemon). When no repo digest exists the image was never pulled from a
    # registry, so there is nothing to pin: say so rather than emit an unpullable string.
    adopt_ref = ""
    if build_method == "adopt-image":
        from agent.skills.container_build import registry_manifest_digest
        md = registry_manifest_digest(image)
        adopt_ref = f"{image.split('@', 1)[0].split(':')[0]}@{md}" if md else ""
        if adopt_ref:
            record["image_by_digest"] = adopt_ref
        else:
            record["adopt_pin_error"] = (
                f"{image} carries no registry manifest digest — it was not pulled from a "
                f"registry, so it cannot be pinned or re-pulled by anyone else")
    recipe = env_recipe.extract_recipe(
        None, name=name, version=version, conda_deps=[],
        primary_tools=[t["name"] for t in tools], platform=platform,
        accelerator=accelerator, license_gated=gated, licenses=licenses,
        redistributable=not gated, content_digest=digest,
        build_method=("authors-dockerfile" if build_method == "authors-dockerfile" else "adopt"),
        adopt_image=adopt_ref,
        dockerfile_source=dockerfile_source or {})
    recipe["shipped_binaries"] = record["shipped_binaries"]
    recipe["tool_identities"] = record.get("tool_identities") or []
    # Carry the OBSERVED SBOM (what actually shipped) beside conda_deps so the machine
    # recipe is self-describing about its installed contents (audit 2026-07-19, W4).
    # Named `resolved_packages` (the record's OBSERVED-closure key), never
    # `installed_packages` — that collides with the per-step request pin (hunt 2026-07-20).
    recipe["resolved_packages"] = record.get("resolved_packages") or []
    recipe["system_packages"] = record.get("system_packages") or []

    out_paths: dict[str, str] = {}
    for label, fname, render in (
        ("env_report", f"{name}.ENV.html",
         lambda: __import__("agent.skills.env_report_html", fromlist=["render_env_report_html"])
                 .render_env_report_html(record)),
        ("attestation", f"{name}.attestation.json",
         lambda: json.dumps(__import__("agent.skills.attestation", fromlist=["build_attestation"])
                            .build_attestation(record, base_image=""), indent=2)),
        ("recipe", f"{name}.recipe.yaml",
         lambda: __import__("yaml").safe_dump(recipe, sort_keys=False)),
        ("recipe_md", f"{name}.recipe.md",
         lambda: env_recipe_render.render_recipe_markdown(recipe, record)),
    ):
        try:
            (reports_dir / fname).write_text(render())
            out_paths[label] = str(reports_dir / fname)
        except Exception as e:
            out_paths[label] = f"({label} render failed: {e!r})"

    # SOFT advisory (never a refusal): requested tools whose evidence only proved
    # presence/loads, not that the tool RUNS. Surfaced so the agent can strengthen the
    # evidence — the honest nudge against a shallow proof reading as functional.
    # Read the depth set from the classifier that defines it — a second stale copy of
    # _SHALLOW_DEPTHS here omitted `presence` and silently under-reported the advisory.
    shallow = [v["tool"] for v in verifications
               if v.get("depth") in env_honesty._SHALLOW_DEPTHS or v.get("depth") == "unknown"]
    advisory = ""
    if shallow:
        advisory = ("shallow evidence (proves presence, not function) for: "
                    + ", ".join(shallow) + " — consider evidence that RUNS the tool on an input")
    # The contract passed; now say how much of it actually looked at anything. An
    # UNOBSERVED clause makes this `degraded` rather than `proven` — same artifact, same
    # registration, honest tag. Branch written out with literal helpers + literal codes so
    # scripts/extract_outcomes.py still harvests both terminals (see coverage_disclosure).
    fields = dict(success=True, cache_hit=False,
                  request_key=rkey, image=image, image_digest=digest,
                  content_digest=digest, build_method=build_method, platform=platform,
                  verifications=verifications, shallow_evidence=shallow,
                  evidence_advisory=advisory,
                  **env_honesty.coverage_disclosure(contract), **out_paths)
    if contract.unobserved:
        return degraded("freeze_from_image.frozen_unobserved", **fields)
    return proven("freeze_from_image.frozen", **fields)


def _clone_url(repo: str) -> str:
    """Normalize a repo argument to a git-cloneable URL. An explicit scheme
    (http/https/git@/file://) is used verbatim; a bare 'owner/repo' is GitHub."""
    return repo if repo.startswith(("http://", "https://", "git@", "file://")) else f"https://github.com/{repo}"


def build_from_authors_recipe(
    *,
    repo: str,
    tools: list[dict],
    name: str,
    env_cache,
    reports_dir: str | Path,
    recipe: str = "Dockerfile",
    ref: str = "",
    version: str = "",
    platform: str = "linux/amd64",
    build_args: Optional[dict] = None,
    gated: bool = False,
    licenses: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Clone the tool's OWN repo at a pinned `ref`, `docker build` its `recipe` (their
    Dockerfile), then hand the built image to freeze_from_image (same honesty contract +
    deliverables). The declarative executor for resolve_tool's `authors_recipe` tier — the
    path taken when the authors' recipe installs pieces a conda/pip reconstruction would
    silently DROP. The pinned source (repo + resolved commit + tag + Dockerfile verbatim)
    is recorded so the build is reproducible. Docker + git (+ network for a remote repo).

    Kept a thin, injectable executor (env_cache + reports_dir params) so it mirrors
    freeze_from_image and is testable on real bytes without the MCP singletons."""
    if not tools:
        return refused("authors_recipe.no_tools",
                       error="declare at least one tool with an evidence command that RUNS it in-image")
    if not (repo or "").strip():
        return refused("authors_recipe.no_repo", error="repo required ('owner/repo' or a git URL)")

    import tempfile as _tf
    url = _clone_url(repo)
    with _tf.TemporaryDirectory(prefix="authors_recipe_") as td:
        cl = _sh(["git", "clone", "--depth", "1"] + (["--branch", ref] if ref else []) + [url, td], timeout=600)
        if cl["rc"] != 0:
            # --branch fails on a raw commit SHA; retry a full clone + checkout
            cl2 = _sh(["git", "clone", url, td], timeout=600)
            if cl2["rc"] != 0:
                return broke("authors_recipe.clone_failed",
                             error=f"could not clone {url}: {(cl2['err'] or cl['err'])[:300]}")
            if ref:
                co = _sh(["git", "-C", td, "checkout", ref], timeout=120)
                if co["rc"] != 0:
                    return broke("authors_recipe.checkout_failed",
                                 error=f"could not checkout {ref!r}: {co['err'][:300]}")
        commit = _sh(["git", "-C", td, "rev-parse", "HEAD"], timeout=60)["out"].strip()
        tag = f"{name}:{version}" if version else f"{name}:latest"
        buildx = ["docker", "buildx", "build", "--platform", platform, "--load", "-f", f"{td}/{recipe}", "-t", tag]
        for k, v in (build_args or {}).items():
            buildx += ["--build-arg", f"{k}={v}"]
        buildx.append(td)
        bd = _sh(buildx, timeout=3600)
        if bd["rc"] != 0:
            return broke("authors_recipe.build_failed",
                         error=f"docker build of {recipe} failed: {(bd['err'] or '')[-800:]}",
                         dockerfile=recipe)
        try:
            dockerfile_text = (Path(td) / recipe).read_text()
        except OSError:
            dockerfile_text = ""

    return freeze_from_image(
        image=tag, tools=[dict(t) for t in tools], name=name, version=version,
        platform=platform, build_method="authors-dockerfile",
        # RECORD WHAT WE ACTUALLY RAN. `recipe` and `build_args` were used two lines up
        # (`-f {td}/{recipe}`, `--build-arg`) and then dropped here, so the rendered recipe
        # always emitted a bare `docker build .` — rebuilding the ROOT Dockerfile for a
        # `recipe="docker/Dockerfile.gpu"` build, with every --build-arg silently reverting
        # to the Dockerfile's ARG defaults. Talos's own Dockerfile carries
        # `ARG BCFTOOLS_VERSION=1.23.1` and `ARG ECHTVAR_VERSION=v0.2.2` — precisely the
        # knobs whose drift this project exists to prevent. The executor had both facts in
        # hand; the record simply had nowhere to put them (Rule 1: fix the PRODUCER).
        dockerfile_source={"repo": url, "commit": commit, "tag": ref or "",
                           "recipe_path": recipe, "build_args": dict(build_args or {}),
                           "platform": platform, "dockerfile": dockerfile_text},
        gated=gated, licenses=list(licenses or []), pull_if_absent=False,
        env_cache=env_cache, reports_dir=reports_dir)
