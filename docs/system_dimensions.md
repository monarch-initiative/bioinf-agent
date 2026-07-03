# System dimensions — the seaworthiness burn-down

The finish line, made visible. NOT a tool list (infinite) — the finite set of SYSTEM
DIMENSIONS an arbitrary tool can exercise. When every row is green, arbitrary tools
route through proven paths, and anything novel falls to synthesis + the honesty
contract (works, or fails legibly). That combination = seaworthy.

Each probe either turns a row green (fixes real defects, permanently) or is wasted.
Method: probe a real tool that exercises the row → fix what breaks → lock with a test
→ mark green. Grounded in truth, not coverage points.

## Local dimensions (agent can harden autonomously)

| # | Dimension | Vehicle | Status | Notes |
|---|-----------|---------|--------|-------|
| 1 | Install tiers: conda/pip/R/jar/binary/cargo/go/source | many | ✅ | exercised across the suite |
| 2 | Discovery + routing (repo-only, auto-chain) | GAPIT | ✅ | github discovery + auto-adopt + r_github tier |
| 3 | Validation honesty — R (import ≠ works) | GAPIT | ✅ | `functional_check` → freeze proves RAN (e5bbbe9) |
| 3b | Validation honesty — pip/Python | (generalized) | ✅ | install_pip_package gained `functional_check` → freeze proves RAN (mirrors R); Talos will use it |
| 4 | Name collisions (same-name, different tool) | talos/cellranger | 🟨 | **Repo-supplied case PROVEN**: `talos`+repo+`language=python` → cross-namespace guard rejects the wrong PyPI `talos` (Keras tuner) and routes to synthesis, correct+legible. Bare-name silent (fuzzy, deliberately not rabbit-holed). |
| 5 | Reference data / caches (big DB, indexed) | VEP | ✅ | `download_reference_database` (GTF) + bgzip/tabix + VEP `--gtf`/`--fasta` custom mode → real annotations verified. Fixed validator false-negative on long comment headers (VEP `--tab` 30 `##` lines) |
| 6 | Perl plugins / CPAN tier | VEP plugins | ✅ | `install_perl_package` (Text::CSV) + VEP `--plugin` load proven. Fixed: legible error when cpanm prerequisite missing |
| 7 | Auxiliary services (redis/postgres/spark) | Redis | ✅ | full lifecycle proven (start→health-poll→I10 record→real round-trip→verify→stop, + failure path). **Fixed the headline: I10 (service health) was advertised but enforced NOWHERE → seal would bless a workflow whose service never came up. Restored as a Layer-2 invariant.** Also fixed a stop_command pid-file leak |
| 8 | Multi-component pipelines (chained steps) | bwa→samtools→bcftools | ✅ | **Multi-step chaining PROVEN**: freeze once → 2 chained container steps (step2's BAM input IS step1's output) → seal green. I8 composition-coherence + lineage held across the chain (first seal correctly REFUSED for undeclared externals, then passed); `validated_in_shipped_image: true` (both steps digest-matched). Fixed: `depends_on` never materialized (finalize retired) + freeze-runner lingered ~9.5 min after result written. Talos itself is a cloud-native Hail pipeline (GCP) — its collision+routing are proven (row 4); a full host Hail install is a tool yak-shave, not a system dimension. |

### Retired-invariant audit (closed) — the I5/I9/I10 runtime-external family

The I10 restoration (row 7) raised the question: did any OTHER respine-retired
invariant fall through the same crack? Audited the full retired set
(I1/I2/I5/I9/I10/I11/I12/I13/I14). The rule that decides it: **is the artifact
baked INTO the ship image, or mounted/run OUTSIDE it at runtime?**

- Baked in → `VALIDATED_IN_IMAGE` genuinely covers it: **I1/I2** (tool presence/verify),
  **I11** (source rebuilt at pinned commit), **I14** (binary re-fetched + sha-anchored
  in the build). Correctly retired.
- Policy metadata → relocated to `POLICY_CLEAN`: **I12** (accelerator), **I13** (license).
  Still enforced.
- **Runtime-external** (lives outside the image, so install==ship CANNOT cover it) →
  must be restored at Layer 2: **I9** authored artifacts (restored earlier as
  `I8.authored_artifact_*`), **I10** service health (row 7), and **I5** reference-DB
  availability — **restored now**. `I8.composition_coherence` had been trusting a
  ref-DB's `local_path` as an input source without checking the file was present;
  `_check_reference_database_availability` now enforces existence + non-empty + size
  match + capped sha256 drift (mirrors the authored-artifact re-anchor; respects that
  ref-DBs can be 100s of GB / directories). 8 certification tests, mutation-verified.

**Audit conclusion: all three runtime-external invariants are now restored; no others fell through.**

### VEP probe — findings NOT yet fixed (logged, judged not clean/generalizable)
- **Silent-wrong GTF prep** (highest hazard): a mis-prepped GTF (`awk OFS` rewriting the attribute column, sort order, `chr22` vs `22`) → VEP emits **all-intergenic output, rc=0** = a silent false success. The honest fix is *functional* output validation (assert real annotations, not just a valid file) — domain-specific, NOT a new VEP-prep primitive (would be tool-specific scaffolding). Same principle as import≠works, at the output level.
- **Apple-Silicon `perl-db_file` breaks VEP host-run** (`dyld: missing symbol`): a platform/env issue, not our code. Confirms the **container-native freeze (linux-amd64) is the reliable path** for Perl-XS-heavy tools — host validation is unreliable on arm64. Architectural confirmation, not a fix.
- **Unpinned specs invite museum versions** (loose `samtools` → 0.1.19): minor; agent should pin.

## HPC / cluster dimensions (need the user at the cluster)

Per the trust posture (no cheeky head-node testing; real cluster runs are user-driven),
these split into (a) bridge-primitive hardening I CAN do via adversarial tests, and
(b) real cluster runs the USER must drive.

| # | Dimension | Status | Notes |
|---|-----------|--------|-------|
| 9 | GPU / accelerators | ⬜ | I12 honesty firewall ✅ certified; real CUDA run needs the cluster (mac = Metal only) |
| 10 | HPC bridge primitives (transfer/submit/poll) | ~✅ | L14 adversarial guards substantial; real runs user-driven |
| 11 | Production pipeline run on cluster | ⬜ | user-driven (submit_workflow_job → poll → fetch) |

Legend: ✅ green · 🔄 in progress · ⬜ untested. Update as rows burn down.

### Multi-step chaining probe (row 8) — findings

**Fixed:**
- **`depends_on` never materialized** (MEDIUM): the PipelineStep model documents `depends_on` as "derived at finalize from input/output overlap" but finalize was retired in the respine and seal never picked it up — so every sealed spec had `depends_on: []`, even for a step consuming a prior step's output. The self-verifying WorkflowSpec wasn't self-DOCUMENTING. Fixed: `_derive_step_dependencies` stamps the edge seal already computes for I8 (exact input↔output overlap, last-writer-wins, honors an explicit value). 8 tests. Same orphaned-respine-promise pattern as I5/I10.
- **freeze-runner lingered ~9.5 min after result written** (MEDIUM): a freeze that returned in 0.3s (adopt-by-digest) left its runner subprocess alive ~567s; the parent JobManager keys job state off `proc.poll()`, so `check_job` read "running" the whole time and `.done` never appeared (parent writes it on the terminal transition). Import leaves zero non-daemon threads (verified), so the linger comes from handles the adopt path creates during its run. Fixed: `freeze_runner` now `os._exit()`s once the result JSON is durably written (freeze writes all deliverables before returning; delivery jobs are independently tracked, so nothing is orphaned). Note: couldn't reproduce the exact adopt-then-linger locally (a nonexistent-env freeze hung INSIDE freeze instead — see below); fix targets the reported symptom.

**Fixed (the "ducks in a row" pass, commit pending):**
- **`ReferenceDatabase.source_url` made optional** (C(a)): a locally-staged reference (no download origin) previously had to fabricate a `file://` URL to pass the model. Now optional — `download_reference_database` still always sets it, I5 pins content by sha256 not URL, and both display sites already `.get()`-guard it. `usage.description`-required documented in the CLAUDE.md cheatsheet.

**Investigated → NON-BUG (record corrected):**
- **"`freeze()` on a nonexistent env HANGS"** — MISDIAGNOSIS. The container solve is already bounded: `cb.exec("pixi add …", timeout=1800)` (container_build.py:230), and every container step goes through `_sh(…, timeout)`. Freeze fails within a bounded window, not forever; the 120s I observed in a local smoke test was first-build docker overhead (base-image pull + pixi bootstrap), not an infinite hang. No fix — the system already handles it.

**Logged (not fixed — LOW/INFO):**
- **`run_pipeline_step` `output_types` `.bam` key reported unmatched** while the BAM still validated via extension inference — the explicit `{".bam": "bam"}` mapping wasn't honored by the `.ext` lookup path; inference saved it. INFO.
- **Host/adopted-image tool-version drift not surfaced**: host bwa 0.7.19 vs adopted mulled-image bwa 0.7.17 → different VCFs (80 vs 65 records). Expected for adopt-mode (shipped bytes win = the point of validated==shipped), but nothing flags the drift between pre-freeze host iteration and the sealed in-container run. INFO.

### Services probe — minor findings (logged, not fixed)
- **GAP 3 (ergonomic):** the conda daemon is `redis-server`, not `redis` (`redis` is the PyPI client). A naive install of `redis` fails `PackagesNotFoundError`; `resolve_tool` doesn't disambiguate a server-vs-client name collision. Doc/hint candidate.
- **GAP 4 (ergonomic):** a diagnostic `run_in_env` with a threaded `pipeline_id` silently becomes a `pipeline_step` (rc=0, no outputs) → an I3 risk at seal. No lightweight "run but don't record" flag; the agent must omit `pipeline_id` for sanity checks.
