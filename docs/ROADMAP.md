# Roadmap — what is deliberately NOT in v1

This file is the canonical log of features **decided and deferred** to a later version.
Each entry records the ruling and, where one exists, the design already agreed — so a
future version starts from the decision, not from a re-derivation. v1 scope itself is
defined by the end-to-end claim: *name a tool → frozen validated image → sealed workflow
→ staged to the cluster → runs there → outputs verified at the locus → sealed record* —
judged on the three artifacts a human reads (the ENV report, the build recipe, the RUN
dashboard).

An item here is **out of v1 by decision, not by accident**. Do not build one "helpfully";
re-scope it first.

---

## Deferred features (user rulings)

### nf-core / Snakemake pipeline EXECUTION
Ruled out of v1 2026-08-06: *"out of scope for v1 … i don't actually ever use these.
will want later though."* **Detection stays in v1** — `artifact_is_a_workflow` refuses to
synthesize an install from a workflow repo, and the refusal names the pipeline and why we
will not stamp a false green on the launcher.

The design for later is already agreed in principle: **a pipeline is a manifest of N
envs, not one env.** Layer 1 freezes the SET (resolve each process's declared container,
adopt each by digest, validate each, report every tool+version+digest); Layer 2 records
which ones really ran from the pipeline's own `software_versions.yml` (an observation,
not a declaration). Declared set is a superset (varies with `--aligner` etc.); report
both, distinguished. This preserves validated==shipped instead of stamping it on the
launcher. Do **not** wire up an engine-freeze route as a shortcut — freezing the Nextflow
engine and sealing would claim `validated_in_shipped_image` while every process ran in
images this system never saw.

### Long-read base-modification (5mC/m6A) methylation routing
A real user ask, parked. The pod5 test data shipped 2026-06-04; the methylation routing
follow-up was never built.

### Batch / parallel installs as a product promise
Dropped as a v1 promise 2026-07; the **foundation stays** (background detachment, store
locking — the locking was bought by detachment, not parallelism). Do not rip out the
foundation; do not re-promise parallelism without a new ruling.

### Publication / methods-section renderer
Explicitly not v1 — but the **information** must all be in the artifacts (a missing
captured version is a v1 defect; a missing methods-paragraph renderer is not). A later
`.py` script over sealed records is expected to be trivial if v1 captures everything.

### LLM-in-the-loop identity eval
The intent corpus carries rows deferred on an identity judgment no harness can grade
mechanically (is this package the tool the user *meant*?). They close when an
LLM-in-the-loop eval exists to grade the ride's judgment, or they stay declared. Not by
enumeration, and never by a tool-name table.

### New meters / new MCP tools
Standing exclusion for v1 ("ship, don't scaffold"). The intent grid, outcomes dashboard
and capability map are maintained as-is, not extended.

---

## Post-v1 structural programs (from the 2026-08 architecture review)

Adopted 2026-08-31 as standing input; each item gets resolved, scheduled post-v1, or
consciously waived — scope calls are the user's.

1. **Type records at construction, seam by seam.** Runtime constructs typed observations
   (`extra="forbid"`, no defaults — the `ShippedBinary` discipline); retire seal-walk
   clauses as types absorb them. Start with `pipeline_steps` (feeds I3/I7/I8). This is
   the root-cause fix for the five compensating control systems (seal-time walk,
   PATCHABLE_KEYS, core_data leaves, one-reading lint, render-parity tests). Parked until
   re-scoped.
2. **Make observation-vs-claim structural.** Append-only observation log (runtime-only
   writer) + agent-writable claims overlay; a record is a fold of the two. Kills the
   anchor-laundering defect class by construction rather than by fill-only rules.
3. **Schema-driven rendering.** Annotate model fields with display semantics; render
   generically. "The page shows what the seal gated on" becomes structural instead of
   being policed by ~145 parity tests over ~3,600 LOC of hand-per-field renderers.
4. **Scheduled real-bytes canary.** A nightly/weekly job running the docker-tier tests
   plus one end-to-end freeze→seal on a canary tool (+ apptainer smoke where available),
   so ecosystem drift (bioconda metadata, registries, apptainer versions) surfaces
   without a human driving a sea trial. CI infrastructure, not a meter.
5. **Shrink the served tool-surface *schemas* — never the menu.** The always-resident
   schema/docstring mass of the LOW_LEVEL tools can be served deferred/on-demand. Every
   tool stays POSITIONED and discoverable (`tool_surface.py` + its build-failing lint) —
   the 24-unmentioned-tools gap caused a real freeze bypass and must not be reopened.

---

## Known-open items that ride along (not features, recorded so they aren't lost)

- The perl tier cannot build an **XS module** in-image (conda-forge perl records a
  relative `cc` path; MakeMaker ignores sysroot overrides). Pure-Perl works.
- `freeze_from_image` refuses a **multi-arch index digest** (correct) but nothing
  resolves the per-arch child digest yet, so an authors' multi-arch image is unusable.
- `REFUSAL_REASONS` still lacks a *"a human must fetch this"* value for
  registration-gated tools (the resolver now has world-describing vocabulary for
  workflows only).
- The accelerator probe's reach: loader-reachable `libcud*.so` layouts (or a
  *bundled-runtime* state for `accelerator`) — the docker-gated half of
  `tests/test_accelerator_probe_reach.py` fails loudly the moment the probe is widened.
