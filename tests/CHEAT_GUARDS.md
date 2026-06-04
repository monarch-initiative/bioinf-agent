# Cheat-guard taxonomy

The test suite's primary job is to make the orchestrating agent unable to lie
about its deliverables. This file is the canonical map of *what* an agent
could fake and *which test* guards each cheat surface. New tests should
declare which L-level they extend; new cheats should land here first, then
the test.

The framing is **adversarial**: assume the agent is fast-talking and would
prefer to fake a result over admitting a tool didn't install. Every honesty
test exists because some way to cheat existed first. When a cheat surface has
no `Guard` cell, that's an open backlog item — fix the gap or accept the risk
explicitly.

> Reading this file end-to-end takes ~5 min and tells you exactly what the
> contract enforces. If you can't find the cheat you're worried about, it
> may not be guarded yet — add a row and a test.

---

## L1 — Tool install fakery

Cheats that get a "tool is installed" claim past the contract without an
actual working binary in the shipped image.

| Cheat | Guard |
|---|---|
| L1.a — wrapper script that just echoes a version string | `evidence_shape` (echo / `true` / `:` cheats rejected) — see `test_invariants.test_verify_*` and `tests/integration/honesty/L1_tool_install/test_evidence_shape_contract.py` |
| L1.b — wrapper that runs `true` so `command -v` returns 0 but the tool is broken | N3 wrapper-smoke chain (`--help` / `--version` / `-h` AND `command -v`) — `tests/integration/honesty/L1_tool_install/test_wrapper_smoke_evidence.py` |
| L1.c — `apt-get install -y <unrelated>` and claim the tool by reusing its name | name-token check in evidence (word-boundary token AND a real anchor) — `test_invariants` verify-anchor tests |
| L1.d — registry presence (`conda list`) but no binary on PATH | N1 conda-meta bin probe (resolves declared bin via `files` list in the meta JSON, runs it in-image) — `tests/integration/honesty/L1_tool_install/test_n1_conda_meta_probe_docker.py` |
| L1.e — shell-function `function tool() { echo 1.0; }` planted in `.bashrc` | not explicitly tested; image init is controlled so the surface is small — accept |

## L2 — Artifact identity

Cheats that decouple the claimed identity of an installed artifact from what's
actually on disk / in the image.

| Cheat | Guard |
|---|---|
| L2.a — clone commit X, claim commit Y in `install_method` | runtime `git rev-parse HEAD` post-clone (agent never supplies); seal records the same value — `test_invariants`, `tests/integration/correctness/test_install_git_repo_cross_arch.py` |
| L2.b — SHA-anchor a binary URL, then swap the actual bytes | install re-fetches + hashes; source-replay does the same in the build container — `test_invariants` `test_install_release_binary_archive_anchors_extracted_binary` |
| L2.c — pip install without recording a version | B7 fix: post-install `pip show` fills version into install_steps before request_key is computed — `test_invariants` B7 tests |
| L2.d — conda install resolves to build N, record build N-1 | `conda list --json` reads the actual installed env at SBOM time — `test_invariants` `test_record_content_digest_*` |

## L3 — Cache integrity (request_key / EnvCache)

Cheats that exploit the cache to short-circuit a build into a wrong artifact.

| Cheat | Guard |
|---|---|
| L3.a — cache-hit on a stale entry whose image has been evicted | `EnvCache.lookup_anchored` re-checks `docker image inspect` — `test_invariants` lookup_anchored tests |
| L3.b — request_key omits a policy facet (gated / accel / license) so two policy-distinct artifacts share a slot | D5 fix: request_key folds in policy hashes — `tests/integration/honesty/L3_cache_integrity/test_request_key_policy_facets.py` |
| L3.c — platform alias (`linux/amd64` vs `linux-64`) collapses onto two cache slots | D6 fix: canonicalizer — `tests/integration/honesty/L3_cache_integrity/test_request_key_policy_facets.py` |
| L3.d — adopt a biocontainer that has a different version of the tool than requested | version-aware adopt ranking + adopt refuses when non-conda installs are present (they can't be represented by biocontainers) — `test_invariants` B1/B7 tests |

## L4 — Mode honesty (adopt vs build)

The freeze has two modes; each has different truth-claims. Cheats that
over-claim from the weaker mode.

| Cheat | Guard |
|---|---|
| L4.a — adopt a pre-built image, claim VALIDATED_IN_IMAGE (we didn't run the contract) | Audit #2: adopt reports `ADOPTED_BY_DIGEST` + `POLICY_CLEAN` only — `tests/integration/honesty/L4_mode_honesty/test_attestation_predicate_roundtrip.py` |
| L4.b — adopt a gated artifact and push to a registry | I13: gated ⇒ tarball-only, never pushed — `tests/integration/honesty/L4_mode_honesty/test_i12_i13_policy_firewall.py` |
| L4.c — adopt biocontainer when env has non-conda installs (binary/source/pip/perl/cargo/go/jar) | biocontainer-adopt refuses when non-conda installs are present — `test_invariants` `test_adopt_decision_*` |
| L4.d — claim `runtime_verified` accelerator without an actual GPU probe | I12: `runtime_verified` requires `runtime_probe` + `min_driver_version` — `tests/integration/honesty/L4_mode_honesty/test_i12_i13_policy_firewall.py` |

## L5 — Workflow run honesty

The recorded run is the truth. Cheats that decouple the recorded run from
what actually happened.

| Cheat | Guard |
|---|---|
| L5.a — run on host, claim ran in shipped image | `validated_in_shipped_image` requires `ran_in_container=True` AND `container_image_digest` matches an EnvCache image_digest — `test_invariants.test_validated_in_shipped_image_*`, `tests/integration/honesty/L5_run_honesty/test_end_to_end_seal.py` |
| L5.b — watch_dir on /tmp picks up foreign files (harness transcripts etc.) as step outputs | N7: snapshot diff filters to `project_root` when watch is shared — `tests/integration/honesty/L5_run_honesty/test_watchdir_snapshot_filter.py` |
| L5.c — step produces no outputs but rc=0, claim success | I3: rc=0 needs detected_outputs OR explicit `mark_step_validated` — `tests/integration/honesty/L5_run_honesty/test_layer2_seal_refusals.py` |
| L5.d — `expected_type="any"` to bypass type-aware validation | I3: `expected_type=="any"` rejected at seal — `tests/integration/honesty/L5_run_honesty/test_layer2_seal_refusals.py` |
| L5.e — `output_types` typo silently falls through to `expected_type="any"` | N8: 5-tier lookup + unmatched keys surfaced in the response — `tests/integration/honesty/L5_run_honesty/test_n8_output_types_lookup.py` |
| L5.f — end-to-end: a step missing run-side fields slips through seal | G2 end-to-end seal wiring test — `tests/integration/honesty/L5_run_honesty/test_end_to_end_seal.py` |

## L6 — Provenance / authored artifacts

Cheats that decouple agent-authored content from what the deliverable says
about it.

| Cheat | Guard |
|---|---|
| L6.a — reference a file as a step input without staging it (orphan) | I8 composition coherence: every step input must trace to an external source OR a prior step's output — `test_invariants` I8 tests + `tests/integration/honesty/L5_run_honesty/test_layer2_seal_refusals.py` |
| L6.b — stage with sha256=X, mutate the file before seal | G1 seal-time re-hash (`I8.authored_artifact_mutated` / `_missing`) — `tests/integration/honesty/L6_provenance/test_authored_artifact_integrity.py`, `tests/integration/honesty/L5_run_honesty/test_end_to_end_seal.py` |
| L6.c — `patch_pipeline` writes to a runtime-captured field (pipeline_steps, install_steps, packages, ...) | PATCHABLE_KEYS allowlist — `test_invariants.test_patch_pipeline_blocks_runtime_captured_keys` |
| L6.d — synthesize an install_method but lie about its corpus / grounding | `synth_build` re-verifies the submission: EXTRACTED must appear verbatim in the named file; AGENT_AUTHORED must be GROUNDED — `tests/test_synthesis.py`, `tests/test_provenance.py` |

## L7 — SBOM / lock honesty

Cheats that decouple the SBOM / lock files from the actual installed env.

| Cheat | Guard |
|---|---|
| L7.a — edit recipe.yaml after build to misrepresent what was built | `content_digest` is derived from the recipe ingredients (lock + apt_snapshot + base_image + commit_sha) — recipe-image divergence detectable but not actively enforced (see open backlog item below) |
| L7.b — pixi.lock doesn't match installed packages | content_digest IS over the lock; mismatch shifts the digest — `test_invariants.test_record_content_digest_*` |
| L7.c — SBOM omits a package present in the image | `conda list --json` is the SBOM, generated from the IMAGE — agent can't selectively omit |

## L8 — Attestation predicate

The signed attestation is the trust chain root. Cheats that misrepresent
what was attested.

| Cheat | Guard |
|---|---|
| L8.a — declare `licenses=["MIT"]` but actually build a gated tool | Audit #2: caller-declared policy lives in a separate "Declared policy" section, explicitly NOT a verified fact. The contract guards against using a declared-license to bypass I13's gated firewall — `tests/integration/honesty/L4_mode_honesty/test_attestation_predicate_roundtrip.py` |
| L8.b — attestation subject digest doesn't match the actual image digest | runtime captures from `docker image inspect`; agent can't write it — `tests/integration/honesty/L4_mode_honesty/test_attestation_predicate_roundtrip.py` |
| L8.c — `validated_in_image` array claims a tool passed without running the evidence | env_honesty actually runs every evidence in the shipped image and stamps pass/fail — `test_invariants` env_honesty tests |

## L9 — HOWTO / user guide

The Layer-2 deliverable users execute. Cheats that decouple it from the
validated run.

| Cheat | Guard |
|---|---|
| L9.a — guide shows a different command than what ran | renderer reads `pipeline_steps[*].command` directly — `test_invariants.test_executed_commands_only_validated_and_run` |
| L9.b — guide references an unrunnable env | `freeze_request_key` is pinned and resolved against EnvCache at render — `test_invariants.test_render_user_guide_excludes_unrun_and_pins_env` |

## L10 — Synthesis tier grounding

The agent-as-generator residual path. Cheats specific to AGENT_AUTHORED
install commands.

| Cheat | Guard |
|---|---|
| L10.a — AGENT_AUTHORED commands with no corpus references | `check_grounding` requires `external_refs` to be present in the fetched corpus — `tests/test_provenance.py` |
| L10.b — EXTRACTED commands that aren't actually in the named file | `synth_build` re-checks verbatim presence against the runtime's fetched bytes — `tests/test_synthesis.py` |

## L11 — Universal file-type-agnostic lineage

| Cheat | Guard |
|---|---|
| L11.a — agent silently mutates a file at the same path between Step N and Step N+1 (path-only I8 walk doesn't see it) | producer step records `output_sha256: {path: sha}` at run time (`EnvManager.hash_outputs`); seal-time `_check_lineage_integrity` re-hashes any consumer's input matching a prior output and compares — `I8.lineage_mutated` / `I8.lineage_missing` — `tests/integration/honesty/L11_universal_lineage/test_lineage_integrity.py` |
| L11.b — overwrite semantics: which producer's hash wins when a path is written by two steps | LATER step in step-order wins (matches natural-overwrite semantics) — `test_path_reproduced_in_later_step_wins` |

This is file-type-AGNOSTIC by design — works for BAM, VCF, parquet, .weird,
anything. Per-file-type lineage (BAM @PG chain, VCF sample IDs) stays
optional, opt-in per `OutputValidator`.

## L12 — Apptainer/Singularity runtime

| Cheat | Guard |
|---|---|
| L12.a — docker-run-pass / apptainer-exec-fail drift (the shipped `.sif` differs in behavior from the source docker image) | `tests/integration/honesty/L12_apptainer_runtime/test_apptainer_runtime_smoke.py` — converts an existing `docker_images/<name>/<name>.tar` via `apptainer build docker-archive` and execs the smoke chain. `skipif-no-apptainer` so dev hosts without it just skip; HPC consumers and CI runners with apptainer execute |

## L13 — Recipe-replay determinism

| Cheat | Guard |
|---|---|
| L13.a — `content_digest` shifts on the same recipe across processes (would silently invalidate EnvCache) | pure-function determinism: same parts → same digest (sort-keyed JSON); each recipe part shifts the digest when perturbed — `tests/integration/honesty/L13_recipe_determinism/test_content_digest_determinism.py` |
| L13.b — recipe part has no influence on identity (collision surface) | parametrized perturbation test covers lock / longtail / platform / engine / base / apt_snapshot |
| L13.c — full-rebuild divergence (different bytes from same recipe) | DEFERRED to `integration_docker_slow` tier; the pure-function variant catches the contract bug; rebuild divergence is rarer and detectable via `lookup_anchored` on real runs |

## L14 — Compute-env command surface

The agent's interaction with a user's compute env (cluster / laptop) is
mediated by a fixed, small set of primitives. Each primitive is gated by a
per-directory permission declared in `~/.bioinf/projects_access.yaml`. Tests
pin the LITERAL shape of every shell invocation; permission denials fire
BEFORE any subprocess runs (fail-closed); no operation is reachable that
isn't declared in the manifest.

The v0 primitive surface is exactly one tool — `snapshot_project` — which
requires `file_name_only` permission. The `upload` permission is declared
in the schema but its primitive isn't wired yet; when it ships, it MUST
fail-closed on overwriting existing files.

**One-level visibility (the surface-minimization contract):**
`file_name_only` grants visibility into a directory's IMMEDIATE contents
only — subdirs are listed BY NAME, but their interiors are never walked.
A single declaration cannot snowball into a multi-million-file walk. To
inspect a subdir, the user must declare it as its own authorized entry.
This is enforced by `_local_walk` (root + iterdir, no rglob) and the
remote `find -maxdepth 1` flag.

| Cheat | Guard |
|---|---|
| L14.a — agent executes an unsanctioned shell command on the compute env | snapshot primitive emits exactly ONE shell shape (pure pathlib for local — zero subprocess; literal pinned remote-string for ssh); spy-on-subprocess tests assert only whitelisted invocations — `tests/integration/honesty/L14_compute_env_safety/test_snapshot_command_surface.py::test_local_snapshot_emits_no_subprocess`, `::test_ssh_snapshot_emits_only_ssh_subprocess` |
| L14.b — agent walks a directory the user didn't authorize | `compute_access.check_permission` is called BEFORE any subprocess; refuses with `PermissionDenied` if the path isn't in the compute_env's `directories[]` list — `::test_unauthorized_path_refused_before_subprocess` |
| L14.c — agent operates with the WRONG permission (e.g. `upload`-dir satisfies a `snapshot` request) | permission match is EXACT (discrete capabilities, not a lattice); `upload` does not satisfy `file_name_only` — `::test_wrong_permission_refused` |
| L14.d — shell injection via adversarial path in the config (`;`, `&&`, `$()`, backticks, pipes) | `shlex.quote` on the path inside the remote-shell string; parametrized injection tests confirm metacharacters appear as literal characters to `find`, not shell — `::test_ssh_remote_cmd_neutralizes_path_injection` |
| L14.e — silent permission misconfiguration (typo in `file_name_only` → default-none) | `compute_access.load_access` validates the schema at load time; unknown permission tokens raise `ConfigError` — `::test_unknown_permission_in_config_refused` |
| L14.f — relative paths bypass the gate | gate refuses non-absolute paths; absolute paths have well-defined meaning at the compute_env boundary, relative paths don't — `::test_relative_path_refused` |
| L14.g — ssh prompts for a password and hangs an agent with no stdin | ssh argv includes `-o BatchMode=yes` so missing ssh-agent fails fast — `::test_ssh_argv_includes_batch_mode_no_password_prompt` |
| L14.h — MCP wrapper leaks a raw exception (would crash the transport) | wrapper catches `PermissionDenied`/`ConfigError`/`FileNotFoundError`/`KeyError` and returns `{error: ...}` — `::test_mcp_wrapper_translates_permission_denied_to_error_dict` |
| L14.k — one declaration grants subtree visibility (recursion bypasses surface minimization) | `_local_walk` uses `root.iterdir()` (no rglob); ssh `find -maxdepth 1`. Explicit pin: subdir contents not in the snapshot when only the parent is declared — `::test_snapshot_does_not_recurse_into_subdirs`, `::test_ssh_remote_cmd_carries_maxdepth_1`. End-to-end against this repo's envs/: `tests/integration/correctness/test_snapshot_local_envs_e2e.py::test_envs_root_snapshot_does_NOT_reveal_bin_contents` |

### Open backlog (L14)

| ID | Cheat | Note |
|---|---|---|
| L14.i — `upload` primitive overwrites an existing file | DEFERRED: contract decided ("never overwrite"), primitive not yet wired. When implementing: pre-flight `test -e <target>` over ssh; refuse if exists; require explicit `--force` for overwrite |
| L14.j — agent executes a new operation (download / hpc_run / etc.) | DEFERRED: each new operation MUST add an entry to `compute_access.OPERATION_REQUIRES`, a `check_permission` gate before its subprocess, and a cheat-guard test under L14_compute_env_safety/ |

---

## Open backlog

| ID | Cheat | Note |
|---|---|---|
| L7.a-strict | Recipe-image divergence enforcement | Current state: content_digest will diverge but we don't actively refuse on it. The image is what ships; recipe is provenance documentation. Acceptable as-is unless a real attack surface emerges. |

## Conventions for new tests

1. **Frame as adversarial.** The test's name and docstring should describe the
   cheat shape it prevents, not the function it calls.
2. **Cite the L-level in the docstring.** `CHEAT GUARD LEVEL: L5.a` or similar.
   Helps the next reader (or AI) cross-reference this file.
3. **Short-circuit at the boundary you don't care about.** Monkeypatch the
   subprocess / validator / network call; let the cheat-surface code run for
   real. The N1 production bug surfaced because we had a Docker-backed test;
   most cheats can be caught with cheaper stubs.
4. **Negative + positive.** Every cheat needs both a *caught* test (the agent's
   attempt fails) AND an *honest* test (the same shape, done honestly, passes).
   Without the positive case, "false-positive refusal" creeps in over time.
