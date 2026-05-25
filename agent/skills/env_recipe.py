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
  • LOCAL DETERMINISM / CONVERGENCE — replaying it HERE yields the same content_digest.
    The conda layer is RE-SOLVED (not cheated from a cached lock), so a match means the
    solve converged — the strongest same-machine signal for "two runs → same result".
It does NOT prove cross-machine reproducibility (different base cache / network bytes /
docker) or independent-party tamper-evidence — those are this SAME rebuild run ELSEWHERE
(CI, a colleague) + signing. The recipe ENABLES them; this verifies the necessary local
conditions. Pure assembly here; the rebuild's I/O is injected for testing.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

RECIPE_VERSION = 1


def extract_recipe(draft: Optional[dict], *, name: str, conda_deps: list[str],
                   primary_tools: list[str], version: str = "",
                   platform: str = "linux/amd64", accelerator: Optional[dict] = None,
                   license_gated: bool = False, licenses: Optional[list[str]] = None,
                   redistributable: bool = True, content_digest: str = "") -> dict[str, Any]:
    """Assemble the self-contained recipe from a draft + the freeze args. Carries the
    install_steps subset (which holds every non-conda install_method, incl. synthesized
    commands + provenance + commit) verbatim, so a rebuild needs nothing else."""
    draft = draft or {}
    return {
        "recipe_version": RECIPE_VERSION,
        "name": name,
        "version": version,
        "platform": platform,
        "primary_tools": list(primary_tools or []),
        "conda_deps": list(conda_deps or []),
        # install_steps carry the non-conda install_methods that build_env_image replays.
        "install_steps": [s for s in (draft.get("install_steps") or []) if isinstance(s, dict)],
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
    )
    expected = recipe.get("content_digest") or ""
    got = br.get("content_digest") or ""
    return {
        "success": bool(br.get("success")),
        "rebuilt_content_digest": got,
        "expected_content_digest": expected,
        "content_digest_match": bool(expected) and got == expected,
        "proves": "COMPLETENESS (rebuilt from the recipe alone) + LOCAL DETERMINISM "
                  "(conda layer re-solved → same content_digest). NOT cross-machine or "
                  "independent-party reproducibility — run this elsewhere (CI / a colleague) "
                  "for that.",
        "build": br,
    }
