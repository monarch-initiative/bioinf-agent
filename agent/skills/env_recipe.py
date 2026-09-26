"""
env_recipe — the portable, SELF-CONTAINED build recipe for a frozen env, plus the
rebuild-from-recipe verification.

freeze() already DESCRIBES an env (SBOM / attestation). The recipe is the thing that
REBUILDS it: everything `build_env_image` needs with NO draft / agent / pipeline-state
— the non-conda install_methods (the synthesized commands + provenance + commit,
jar/binary/source/cargo/go/perl), the conda specs, the policy flags, and the expected
content_digest. It is the artifact a colleague, CI, or verify-by-rebuild consumes.

`rebuild_from_recipe()` replays it and recomputes content_digest. WHAT THIS PROVES,
precisely (the honest standard — no overclaiming):
  • COMPLETENESS — the recipe is self-contained: a rebuild from it ALONE (fresh, no
    draft) succeeds. If anything were hiding in the draft, the rebuild would fail.
  • LOCAL DETERMINISM / CONVERGENCE — replaying it HERE yields the same content_digest
    for every layer that is actually re-executed: the apt snapshot, the jar / binary /
    source / cargo / go tiers, and the image assembly.
It does NOT prove cross-machine reproducibility (different base cache / network bytes /
docker) or independent-party tamper-evidence — those are this SAME rebuild run ELSEWHERE
(CI, a colleague) + signing. The recipe ENABLES them; this verifies the necessary local
conditions. Pure assembly here; the rebuild's I/O is injected for testing.

AND IT DOES NOT PROVE THE CONDA SOLVE CONVERGED. The replay passes
`conda_lock_files=recipe["conda_lock"]`, and on a prebaked lock `env_build` takes the
`declare_locked` branch — `pixi install --locked`, NO solve. Replaying the lock is the
RIGHT design (it is what makes the rebuild immune to bioconda drift), but it means the
conda package set is pinned BY CONSTRUCTION, so its agreement is not evidence of
anything. Never name the lock layer as a convergence signal: `proves` ships this
module's claims in the tool's return value, so an overclaim here reaches the reader at
runtime.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

from agent.skills.outcomes import proven, broke

RECIPE_VERSION = 1

#: The build_methods whose image came from a Dockerfile rather than from our own
#: container-native path. Read by `verify_env_recipe` (which refuses to rebuild them) and
#: by the recipe renderer (which must not tell the reader to run that refusal).
#:
#: `"freeze-from-image"` was in both of those tuples and is NOT here, because no producer
#: has ever written it: `freeze_from_image.py` emits `"authors-dockerfile"` or `"adopt"`,
#: full stop. A dead label sitting in a routing tuple reads as a supported method — three
#: live methods were being maintained as four.
AUTHORS_METHODS = ("authors-dockerfile",)


def extract_recipe(draft: Optional[dict], *, name: str, conda_deps: list[str],
                   primary_tools: list[str], version: str = "",
                   platform: str = "linux/amd64", accelerator: Optional[dict] = None,
                   license_gated: bool = False, licenses: Optional[list[str]] = None,
                   redistributable: bool = True, content_digest: str = "",
                   conda_lock: Optional[dict[str, str]] = None,
                   apt_snapshot: str = "",
                   build_method: str = "container-native-build",
                   adopt_image: str = "",
                   dockerfile_source: Optional[dict] = None,
                   built_commands: Optional[list[dict]] = None) -> dict[str, Any]:
    """Assemble the self-contained recipe from a draft + the freeze args. Carries the
    install_steps subset (which holds every non-conda install_method, incl. synthesized
    commands + provenance + commit) verbatim, so a rebuild needs nothing else.

    `conda_lock` is the per-file engine lock dict (e.g. {pixi.toml: ..., pixi.lock: ...}
    captured during freeze). When present, a replay skips the conda/pip SOLVE and
    materializes the env from these exact bytes — eliminates the bioconda-moves-and-
    your-rebuild-drifts class of failure. The lock is the URL+sha256 list pixi
    generates; same lock → identical packages across time and machines.

    `build_method` selects how the env was produced, so the recipe REPRESENTS every
    freeze scenario (not just container-native builds):
      • container-native-build — conda/pip + the non-conda tiers, baked into an image.
      • adopt                   — a published BioContainer pulled BY DIGEST (`adopt_image`).
      • authors-dockerfile      — the tool's OWN Dockerfile at a pinned source commit
                                  (`dockerfile_source`: {repo, commit, tag, dockerfile?}).
    A recipe ALWAYS exists regardless of path; the human renderer branches on this."""
    draft = draft or {}
    return {
        "recipe_version": RECIPE_VERSION,
        "name": name,
        "version": version,
        "platform": platform,
        "build_method": build_method,
        "primary_tools": list(primary_tools or []),
        "conda_deps": list(conda_deps or []),
        "conda_lock": dict(conda_lock) if conda_lock else {},
        # snapshot.debian.org timestamp pinning the apt layer. Empty for pre-
        # Phase-2 recipes; when set, replay points apt at the same snapshot URL.
        "apt_snapshot": apt_snapshot or "",
        # install_steps carry the non-conda install_methods that build_env_image replays.
        "install_steps": [s for s in (draft.get("install_steps") or []) if isinstance(s, dict)],
        # THE TRANSCRIPT — the literal commands that built the shipped image, captured
        # from the BuildResult's longtail_steps (ContainerBuild.run_install records the
        # exact string it exec'd, engine wrapper and all, and emit_dockerfile bakes that
        # same string as `RUN <command>`).
        #
        # WHY IT IS RECORDED RATHER THAN RE-DERIVED. Re-authoring these lines from
        # `install_method` in a per-tier renderer makes a SECOND author of a string
        # `install_commands` already wrote, and the two drift — handing the reader
        # flag spellings and wrapper lines nobody ever ran. The producer captures and
        # the reader does not scrape (the standing rule that also produced
        # ShippedBinary). Recording beats recomputing for a second reason: replaying
        # `_map_install` at render time answers "what would we run TODAY", which is a
        # different question from "what built THIS image" whenever a generator has
        # changed since. Absent (adopt / authors-dockerfile / a recipe without the
        # field) is a real state the renderer must state rather than paper over with
        # a paraphrase.
        "built_commands": [
            {k: v for k, v in s.items() if k in ("tool", "purpose", "command", "evidence")}
            for s in (built_commands or []) if isinstance(s, dict) and s.get("command")
        ],
        # adopt: the biocontainer ref (image@sha256:…) the recipe pulls by digest.
        "adopt_image": adopt_image or "",
        # authors-dockerfile: the pinned source the Dockerfile builds against.
        "dockerfile_source": dict(dockerfile_source) if dockerfile_source else {},
        "accelerator": accelerator,
        "license_gated": bool(license_gated),
        "licenses": list(licenses or []),
        "redistributable": bool(redistributable),
        "content_digest": content_digest,
    }


def rebuild_from_recipe(recipe: dict, *, engine=None,
                        build_fn: Optional[Callable[..., dict]] = None) -> dict[str, Any]:
    """Rebuild an env from its recipe ALONE (no draft/agent) and compare content_digest
    to the recorded one. `build_fn` defaults to env_freeze.build_env_image (injected so
    the match-logic is unit-testable without Docker). See the module docstring for what
    a match does and does NOT prove."""
    if build_fn is None:
        from agent.skills.env_freeze import build_env_image as build_fn  # noqa: N806
    spec = {"install_steps": recipe.get("install_steps", [])}
    br = build_fn(
        spec, name=recipe["name"], version=recipe.get("version", ""),
        conda_deps=recipe.get("conda_deps", []),
        primary_tools=recipe.get("primary_tools", []),
        platform=recipe.get("platform", "linux/amd64"),
        accelerator=recipe.get("accelerator"),
        license_gated=recipe.get("license_gated", False),
        licenses=recipe.get("licenses", []),
        redistributable=recipe.get("redistributable", True),
        engine=engine,
        # the prebaked lock (if the recipe carries one): replay materializes the env
        # from these exact bytes, no solve — no chance of bioconda drift.
        conda_lock_files=recipe.get("conda_lock") or None,
        # the captured snapshot.debian.org timestamp — replay's apt resolves to
        # the SAME bytes as the original freeze. Empty for pre-Phase-2 recipes
        # (which keep floating-apt behavior, backward compatible).
        apt_snapshot=recipe.get("apt_snapshot") or "",
    )
    expected = recipe.get("content_digest") or ""
    got = br.get("content_digest") or ""
    match = bool(expected) and got == expected
    # WHAT THE REPLAY ACTUALLY RE-EXECUTES. This said "conda layer re-solved → same
    # content_digest", of a replay that installs from the recipe's own pixi.lock with
    # `pixi install --locked`. See the module docstring: the lock is replayed, never
    # re-solved, so the conda set agrees by construction and its agreement evidences
    # nothing. `proves` is RETURNED to the caller, which is why this string mattered more
    # than the docstring that said the same thing.
    _locked = bool(recipe.get("conda_lock"))
    proves = ("COMPLETENESS (rebuilt from the recipe alone) + LOCAL DETERMINISM: every "
              "layer that is re-executed (apt snapshot, jar/binary/source/cargo/go tiers, "
              "image assembly) converged to the same content_digest."
              + (" The conda layer was REPLAYED from the recipe's pinned lock, not "
                 "re-solved, so that layer is identical by construction rather than by "
                 "demonstration." if _locked else
                 " This recipe carries no lock, so the conda layer WAS solved afresh and "
                 "its agreement is part of what converged.")
              + " NOT cross-machine or independent-party reproducibility — run this "
                "elsewhere (CI / a colleague) for that.")
    if bool(br.get("success")) and match:
        return proven(
            "env_recipe.reproduced",
            success=True,
            rebuilt_content_digest=got,
            expected_content_digest=expected,
            content_digest_match=match,
            proves=proves,
            build=br,
        )
    return broke(
        "env_recipe.not_reproduced",
        success=bool(br.get("success")),
        rebuilt_content_digest=got,
        expected_content_digest=expected,
        content_digest_match=match,
        proves=proves,
        build=br,
    )
