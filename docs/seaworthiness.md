# Seaworthiness — the milestone for autonomous use

The big flag we work toward. The system is **seaworthy** when an autonomous
subagent can drive it with no human watching and never be misled — no false
green, no uncaught crash, no silent failure. This doc defines that precisely,
makes it measurable, and splits it into what we can certify locally now (v1) vs
what needs a real cluster (v2).

Canonical over [stress_test_campaign.md](stress_test_campaign.md) (the tool-tier
exercise is now just one input to this). Findings log: [stress_findings.md](stress_findings.md).

## Why not "100% coverage" or "no known bugs"
- *100% coverage* is the wrong flag — most of the 452 terminals don't bear on
  trust, and chasing all of them is boil-the-ocean.
- *No known bugs* can never be declared done.

A ship isn't certified because every rivet was tested. It passes a **sea trial**
against defined criteria on its load-bearing systems. Same here.

## The load-bearing surface (what actually must hold)
A subagent trusts the **outcome tag**. So the surface its trust depends on is
(auto-derived in `scripts/seaworthy_scope.py`, single source of truth):

1. **every `proven`** — a false green makes the agent proceed on a lie
2. **every seal invariant** (`source == invariant`: I0/I3/I6/I7/I8/I12/I13,
   BUILT/VALIDATED_IN_IMAGE/POLICY_CLEAN/ADOPTED…) — the gates that *exist* to
   catch false-greens
3. **the named firewalls** (`FIREWALL_CODES`) — helper-tagged gates that, if
   they fail to fire, ship a bad artifact (sha256, validated==shipped, …)

Everything else is ordinary error propagation — important, but honest by
construction (a `broke` that surfaces a subprocess failure isn't a trust risk).

**Current scope (from the ledger):**

| | terminals | certified today | to certify |
|---|---|---|---|
| **Load-bearing total** | 136 | 34 | 102 |
| **LOCAL (Seaworthy v1)** | **124** | **34** | **90** |
| HPC (Seaworthy v2, needs a cluster) | 12 | 0 | 12 |

## The five certification criteria
Seaworthy v1 = **all** of these green on the LOCAL load-bearing surface:

- **C1 — No false-greens.** Every honesty-critical `proven` has a test that
  tries to fake the green and fails to. (A lying green is the one catastrophic
  outcome under full-auto.)
- **C2 — Every firewall fires.** Each honesty gate has an *adversarial* test
  proving it rejects the attack (e.g. the sha256 mismatch test — certified #1).
- **C3 — Zero vanished.** Already 0, ratchet-locked. Stays 0.
- **C4 — No uncaught crashes.** Every primitive returns a *tagged outcome* even
  on hostile input (bad paths, missing envs, malformed files) — never an
  unhandled exception an agent can't interpret.
- **C5 — Legible assurance.** Full-assurance and reduced-assurance are
  distinguishable (`degraded`, not `proven`). See finding F1.

## Certified = verified
A load-bearing terminal is **certified** when it is `verified` (executed by a
test AND named by its code). For a firewall/gate, verified means an adversarial
test actually triggered its reject branch. For a `proven`, a test exercises and
names the success. The meter counts certified / load-bearing over the LOCAL
surface — visible as the ⚓ banner + the "⚓ show only uncertified load-bearing"
filter on `docs/outcomes_dashboard.html`.

## The sea trial (the final gate)
Once C1–C5 are green, a **subagent drives a real workflow end-to-end**
(start → install → freeze → seal) with no human in the loop, and it either
completes *honestly* or fails *legibly* — no false-green, no crash, no silent
hang. Passing the trial = **Seaworthy v1**.

## The v1 worklist (90 local load-bearing terminals to certify)
Ordered by concentration (use the dashboard's ⚓ filter for the live list):

| subsystem | to certify | how we certify |
|---|---|---|
| validate | 22 | feed each validator a malformed/empty file → assert it rejects, not passes |
| env_manager | 21 | attack each install firewall (bad sha, missing env, failed subprocess) |
| container_build | 15 | force a build/validate failure → assert `validation_in_image_failed` |
| freeze | 8 | adopt-honesty, recipe-reproduction, cache-hit integrity |
| env_build | 4 | declare/verify-in-image failure paths |
| test_runner / core_test_data | 7 | download/build failure + genome-materialize |
| the rest (I8/I6/I13/service/…) | ~13 | one adversarial test each |

## Working order
1. **C2 firewalls first** — the scariest under full-auto (sha256 done).
2. **C1 false-green attacks** on the honesty-critical provens (freeze.built,
   seal.sealed, validated_in_image, binary_installed).
3. **C5** — land the `degraded` assurance distinction (F1) across tiers.
4. **C4** — a small fuzz harness: drive every primitive with hostile input,
   assert a tag comes back (never an exception).
5. **Sea trial.**
6. **Seaworthy v2** — repeat C1–C5 on the 12 HPC terminals, user-driven on a
   real cluster ([[feedback-no-cheeky-head-node-testing]]).

## How we'll know we're there
The dashboard says **⚓ SEAWORTHY v1: 124/124 · SEA TRIAL READY**, C1–C5 all
green, and the trial passes. Not before, not "feels solid." Today: **34/124**.
