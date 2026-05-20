# fastp install — first autonomous flow validation post I4/I7/I8 refactor

Date: 2026-05-19
Outcome: **PASS** — first end-to-end autonomous install producing a spec that satisfies every invariant including the new I4 (multi-shape self-test), I7 (resource provenance), I8 (composition coherence).

## What worked

- `install_packages(bioinf_fastp, [{spec: fastp, channel: bioconda}])` → reused existing env, recorded install_step.
- `verify_installation` → captured `fastp 1.3.3` as verify_output (I2).
- `run_pipeline_step` with `watch_dir` → detected all 4 outputs (trimmed R1/R2, JSON, HTML), auto-validated each (I3).
- `resource_usage` populated by the psutil monitor for the live step: wall_seconds=0.96, peak_rss_mb=82.7, max_cpu_percent=104.6, sample_count=4 (I7).
- `usage.trials` declared TWO paired-end shapes (exome SRR1517830, rnaseq ERR188297). Finalize self-test ran the `command_template` against BOTH in fresh scratch dirs; both produced all declared output files. `_self_test = {ok: true, trial_count: 2, passed: 2, source: trials}` (I4).
- I8 composition check passed cleanly: the single step's two FASTQ inputs are paths in `test_data` (declared external); detected_outputs do not feed any downstream step in this single-step pipeline.
- Docker image built (`fastp:1.3.3`, linux/amd64), conda-pack tarball produced, docker_status=built.
- `lock_sha256: e5f6ab9feb2dc88ced3eeaee2d4b22b925d98a868c6f1c56e5e997d2e030e26a` recorded for byte-verification.
- `pytest tests/test_invariants.py` — 8/8 pass (including the new fastp spec).

## Real bug surfaced

**BIOINF_MCP_AUTO_RELOAD did not propagate to the MCP server process.** The server was launched at 12:14pm without the env var (confirmed via `ps eww`), so the auto-reload watcher never started. My code edits to env_manager / spec_writer / core_data sat on disk for hours while the live server kept running pre-refactor code. The first symptom was `run_in_env` returning a response with **no `resource_usage` key** despite the source clearly populating it.

Fix applied this session: `kill <pid>`; the MCP client respawned the server on the next call, which loaded the latest code.

Followup needed (not done this session — not in scope for the validation): figure out why the `env` block in `.claude/settings.json` doesn't reach the spawned server's environment. Workaround: kill the server when stale code is suspected. **Don't trust BIOINF_MCP_AUTO_RELOAD until this is understood.**

## Observed resource cost

- fastp 1.3.3 on 10K paired exome reads: 0.96s wall, 82.7 MB peak RSS, 104.6% CPU peak (briefly multi-threaded).
- Self-test trials each ran a separate fastp invocation in `/tmp/selftest_fastp_*/`; both completed <1s.

## What this proves

The "small invariants + small primitives → cascading correctness" architecture works end-to-end on a real install driven through the live MCP server. A subagent or interactive operator can now point `install_pipeline_brief("fastp")` at this stack and produce a spec whose every claim is anchored to an on-disk artifact or a recorded subprocess execution.

## Blockers / scar tissue

- One real blocker (auto-reload), one cosmetic gap.
- Cosmetic: `validate_pipeline_draft` reported `self_test.command_run: ""` / `substitutions: null` in its summary output. The actual finalize self-test ran correctly with full trial detail; the dry-run summary is just truncating the multi-trial output. Minor — fix the dry-run formatter when convenient.

## Coverage gaps (out of scope but worth recording)

- Both trials are paired-end gzipped FASTQs. Genuinely different shapes (paired-vs-single, gzipped-vs-uncompressed) would prove more. fastp's `command_template` references `INPUT_R2` unconditionally so single-end can't be tested without a different template. To exercise that, add an OR-pattern or a second usage block in a future install.
- No GPU step here — `peak_gpu_mb` always None. The I7 schema supports it; needs a CUDA pipeline to exercise.
