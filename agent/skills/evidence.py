"""
Evidence strategies — named, reusable presence proofs for the install re-spine.

Each strategy answers ONE question the agent cannot fake: it queries the env's
own registry or filesystem rather than trusting agent-supplied stdout. The
verify() gate composes these instead of re-implementing presence checks, and
(as the re-spine proceeds) every install tier — conda, pip, R, jar, source,
release-binary, container — picks the strategy that proves *its* category
worked. New tier ⇒ new strategy here, not a new bespoke verify path.

Strategy contract:  fn(em, env_name, name) -> Evidence
    Evidence = {"strategy": str, "anchored": bool, "detail": object | None}

`em` is an EnvManager, used only for `run_in_env`. Keeping these as free
functions over that tiny interface is what stops method-proliferation: the
knowledge of "how do you prove an R library is present" lives in exactly one
place and is reused everywhere.

This module is a behavior-preserving extraction of the anchors that previously
lived inline in EnvManager.verify / EnvManager._package_in_registry.
"""

from __future__ import annotations

import json as _json
import shlex as _shlex
from typing import Any, Callable


def cli_which(em, env_name: str, name: str) -> dict[str, Any]:
    """CLI presence: `which {name}` resolves to a path inside the env."""
    res = em.run_in_env(env_name, f"which {name} 2>/dev/null", timeout=10)
    out = (res.get("stdout") or "").strip()
    anchored = res.get("returncode") == 0 and bool(out)
    return {"strategy": "cli_which", "anchored": anchored, "detail": out or None}


def conda_registry(em, env_name: str, name: str) -> dict[str, Any]:
    """Conda registry presence via `conda list --json`, with an EXACT
    (case-insensitive) name match — conda's name arg is a regex, so a substring
    like "numpy" would otherwise surface "numpy-base"."""
    q = _shlex.quote(name)
    conda = em.run_in_env(env_name, f'conda list -p "$CONDA_PREFIX" --json {q}', timeout=30)
    anchored = False
    if conda.get("returncode") == 0:
        try:
            data = _json.loads(conda.get("stdout") or "[]")
            anchored = isinstance(data, list) and any(
                isinstance(e, dict) and e.get("name", "").lower() == name.lower()
                for e in data
            )
        except Exception:
            anchored = False
    return {"strategy": "conda_registry", "anchored": anchored, "detail": name if anchored else None}


def pip_show(em, env_name: str, name: str) -> dict[str, Any]:
    """Pip registry presence: `pip show` exits 0 only if the dist is installed."""
    q = _shlex.quote(name)
    pip = em.run_in_env(env_name, f"pip show {q}", timeout=30)
    anchored = pip.get("returncode") == 0 and bool((pip.get("stdout") or "").strip())
    return {"strategy": "pip_show", "anchored": anchored, "detail": name if anchored else None}


def r_namespace(em, env_name: str, name: str) -> dict[str, Any]:
    """R library presence: requireNamespace() succeeds. Packages installed via
    install_r_package (CRAN / Bioconductor / GitHub) live in
    $CONDA_PREFIX/lib/R/library and are tracked by NEITHER conda NOR pip, so
    this is their only registry anchor. Probes the name plus its conda-prefix-
    stripped forms (r-ape→ape, bioconductor-multtest→multtest)."""
    r_names = {name}
    for prefix in ("r-", "bioconductor-"):
        if name.lower().startswith(prefix):
            r_names.add(name[len(prefix):])
    checks = " || ".join(f"requireNamespace('{n}',quietly=TRUE)" for n in sorted(r_names))
    rprobe = em.run_in_env(
        env_name, f'Rscript -e "quit(status=if({checks}) 0 else 1)"', timeout=60
    )
    anchored = rprobe.get("returncode") == 0
    return {"strategy": "r_namespace", "anchored": anchored,
            "detail": sorted(r_names) if anchored else None}


def perl_module_load(em, env_name: str, module: str) -> dict[str, Any]:
    """Perl module presence: `perl -M{module} -e1` loads cleanly. The anchor for
    the Perl/CPAN tier (Ensembl VEP, BioPerl) — cpanm-installed modules live in
    the env's perl site lib and are tracked by NEITHER conda nor pip, so this is
    their registry anchor. `module` is the Perl package name (Bio::DB::HTS), not
    a conda-style name. Module names are restricted to [A-Za-z0-9_:] (shell-safe),
    and a name outside that set is rejected rather than run."""
    import re as _re
    if not _re.match(r"^[A-Za-z0-9_:]+$", module or ""):
        return {"strategy": "perl_module_load", "anchored": False,
                "detail": f"invalid perl module name: {module!r}"}
    res = em.run_in_env(env_name, f"perl -M{module} -e1", timeout=60)
    anchored = res.get("returncode") == 0
    return {"strategy": "perl_module_load", "anchored": anchored,
            "detail": module if anchored else None}


def file_present(em, env_name: str, path: str, sha256: str | None = None) -> dict[str, Any]:
    """Filesystem presence: `path` exists as a file, and (if `sha256` is given)
    its bytes hash to it. The anchor for the release-binary tier (I14) and any
    on-disk artifact — a recorded URL is a claim; a present file whose bytes
    match the recorded hash is proof the agent cannot fabricate.

    `em` is unused (kept for a uniform call signature); the check is pure
    filesystem so it needs no env.
    """
    import hashlib
    from pathlib import Path as _Path

    p = _Path(path)
    if not p.is_file():
        return {"strategy": "file_present", "anchored": False,
                "detail": f"missing: {path}"}
    if sha256:
        h = hashlib.sha256()
        with p.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                h.update(chunk)
        digest = h.hexdigest()
        if digest.lower() != sha256.lower():
            return {"strategy": "file_present", "anchored": False,
                    "detail": f"sha256 mismatch: recorded {sha256[:12]}…, on-disk {digest[:12]}…"}
        return {"strategy": "file_present", "anchored": True, "detail": f"{path} (sha256 ok)"}
    return {"strategy": "file_present", "anchored": True, "detail": path}


def registry_anchor(em, env_name: str, name: str) -> dict[str, Any]:
    """conda OR pip OR R-library presence — the composite backing
    EnvManager._package_in_registry. Short-circuits on the first hit, in the
    same order the inline code used (conda → pip → R)."""
    for fn in (conda_registry, pip_show, r_namespace):
        ev = fn(em, env_name, name)
        if ev["anchored"]:
            return {"strategy": f"registry_anchor:{ev['strategy']}",
                    "anchored": True, "detail": ev["detail"]}
    return {"strategy": "registry_anchor", "anchored": False, "detail": None}


def presence_anchor(em, env_name: str, name: str) -> dict[str, Any]:
    """`which` OR registry — the composite backing verify()'s present-anchor
    gate (the thing that kills the library-only echo cheat)."""
    w = cli_which(em, env_name, name)
    if w["anchored"]:
        return w
    return registry_anchor(em, env_name, name)


# The registry. Composing tiers look strategies up here by name; tests assert
# the set of available anchors so a dropped strategy is caught.
STRATEGIES: dict[str, Callable[..., dict[str, Any]]] = {
    "cli_which":        cli_which,
    "conda_registry":   conda_registry,
    "pip_show":         pip_show,
    "r_namespace":      r_namespace,
    "perl_module_load": perl_module_load,
    "file_present":     file_present,
    "registry_anchor":  registry_anchor,
    "presence_anchor":  presence_anchor,
}
