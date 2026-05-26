"""
env_vendor — opt-in HEAVY mirror sidecar (the "audit-proof" mode).

STATUS: design stub. Not wired into freeze yet. This module documents what the
heavy mirror would do, so adding it later is a contained change instead of a
re-design. The default link-rot protection is `_swh_clone` in container_build
(automatic at rebuild time for git sources, no freeze-time cost) — that's
sufficient for the common case. The HEAVY mirror is for hospital / GxP /
audit-proof scenarios where you want zero external dependencies at rebuild time,
including for release binaries and JARs that Software Heritage cannot archive.

WHAT THE HEAVY MIRROR ADDS, when enabled (e.g. via `freeze(..., mode="audit_proof")`):

  1. RELEASE BINARIES — at freeze, every `install_release_binary` URL is fetched
     and stored next to the recipe in `{name}.vendor/binaries/<sha256>.<basename>`.
     The recipe's install command falls back to the local copy on URL 404.

  2. JARS — same pattern. `install_jar_tool` URLs cached to
     `{name}.vendor/jars/<sha256>.jar`.

  3. SOURCE TARBALLS (optional, on top of SWH) — `git archive` of each pinned
     commit to `{name}.vendor/sources/<commit>.tar`. Useful when SWH may also be
     down (the belt-AND-suspenders case for GxP).

  4. SIDECAR TARBALL — everything bundled as `{name}.vendor.tar.gz` next to the
     recipe.yaml. Self-contained: `verify_env_recipe` with the sidecar present
     uses LOCAL bytes for every blob, no network at all.

WHEN TO TURN IT ON:

  - Hospital / clinical / GxP environments where rebuild must work offline or
    where upstream URL availability cannot be assumed across the artifact's
    intended lifetime (years to decades).
  - Regulatory submissions where the auditor wants a self-contained artifact set
    that proves rebuild without depending on any third-party service surviving.

WHEN IT'S OVERKILL:

  - Research re-analysis on short horizons. SWH fallback (already on) covers
    99%+ of practical link rot.
  - Large envs (200+MB per env). The sidecar size scales with binary count.

COST ESTIMATE:

  - Freeze: adds 30s–10min depending on env size (curl + tar).
  - Disk: 10MB–GB per env.
  - Rebuild (no upstream): zero network — the sidecar IS the source of truth.

INTEGRATION POINTS WHEN WIRING THIS UP LATER:

  - `mcp_server.freeze` gains a `mode: "default"|"audit_proof"` arg.
  - `env_vendor.materialize(install_steps, dest)` walks install_steps, fetches
    each binary/jar/source, writes them to dest, returns the manifest.
  - The recipe gains a `vendor_manifest` field referencing the sidecar.
  - The install commands (release_binary / jar / source) check for the local
    mirror first (env var like `BIOINF_VENDOR_DIR` pointing into the sidecar
    when verify_env_recipe extracts it), fall back to upstream.

CONTRACT THIS MODULE WOULD UPHOLD:

  - Every cached blob is sha256-verified before use (anchor preserved).
  - Sidecar is content-addressed (filenames include the sha256) → tamper-evident.
  - Pure-data: no agent-authored content; everything is captured at freeze.
"""

from __future__ import annotations


def materialize(install_steps: list[dict], dest_dir: str) -> dict:
    """STUB — not yet implemented. See module docstring for the contract."""
    return {"success": False, "stage": "vendor_materialize",
            "reason": "env_vendor.materialize is a future heavy-mode feature; "
                      "default freeze uses SWH fallback for git sources (see "
                      "container_build._SWH_CLONE_SCRIPT). Enable HEAVY mirror "
                      "via mode='audit_proof' once this stub is implemented."}
