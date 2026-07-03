#!/usr/bin/env python3
"""
seaworthy_scope — the single source of truth for the Seaworthy v1 milestone.

Seaworthiness is NOT 100% coverage of all 452 terminals. It is certification of
the LOAD-BEARING surface: the terminals an autonomous subagent's trust depends
on. A subagent trusts the outcome tag, so the load-bearing surface is:

  1. every `proven`        — a false green makes the agent proceed on a lie
  2. every seal invariant  — the honesty gates that EXIST to catch false-greens
                             (source == 'invariant': I0/I3/I6/I7/I8/I12/I13,
                              BUILT/VALIDATED_IN_IMAGE/POLICY_CLEAN/ADOPTED…)
  3. the named FIREWALLS    — helper-tagged gates that, if they fail to fire,
                             ship a bad artifact (supply-chain / validated==shipped)

Everything else (ordinary error propagation) is important but not trust-critical
in the false-green sense — a `broke` that surfaces a subprocess failure is honest
by construction.

The surface splits into two ARENAS:
  - LOCAL  — certifiable on this machine (install/build/freeze/validate/seal).
             This is Seaworthy v1.
  - HPC    — needs a real cluster to certify honestly (transfer/cluster/submit).
             Deferred to Seaworthy v2 (see [[feedback-no-cheeky-head-node-testing]]).

A load-bearing terminal is CERTIFIED when it is `verified` (executed by a test
AND named by its code) — for a firewall/gate that means an adversarial test
actually triggered its reject branch; for a `proven` it means a test exercises
and names the success. The v1 meter = certified / total over the LOCAL surface.
"""
from __future__ import annotations

# Subsystems that can only be honestly certified against a real cluster.
HPC_SUBSYSTEMS = {
    "transfer", "cluster", "run_cluster", "stage", "submit",
    "submit_workflow", "modules", "globus",
}

# Helper-tagged firewalls (not invariant-source) whose FAILURE-TO-FIRE would
# ship a bad artifact. Kept explicit on purpose — these are the honesty
# contract's teeth, worth naming. Add here when a new firewall lands.
FIREWALL_CODES = {
    "env_manager.binary_sha256_mismatch",       # supply-chain: wrong/tampered asset at INSTALL
    "build.binary_integrity_mismatch",           # supply-chain: asset mutated INSTALL→SHIP (F2)
    "freeze.adopt_honesty",                      # adopt only a pure-conda env
    "freeze.recipe_not_reproduced",              # reproducibility check
    "freeze.recipe_invalid",
    "freeze.recipe_load_failed",
    "container_build.validation_in_image_failed",  # validated == shipped
    "env_build.verification_in_image_failed",       # validated == shipped
}


def subsystem(code: str | None) -> str:
    return code.split(".", 1)[0] if code else "«untagged»"


def is_load_bearing(entry: dict) -> bool:
    return (entry.get("outcome") == "proven"
            or entry.get("source") == "invariant"
            or entry.get("code") in FIREWALL_CODES)


def arena(entry: dict) -> str:
    """'local' (Seaworthy v1) or 'hpc' (v2, needs a cluster)."""
    return "hpc" if subsystem(entry.get("code")) in HPC_SUBSYSTEMS else "local"


def is_certified(entry: dict, overlay: dict | None) -> bool:
    """A load-bearing terminal is certified when it is verified: its line was
    executed by the suite AND a test names its code. `overlay` is
    terminal_coverage.json's by_where map (None ⇒ nothing certified — coverage
    not measured)."""
    if overlay is None:
        return False
    return overlay.get(entry["where"]) is True and bool(entry.get("named_in_test"))


def summarize(ledger: list[dict], overlay: dict | None) -> dict:
    """Compute the Seaworthy meter from a ledger + coverage overlay."""
    lb = [e for e in ledger if is_load_bearing(e)]
    local = [e for e in lb if arena(e) == "local"]
    hpc = [e for e in lb if arena(e) == "hpc"]
    lc = sum(1 for e in local if is_certified(e, overlay))
    hc = sum(1 for e in hpc if is_certified(e, overlay))
    return {
        "load_bearing": len(lb),
        "local_total": len(local), "local_certified": lc,
        "hpc_total": len(hpc), "hpc_certified": hc,
        "local_pct": round(100 * lc / len(local)) if local else 0,
        "seaworthy_v1": bool(local) and lc == len(local),
    }
