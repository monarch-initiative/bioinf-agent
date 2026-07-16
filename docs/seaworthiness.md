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

**Current scope: read it off the dashboard — `docs/outcomes_dashboard.html`, ⚓ banner.**

There are deliberately no numbers in this file. There used to be a table here, and a
second copy in a project memory, and both drifted from `scripts/seaworthy_scope.py`,
which computes the meter and was right the whole time (audit 2026-07-16: this file said
34/124, the memory said 77/125, the generator said 87/142). Three hand-copied snapshots,
three different answers to a question that has one computed answer. A number typed into
prose is a number that rots — the same duplicated-truth failure this project's whole
honesty contract exists to prevent, committed against ourselves.

Regenerate with `scripts/extract_outcomes.py` + `scripts/measure_terminal_coverage.py`
+ `scripts/render_outcomes_dashboard.py`.

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

## Certified = verified — and what that does NOT mean
A load-bearing terminal is **certified** when it is `verified`: its line was executed
by the suite AND some test names its outcome code. The meter counts certified /
load-bearing over the LOCAL surface — the ⚓ banner + the "⚓ show only uncertified
load-bearing" filter on `docs/outcomes_dashboard.html`.

**Read that definition literally, because it is weaker than it sounds.** This section
used to claim "for a firewall/gate, verified means an adversarial test actually
triggered its reject branch." **Nothing checks that.** The two halves are computed by
independent whole-corpus scans with no join key: "executed" comes from one coverage run
over the whole suite, "named" from a raw-text grep of `tests/**` that counts comments and
docstrings. Nothing requires the naming test to be the executing test, or the executing
test to have taken the reject branch.

So the meter measures **execution, not rejection** — and the entire honesty contract
lives in the reject branch. Coverage will paint `except Exception: pass` bright green;
that is precisely how the reliability gate sat dead for five commits under 1251 green
tests. Certification is a floor ("this line is reachable and someone named it"), never
proof that a gate fires. Only an adversarial test that reintroduces the defect proves
that, and only a human reading it can confirm it did.

## The sea trial (the final gate)
Once C1–C5 are green, a **subagent drives a real workflow end-to-end**
(start → install → freeze → seal) with no human in the loop, and it either
completes *honestly* or fails *legibly* — no false-green, no crash, no silent
hang. Passing the trial = **Seaworthy v1**.

## The v1 worklist
**Get the live list from the dashboard's ⚓ filter** — a hand-copied worklist is the
thing this file just got burned by. Certify by subsystem: feed each validator a
malformed file and assert it rejects; attack each install firewall (bad sha, missing
env, failed subprocess); force a build/validate failure and assert
`validation_in_image_failed`; and so on — one adversarial test each.

**But treat the meter as secondary.** The 2026-07-16 audit found 10 gates that were
live in code and absent in effect, and **not one** would have been caught by certifying
another terminal — several were on lines coverage already painted green. Every one was
found by driving the real surface and reading the real artifacts. Grinding the meter is
the cheaper-feeling work; it is not the work that finds these.

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
The dashboard's ⚓ banner reads **SEA TRIAL READY** (it computes that itself — see
`scripts/seaworthy_scope.py:summarize`), and the trial passes. Not before, not "feels
solid," and not because this file says so.

**C1–C5 have no evaluator.** They are prose intent, not a computed gate: `grep` for
them across `scripts/` and `agent/` returns nothing but this document. Only C3 (zero
vanished) is mechanically ratcheted. That gap is how a memory came to record "C1–C5
GREEN" with nothing able to contradict it. Either the criteria get an evaluator or they
stay honestly labelled as intent — but nobody should ever again report them as a status.
