# Tier 7 — deletion verification (2026-07-16)

Raw output of the `tier7-subtract-verify` workflow at HEAD `36c0d8c`: 10 verifiers
re-checked every candidate AGAINST HEAD (the earlier scouting predated tiers 5/6), then
10 adversarial refuters were told to PROVE each DELETE verdict wrong.

**Nothing here is deleted yet. Nothing marked REFUTED may be deleted.**

The refutation pass earned its keep twice — see the two REFUTED rows. The original audit
had already mislabelled two live subsystems (`scp_head_node`, GPU submission) as dead
empire, so a 'dead code' verdict is a claim like any other: refute it before acting.

Delete this file once Tier 7 lands — it is a working artifact, not a record.

## Verdicts

| item | verdict | real LOC | refutation |
|---|---|---:|---|
| `delete_decision_tree_and_stale_docs` | DELETE | 2653 | upheld |
| `delete-async-globus-and-globus_task_status-rec` | DELETE | 224 | **REFUTED — DO NOT DELETE** |
| `resolver.route + env_freeze.build_env_from_too` | DELETE | 186 | upheld |
| `env_manager_retired_version_probes` | DELETE | 170 | upheld |
| `dead-code-batch-7-symbols` | DELETE | 130 | upheld |
| `spack` | DELETE | 122 | upheld |
| `freeze.content_digest_parts + freeze.content_d` | DELETE | 93 | upheld |
| `agent/skills/env_vendor.py` | DELETE | 77 | upheld |
| `agent/models/__init__.py + KNOWN_PIPELINES` | DELETE | 47 | upheld |
| `install_method.docker_pull+manual / PipelineSp` | PARTIAL | 34 | **REFUTED — DO NOT DELETE** |

**Safe to delete: ~3478 LOC.** Blocked: 2.

## The two refutations (read these before touching either area)

### delete-async-globus-and-globus_task_status-reconciliation

**Why the DELETE verdict is wrong:**

The verdict bundles two independent things and is wrong about the second. `globus_task_status` (transfer_providers.py:1011-1080 + the @mcp.tool at bridge_tools.py:192-237) has ZERO coupling to async. Its only guards are: env.type=="ssh", env declares data_transfer.type=globus, and task_id matches a canonical-UUID regex. Nothing checks whether the task came from an async submit. Async touches only the optional finalize_manifest_for_task tail at :1071-1079.

The claim's own corpus evidence refutes it. "Zero records carry globus_task_id from an async submit" is true but supports the reverse inference. I re-ran the corpus: 80 of 92 real records carry a live globus_task_id (63 uploaded + 10 downloaded + 7 failed), ALL minted by the SYNC path (transfer.py:1082 journals task_id=pr.get("task_id") on sync success). Only the 12 "refused" lack one because they never reached Globus. All 80 pass globus_task_status' UUID guard (verified programmatically, 80/80), and projects_access.yaml:141 declares a LIVE env with data_transfer.type=globus, so all three guards pass. The sync path is a task_id FACTORY and globus_task_status is the only consumer of its output. The claim measured "async never ran" and concluded "the poll primitive is unreachable" — a non-sequitur.

Concretely stranded today: the two 540s-cap firings left tasks 72df1b28-798f-11f1-b724-02f0d340f1a1 and 8b03ebf4-799b-11f1-9780-02f0d340f1a1 in state 'ACTIVE' — Globus keeps running them server-side. globus_task_status is the ONLY tool in the repo that can answer "did it finish?", and it works on those IDs with no async involved. Deleting it removes the sole resolution path for a failure mode that has ALREADY OCCURRED TWICE and is structurally guaranteed to recur: wait_cap = min(timeout, 540) (transfer_providers.py:569) and `timeout` appears ZERO times in bridge_tools.py, so an MCP caller can never exceed 540s. At the observed 9.4 MB/s that is a hard ~5 GB ceiling on the stage_apptainer.py:174 .sif push with no override.

CONCEDED: the async SUBMIT machinery genuinely never ran (transfer_providers.py:550-566, transfer.py:1055-1067, _find_manifest_by_task_id + finalize_manifest_for_task at transfer.py:667-743, ~107 LOC). All three bugs are real. That subset is a defensible removal. But globus_task_status must SURVIVE — with its finalize_manifest_for_task call dropped and bug 1 (unconditional "success": True on FAILED, :1056/:1080) fixed, since polling a sync task is exactly where a wrong `success` misleads.

EDIT-SET ERRORS (secondary): (1) scripts/render_decision_tree.py:554-555 writes .json and .html, NOT .md — the claim's "REGENERATE scenario_decision_tree.md from the script" is impossible; the .md is hand-maintained (script line 50: "mirrors docs/scenario_decision_tree.md"), and the claim OMITS scenario_decision_tree.html which the script DOES generate. (2) Three files reference the symbols and are absent from the edit set: docs/hpc_bridge_phase2.md:492, docs/stress_findings.md:179, tests/test_outcome_tags.py:272.

**Reachable via:**

```
REACHABLE via the SYNC globus path, not via async at all:

1. MCP entry point (user/agent-callable, live): bridge_tools.py:192 `@mcp.tool() def globus_task_status(project_name, compute_env_name, task_id)`. AST-confirmed as 1 of 8 @mcp.tool functions in bridge_tools. Exposed to me right now as `mcp__bioinf__globus_task_status` in my own deferred-tool list — callable this turn. Documented in CLAUDE.md:78 as a primitive the agent is told to use.

2. The task_ids it consumes are produced by the LIVE SYNC path, not async:
   transfer.py:1082  _journal(result="success", ..., task_id=pr.get("task_id"))
   -> 80/92 real transfer_history records carry a canonical-UUID `globus_task_id`
   -> all 80 pass globus_task_status' UUID guard (verified: 80 pass / 0 fail)
   -> projects_access.yaml:138-146 declares a live env with data_transfer.type=globus, satisfying the other two guards.
   Any of those 80 IDs is a valid input to globus_task_status TODAY with zero async involvement.

3. The system's own live error handler routes the caller INTO it. transfer_providers.py:599-611 `transfer.globus_sync_wait_exceeded` returns `task_id=task_id` and its message names globus_task_status. This terminal FIRED TWICE in real usage:
   transfer_history/longleaf_test/2026-07-06/2026-07-06T23-16-31Z_download_2ff67f7581.json -> task 72df1b28-798f-11f1-b724-02f0d340f1a1, still 'ACTIVE'
   transfer_history/longleaf_test/2026-07-07/2026-07-07T00-43-06Z_download_f996bfa478.json -> task 8b03ebf4-799b-11f1-9780-02f0d340f1a1, still 'ACTIVE'
   Both left a server-side-running Globus task whose ONLY interrogation path in this repo is globus_task_status.

4. Structural recurrence guarantee: wait_cap = min(timeout, _SYNC_WAIT_S_DEFAULT=540) at transfer_providers.py:569/429; `timeout` is NOT exposed on the MCP surface (0 hits for "timeout" in bridge_tools.py). stage_apptainer.py:174 pushes .sif/.tar through transfer.upload on this exact path. At the observed 9.4 MB/s that is a hard ~5 GB ceiling; a routine >5G
```

### install_method.docker_pull+manual / PipelineSpec.docker / DockerBuild

**Why the DELETE verdict is wrong:**

PARTIALLY REFUTED — the core thesis holds, but one member of the deletion set is genuinely reachable and the proposal as written breaks real things.

CONFIRMED (I attacked these and could not break them): docker_pull/manual have zero producers — verified no on-disk records at ANY extension (incl. drafts/EnvCache), no scripts/.claude/hook/shell refs, no star-imports of agent.models, no getattr/dispatch-dict/globals() lookup, no subprocess entry (freeze_runner.py/__main__.py clean). resolver.py's "manual" IS a separate namespace (TIER_ORDER, line 55-56) never assigned to install_method.type. DockerBuild: zero instantiations. list_installed_pipelines: I reproduced 7/7 ValidationError exactly. BLOCKED_PATCH_KEYS removal is security-neutral (patch is whitelist-first). InstallMethod is only ever constructed in tests — its Literal is inert in production because WorkflowSpec does NOT embed packages/install_steps (core_data.py:1115), so PackageRecord/InstallMethod are only reachable via the producerless PipelineSpec.

REFUTED #1 (the real one) — user_guide.py:171 is NOT "always {}". generate_user_guide (freeze_tools.py:687) has signature (pipeline_id="", spec: dict = {}, freeze_request_key="", write=True) and passes the CALLER-SUPPLIED spec dict UNVALIDATED into render_user_guide (line 710). freeze_request_key is OPTIONAL, so `freeze_record=None` -> the docker fallback is the DEFAULT branch, not an unselected one. This is the "public entry point a user calls directly" + "default branch" vector. The claim's "LIVE, always {}" assumes the spec always comes from a draft; the documented MCP surface says otherwise. Deleting the read is NOT behavior-identical: it silently rewrites a caller-supplied REAL digest-pinned image ref into a FABRICATED "{name}:latest" tag — in the one tool whose stated premise is "every command shown was executed". The claim spotted the fabrication smell but missed that its own edit makes fabrication STRICTLY MORE likely.

REFUTED #2 — the proposal breaks 4 currently-green tests. I applied ALL proposed edits to a scratch copy (verified `agent` resolved to the copy) and ran pytest. Baseline in the untouched repo: 23 passed. After the proposal: 4 failed / 19 passed:
  - test_patch_pipeline_blocks_runtime_captured_keys (test_invariants.py:235 pins "docker"/"docker_status" in rejected_keys)
  - test_content_digest_is_stable_and_sensitive
  - test_content_digest_from_spec_is_degenerate_on_a_draft (test_invariants.py:1745 — a REGRESSION GUARD encoding a real past bug: "4 distinct envs -> identical record content_digest". Deleting the function deletes the guard documenting WHY the freeze path must never go back to it.)
  - test_render_user_guide_without_freeze_falls_back_to_docker (test_invariants.py:2135 — exercises the exact docker read; fixture pipeline_name="bwa_samtools" matches no package, so version=="" and the fallback yields "bwa_samtools:latest", not the asserted "bwa_samtools:1.21")
The claim measured "BASELINE GREEN ... 8 passed" BEFORE any edit and never re-ran after. It mentions "the test update below" for pipeline_state, but no such test edit exists in its proposed-edits list.

REFUTED #3 — the claim's extra="allow" safety argument inverts. It proposes deleting PipelineSpec.docker while explicitly leaving resources.py:170/178 alone. I proved on the patched model that a legacy docker block lands in extras as a RAW DICT, so pspec.docker returns dict and resources.py:178 `docker.image_tag` raises AttributeError: 'dict' object has no attribute 'image_tag' — not the claimed benign "lands in extras rather than raise". Masked today only because list_pipelines is 100% broken; it becomes a live crash the moment anyone takes the claim's own option (b) fix.

REFUTED #4 — stale anchors. The claim asserts "ALL line numbers are HEAD-stable (via git show HEAD:<file>)" and blames a dirty tree. My tree is CLEAN at HEAD 36c0d8c, and `git show HEAD:agent/models/core_data.py` puts DockerBuild at 1085 and PipelineSpec.docker at 1222 — NOT the claimed 1048/1185. The claim analyzed an older commit (gitStatus snapshot 8a27daf; HEAD has since moved through 40cf9ca/36c0d8c). Any scripted application of these edits hits the wrong lines.

SAFE SUBSET (if the user wants it): DockerBuild class + models/__init__ exports + InstallMethod.docker_image + the Literal fix (adding the genuinely-missing synthesized/spack) + freeze.py:298 `t != "conda"` + the docstring fixes. NOT safe without rework: the user_guide.py:171 collapse (reachable, honesty regression), the content_digest_* deletion (kills a regression guard; needs the tests deleted deliberately, which is a judgment call for the user), PipelineSpec.docker deletion (needs resources.py fixed in the same change), and BLOCKED_PATCH_KEYS (needs test_invariants.py:235 updated).

**Reachable via:**

```
agent/skills/user_guide.py:171 (`docker = spec.get("docker") or {}`) IS reachable with a NON-CONSTANT value via the documented MCP tool generate_user_guide (agent/mcp_tools/freeze_tools.py:687-710), whose `spec: dict = {}` parameter accepts an arbitrary caller-supplied dict passed unvalidated to render_user_guide, and whose `freeze_request_key` is optional (freeze_record=None makes the docker branch the DEFAULT). Executed read-only against the UNPATCHED repo:

  $ python -c "from agent.skills.user_guide import render_user_guide; print(render_user_guide({'pipeline_name':'legacy_tool','docker':{'image_tag':'ghcr.io/authors/legacy@sha256:deadbeef'}}, freeze_record=None))"
  -> 'apptainer pull legacy_tool.sif docker://ghcr.io/authors/legacy@sha256:deadbeef'

The proposed edit would instead emit 'apptainer pull legacy_tool.sif docker://legacy_tool:latest' — a fabricated tag replacing a real digest-pinned reference. This path is also pinned by a currently-green test (tests/test_invariants.py:2135 test_render_user_guide_without_freeze_falls_back_to_docker), and the render_user_guide docstring (user_guide.py:147-149) documents it verbatim: "without it the guide falls back to the spec's docker info."

Corroborating proof the proposal is unsafe (scratch copy at /private/tmp/.../scratchpad/repo, repo left untouched): baseline `pytest tests/test_invariants.py -k "content_digest or user_guide or patch_pipeline or install_method or non_conda"` = 23 passed; with ALL proposed edits applied = 4 failed, 19 passed.

For the other members of the cluster (docker_pull/manual enum values, DockerBuild class, InstallMethod.docker_image, PipelineSpec.docker as a producer target): none found — genuinely unreachable, thesis confirmed.
```

---

## Full per-item evidence, edits, tests and docs

## delete_decision_tree_and_stale_docs

- **verdict** `DELETE` · **LOC** 2653 · claim accurate: partly · MCP-reachable: False · risk: low
- **refutation**: upheld

### evidence
```
CORE CLAIM CONFIRMED (render_decision_tree.py is a hand-authored literal, dead):
- scripts/render_decision_tree.py:53 `TREE = N("🧑‍🔬 Scientist — ...", kind="root", ...)` — exactly line 53 as claimed. Its ONLY imports are `json` + `pathlib` (grep "import " → lines 23,25,26). It reads NO input (no read_text/json.load/open of any source). Its own docstring:6-9 admits: "The tree is held as structured data below (the hand-maintained 'scenario overlay')... so this COULD LATER be fed by an AST skeleton-extractor and stay in sync with the code."
- Zero tests: `grep -rn "decision_tree|render_decision" tests/` → 0 hits.
- Zero subprocess/`python -m` spawns: grep across *.py/*.sh/*.json/*.yaml/*.toml/*.cfg/*.ini → only self-references inside render_decision_tree.py. No Makefile, no .github/, no .claude/ hook.
- Zero MCP reachability: no agent/ module imports it (73 @mcp.tool functions, none touch it).
- Staleness CONFIRMED: grep author_image|authors_recipe|freeze_from_image|build_env_from_authors_recipe → 0 hits in ALL FOUR tree files, while those tiers are live in 5 modules (agent/skills/authors_sources.py, resolver.py, freeze_from_image.py, agent/mcp_tools/env_tools.py, freeze_tools.py).

DRIFT QUANTIFIED (the hand tree models ~20% of the real surface):
- docs/scenario_decision_tree.json = 164 nodes / 103 outcomes {proven:22, refused:55, broke:14, degraded:7, vanished:3, loop:2}.
- docs/outcomes_ledger.json (DERIVED by scripts/extract_outcomes.py via AST over real code) = 515 terminals / 496 distinct codes {refused:228, broke:185, proven:98, loop:3, untagged:1}, INCLUDING 11 entries the tree omits entirely: freeze_from_image.image_absent, freeze_from_image.pull_failed, freeze_from_image.frozen, authors_recipe.clone_failed, authors_recipe.checkout_failed, authors_recipe.build_failed, +5.
- git last-touch: render_decision_tree.py + scenario_decision_tree.md frozen at 2026-07-02 (d18fd4b); outcomes_ledger.json 2026-07-16 (d0d34c7), seaworthiness.md 2026-07-16 (fb57ac0). The hand copy is 14 days / ~7 commits stale across the entire authors-recipe arc (8cac979, 52d6abe, d0f0060, 40cf9ca).

CLAIM WRONG ON LOC: claim says "~1,100 LOC total" for the tree set. Real: 1355 (json) + 344 (md) + 181 (html) + 557 (render_decision_tree.py) = 2437. Plus system_dimensions.md 86 + stress_test_campaign.md 130 = 2653 total.

CLAIM WRONG ON "nothing imports it": no CODE imports it, but THREE prose referrers exist and must be fixed, one in a LIVE module:
- agent/skills/outcomes.py:13 (imported by 34 agent/ modules — LIVE) claims "the decision-surface model (docs/scenario_decision_tree.*) is DERIVED from the code and cannot silently drift". FALSE on both counts.
- agent/skills/outcomes.py:16 "The six classes mirror the decision-tree legend (docs/scenario_decision_tree.md):"
- scripts/extract_outcomes.py:5 "(see docs/scenario_decision_tree.md and agent/skills/outcomes.py)"
- docs/seaworthiness.md:9 links stress_test_campaign.md.
CLAUDE.md and README.md: ZERO references to any of 
```

### edits

- `scripts/render_decision_tree.py` — **delete_file** — 557 LOC. Entire file. Hand-authored TREE literal at :53; imports only json+pathlib; reads no input; zero callers/tests/spawns/CI. Function superseded by the DERIVED pair scripts/extract_outcomes.py -> docs/outcomes_ledger.json -> scripts/render_outcomes_dashboard.py -> docs/outcomes_dashboard.html. DO NOT touch those four — they are live.
- `docs/scenario_decision_tree.json` — **delete_file** — 1355 LOC. Generated output of the deleted renderer (render_decision_tree.py:554). 164 nodes/103 outcomes vs the ledger's 515 terminals; omits the authors_recipe/author_image/freeze_from_image tiers entirely.
- `docs/scenario_decision_tree.html` — **delete_file** — 181 LOC. Generated output of the deleted renderer (render_decision_tree.py:555). Header hardcodes the stale '103 tagged outcomes' legend (:46).
- `docs/scenario_decision_tree.md` — **delete_file** — 344 LOC. The hand-maintained 'scenario overlay' source-of-prose the literal mirrors (render_decision_tree.py:50 'THE TREE (mirrors docs/scenario_decision_tree.md)'). Last touched 2026-07-02 d18fd4b.
- `docs/system_dimensions.md` — **delete_file** — 86 LOC. Zero referrers repo-wide (incl. seaworthiness.md and CLAUDE.md). Delete for being an orphan AND actively false — :64 row 11 'Production pipeline run on cluster | ⬜' is contradicted by job_submissions/longleaf/ real submissions + RUNGS 3+4 GREEN (2026-07-06). NOTE: the claim's stated reason ('superseded by the dashboard') is wrong — the dashboard measures an orthogonal axis. If any content is salvaged, salvage only :62 row 9 (GPU: 'I12 firewall certified; real CUDA run needs the cluster'), which is still accurate and is the one open capability row — fold it into docs/seaworthiness.md rather than losing it.
- `docs/stress_test_campaign.md` — **delete_file** — 130 LOC. Self-declared SUPERSEDED at :2-4 by seaworthiness.md; a plan doc titled 'NOT YET RUN'. CAVEAT: :7 says 'Keep this for the tier reference' — the install-tier matrix is its only unique content. Delete is defensible (zero code depends on it) but this is the weakest of the six; if the tier matrix is still wanted, KEEP this one file and delete the other five.
- `agent/skills/outcomes.py` — **edit** — Lines 11-14 (docstring). LIVE MODULE — imported by 34 agent/ modules; docstring-only change, no runtime effect. MUST fix: it currently asserts the deleted artifact is derived. Replace 'so the decision-surface model\n     (docs/scenario_decision_tree.*) is DERIVED from the code and cannot silently\n     drift.' with a pointer to the artifacts that ARE derived: 'so the decision-surface model (docs/outcomes_ledger.json, rendered to docs/outcomes_dashboard.html) is DERIVED from the code and cannot silently drift.' Also line 16: drop the '(docs/scenario_decision_tree.md)' parenthetical from 'The six classes mirror the decision-tree legend (docs/scenario_decision_tree.md):' -> 'The six classes:'. Leaving :13 as-is would strand a false claim pointing at a deleted file.
- `scripts/extract_outcomes.py` — **edit** — Line 5 (docstring). Change 'see docs/scenario_decision_tree.md and agent/skills/outcomes.py' -> 'see agent/skills/outcomes.py'. Cosmetic; no runtime effect. File itself is LIVE — do not delete.
- `docs/seaworthiness.md` — **edit** — Lines 9-10. Remove the dead link sentence 'Canonical over [stress_test_campaign.md](stress_test_campaign.md) (the tool-tier\nexercise is now just one input to this). ' and PRESERVE the rest of line 10: 'Findings log: [stress_findings.md](stress_findings.md).' (stress_findings.md is a different, live file — do not remove that link).
- `docs/audit_2026-07-16.md` — **edit** — Lines 695-699. The audit's own claim text. Correct two errors if the doc is kept as a record: (a) :695 '~1,300 LOC' -> the real tree-set figure is 2,437 (2,653 incl. both docs); (b) :698 'system_dimensions.md (hand-maintained checkboxes, superseded by the dashboard)' — the dashboard does NOT supersede it (orthogonal axis); the real reason is orphaned + factually false at :64.

### tests affected

- (none)

### docs affected

- docs/seaworthiness.md:9-10
- agent/skills/outcomes.py:11-16
- scripts/extract_outcomes.py:5
- docs/audit_2026-07-16.md:695-699

### surprises

1. THE BIG ONE — the lie lives in the LIVE module, not the dead one. agent/skills/outcomes.py:13 (imported by 34 modules) states the decision-tree model "is DERIVED from the code and cannot silently drift". Both halves are false: render_decision_tree.py:6-9 openly admits the tree is "hand-maintained" and only "COULD LATER be fed by an AST skeleton-extractor", and it HAS silently drifted (103 modelled outcomes vs 515 real terminals; the entire authors-recipe tier missing for 14 days). The dead renderer is honest about itself; the live module's docstring is what misleads. This is a textbook instance of the audit's disease — and deleting the tree WITHOUT the outcomes.py:11-16 edit leaves the false claim pointing at a deleted path. That docstring edit is mandatory, not cosmetic.

2. system_dimensions.md is stale in the audit's OWN direction of error. Its :64 row 11 "Production pipeline run on cluster | ⬜ [untested]" is false — job_submissions/longleaf/ contains real production submissions and RUNGS 3+4 went GREEN 2026-07-06. This is the same failure mode as the audit's two known misses (scp_head_node, GPU submission called dead while live): a hand-maintained record claiming a live capability is absent. Its :62 GPU row 9, by contrast, is still ACCURATE ("I12 firewall certified; real CUDA run needs the cluster") and is the only surviving unique content — salvage that one line into seaworthiness.md rather than dropping it.

3. ADJACENT-DEAD CHECK CAME BACK CLEAN — and there is a live look-alike to protect. scripts/ holds a near-identical sibling, render_outcomes_dashboard.py, which IS live and derived (reads outcomes_ledger.json + terminal_coverage.json; its docstring:17 states it "is A PROJECTION OF THE CODE + THE TEST RUN, not a hand-drawn diagram — so it can't rot or flatter us"). extract_outcomes.py, measure_terminal_coverage.py, and seaworthy_scope.py are likewise live. A careless `rm scripts/render_*.py` would take out the derived dashboard alongside the hand-drawn tree. The two renderers ARE the N-places pair; the fix is deleting the hand-drawn one, and only that one.

4. The claim's LOC number is off by ~2.2x (1,100 claimed vs 2,437 real for the tree set), and "nothing imports it" is true only for code — 3 prose referrers exist, 1 in a live module. Both errors bias toward under-stating the work, not over-stating the case; the verdict survives.

5. stress_test_campaign.md is the one file with a stated keep-intent (:7 "Keep this for the tier reference") and an inbound link from live seaworthiness.md:9. No code depends on it, so DELETE is defensible, but it is the only one of the six where a reasonable person could say KEEP — flagging rather than burying that.

---

## delete-async-globus-and-globus_task_status-reconciliation

- **verdict** `DELETE` · **LOC** 224 · claim accurate: partly · MCP-reachable: True · risk: medium
- **refutation**: REFUTED — The verdict bundles two independent things and is wrong about the second. `globus_task_status` (transfer_providers.py:1011-1080 + the @mcp.tool at bridge_tools.py:192-237) has ZERO coupling to async. Its only guards are: env.type=="ssh", env declares data_transfer.type=globus, and task_id matches a canonical-UUID regex. Nothing checks whether the task came from an async submit. Async touches only 

### evidence
```
CORPUS (claim CONFIRMED): 92 records in transfer_history/, ALL `"transport": "globus"` (sync globus is heavily live). Outcome counts: 63 uploaded / 12 refused / 10 downloaded / 7 failed / **0 submitted**. Zero records carry `"task_id"` or `"globus_task_id"` from an async submit. The async path has never run.

LOC (claim's 224 is EXACT for the core block):
  71  transfer_providers.py:1010-1080  globus_task_status
  17  transfer_providers.py:550-566    async early-return branch in _run_transfer
  77  transfer.py:667-743              _find_manifest_by_task_id + finalize_manifest_for_task
  13  transfer.py:1055-1067            async submit branch (_journal result="pending")
  46  bridge_tools.py:192-237          @mcp.tool globus_task_status
 224  TOTAL (+ ~181 LOC of tests, + ~20 LOC param threading)

REACHABILITY — NOT dead code, it is reachable-but-never-used:
  - bridge_tools.py:192 `@mcp.tool() def globus_task_status` is one of 67 @mcp.tool functions (claim says 65; actual count is 67 -> 66 after deletion).
  - `async_globus: bool = False` is an EXPOSED parameter on the MCP tools upload (bridge_tools.py:77) and download (bridge_tools.py:133).
  - No subprocess/`python -m` spawn reaches it (checked; only agent/skills/freeze_runner.py uses that pattern, unrelated).
  - No internal caller ever passes async_return=True: stage_apptainer.py:174, acquire_data.py:311/484, submit_workflow.py:406, run_cluster_step.py:390/537 all call transfer.upload/download WITHOUT async_globus. The ONLY async_return=True call sites in the repo are 2 test lines.
  => This is a deliberate feature removal, not dead-code cleanup.

ALL THREE BUGS CONFIRMED at exact lines:
  1. transfer_providers.py:1056-1057 sets `"success": True` and :1080 `return proven("transfer.task_status_ok", **out)` unconditionally — fires even when :1055 `status == "FAILED"`.
  2. transfer_providers.py:551-566 async branch returns immediately; the `_is_permission_denied` classifier only runs in the sync poll loop (:581 ACTIVE-detector, :620 FAILED-detector). Async never classifies.
  3. transfer.py:739-742 `try: mpath.write_text(...) except OSError: pass` then :743 `return mpath` (non-None) -> transfer_providers.py:1076-1079 sets `manifest_outcome: "uploaded/downloaded"` on a write that silently failed.

*** SURPRISE — the claim's RATIONALE is WRONG (verdict survives, edit set changes): ***
The claim says "Largest real transfer was 1.0GB SYNC in 1m25s vs a ~600s watchdog, so the need never materialized."
  - The cap is `_SYNC_WAIT_S_DEFAULT = 540` (transfer_providers.py:429), not ~600. `wait_cap = min(timeout, 540)` (:569) and `timeout` is NOT exposed on the MCP surface -> an MCP caller can NEVER wait past 540s.
  - The cap DID FIRE TWICE IN REAL USAGE — two records at took "9m09s" (=549s), outcome "failed":
      transfer_history/longleaf_test/2026-07-06/2026-07-06T23-16-31Z_download_2ff67f7581.json
      transfer_history/longleaf_test/2026-07-07/2026-07-07T00-43-06Z_download_f996bfa478.json
    Both
```

### edits

- `agent/mcp_tools/bridge_tools.py` — **remove_symbol** — Delete the @mcp.tool `globus_task_status` at lines 192-237 (decorator on 192, def on 193-195, through `return transfer_providers.globus_task_status(env, task_id)` on 237). Drops the MCP tool count 67 -> 66.
- `agent/mcp_tools/bridge_tools.py` — **edit** — upload(): remove param `async_globus: bool = False` (line 77) and the pass-through `async_globus=async_globus,` (line 123). Remove docstring sentence lines 99-101 ("Pass `async_globus=True` to return immediately with a task_id instead of waiting for SUCCEEDED; poll completion via `globus_task_status`.") keeping the `scp_head_node`/`globus` wire-protocol sentence. Remove `task_id?,` from the Returns list (line 114).
- `agent/mcp_tools/bridge_tools.py` — **edit** — download(): remove param `async_globus: bool = False` (line 133) and pass-through `async_globus=async_globus,` (line 155). Remove `task_id?,` from Returns list (line 146).
- `agent/mcp_tools/bridge_tools.py` — **edit** — Module docstring line 19: delete `  globus_task_status             — async-Globus poll`.
- `agent/mcp_tools/__init__.py` — **edit** — Line 57: drop `, globus_task_status` from the trailing noqa comment listing bridge_tools exports.
- `agent/skills/transfer_providers.py` — **remove_symbol** — Delete `globus_task_status` at lines 1010-1080 (incl. the `# Public — the async-task polling primitive uses this from outside.` comment on 1010). Keep the `# Factory` block at 1083+ intact.
- `agent/skills/transfer_providers.py` — **edit** — _run_transfer: delete the async early-return branch lines 550-566 (`# 2) Either return immediately (async) or poll to terminal (sync).` through the closing `)` of the loop(...) call). Retitle the surviving comment to `# 2) Poll to terminal.` Remove `async_return: bool,` from the signature (line 535).
- `agent/skills/transfer_providers.py` — **edit** — MANDATORY COLLATERAL — rewrite the `transfer.globus_sync_wait_exceeded` terminal at lines 600-611. Its `error` text (601-607) and `hint` (610) both name `async_return=True` + `globus_task_status`, which will no longer exist. This message FIRED TWICE in real usage (see evidence). Replace with a remedy that survives: the Globus task keeps running server-side, so name the task_id and point at `globus task show <task_id>` / `globus task wait <task_id>` by hand, and re-run the transfer once it completes. Do NOT leave a dangling reference to a deleted tool.
- `agent/skills/transfer_providers.py` — **edit** — Remove `async_return: bool = False,` from the TransferProvider ABC upload_one (line 106) + download_one (line 116) and fix the ABC docstring at 108 ("`async_return` is meaningful only for..."). Remove from ScpHeadNodeProvider.upload_one (165) / download_one (247) and delete the now-moot comments at 167 and 249. Remove from GlobusProvider.upload_one (443) / download_one (454) and their `async_return=async_return,` forwards at 450 / 461.
- `agent/skills/transfer_providers.py` — **edit** — Delete stale comments: lines 360-361 ("With async_return=True we return immediately after submit with the task_id; the caller polls later via the globus_task_status MCP tool.") and line 428 ("set async_return=True and poll via globus_task_status themselves.").
- `agent/skills/transfer.py` — **remove_symbol** — Delete the whole `# Async confirmation — reconcile a `submitted` manifest to its real outcome` section, lines 667-743: the banner comment (667-669), `_find_manifest_by_task_id` (671-685) and `finalize_manifest_for_task` (688-743). No callers remain once transfer_providers.py:1072 is removed.
- `agent/skills/transfer.py` — **edit** — _do_transfer: delete the async submit branch lines 1055-1067 (`# Async submit — no bytes yet; manifest reflects submitted state.` through the closing `)` of the `loop("transfer.submitted_async", ...)` return). Remove `async_return=async_globus,` at 1038 (upload_one) and 1044 (download_one).
- `agent/skills/transfer.py` — **edit** — Remove `async_globus: bool = False,` from upload() signature (823) and its forward at 845; from download() signature (856) and its forward at 871; and `async_globus: bool,` from _do_transfer() signature (882). Keep `timeout` and `access_path`.
- `agent/skills/transfer.py` — **edit** — _OUTCOME_VERB (532-541): delete the two `pending` rows, lines 535-536 (`("pending", "upload"): "submitted",` / `("pending", "download"): "submitted",`). _VERIFIED_PHRASE (543-547): delete line 546 `"globus_pending": "pending — Globus verifies on completion",`. Delete the `if outcome == "submitted":` branch in the summary builder at line 654 (and its body). KEEP the `task_id` param of _write_transfer_manifest (563) — the SYNC globus path still records `globus_task_id` (transfer.py:1083).
- `tests/integration/honesty/L14_compute_env_safety/test_globus_provider.py` — **remove_symbol** — DELETE class TestAsyncReturn (lines 579-603, incl. `def test_async_return_skips_polling` 581-603).
- `tests/integration/honesty/L14_compute_env_safety/test_globus_provider.py` — **remove_symbol** — DELETE the `# Async confirmation ...` banner (643-644) and class TestManifestReconciliation (646-745): `_write_submitted` (652-663), `test_succeeded_finalizes_submitted_to_uploaded` (666-693), `test_failed_finalizes_submitted_to_failed` (696-720), `test_active_poll_leaves_manifest_submitted` (723-745).
- `tests/integration/honesty/L14_compute_env_safety/test_globus_provider.py` — **remove_symbol** — DELETE the `# globus_task_status — the public async-poll primitive` banner (749-750) and class TestTaskStatusPrimitive (752-807): `test_task_status_happy_path` (754-775), `test_task_status_refuses_bad_uuid` (786-798), `test_task_status_refuses_non_globus_env` (801-807).
- `tests/integration/honesty/L14_compute_env_safety/test_globus_provider.py` — **edit** — UPDATE (do NOT delete) class TestSubmitArgvShape (63-116): `test_upload_argv_is_local_to_remote` (65-95, async_return=True at line 80) and `test_download_argv_is_remote_to_local` (98-116, async_return=True at line 111) pass async_return ONLY to exit after submit without polling. They assert the argv shape of the LIVE sync globus path and must survive. Rewrite: drop the async_return kwarg and monkeypatch `GlobusProvider._task_show` to return `{"status": "SUCCEEDED", ...}` so the sync poll loop terminates on the first iteration. Delete the `# async_return so we exit after submit (no poll)` comment at line 79.
- `tests/integration/honesty/L14_compute_env_safety/test_globus_provider.py` — **edit** — Module docstring: delete the two bullets at lines 17-19 ("async_return=True returns the task_id IMMEDIATELY without ... pointing the caller at globus_task_status.") and 22 ("globus_task_status (the public poll primitive) refuses smuggled ..."). KEEP class TestSyncWait (123-216), TestPermissionDeniedClassifier (223-474), TestGlobusPreflightIsSshFree (481-572), TestCliMissing (610-639), TestFactoryDispatch (814-834) — all exercise the live sync path.
- `tests/test_c4_crash_safety.py` — **edit** — Remove the battery entry at lines 113-114: `("globus_task_status", B.globus_task_status, dict(project_name=BAD_PROJ, compute_env_name=BAD_ENV, task_id="not-a-uuid"), True),`. Leave the surrounding upload/download/cluster_module_avail/cluster_job_status entries.
- `CLAUDE.md` — **edit** — Line 77: change the `upload`/`download` signature to drop `, async_globus=False` (both), and delete the sentence "`async_globus=True` returns a task_id; poll via `globus_task_status`." Keep the zone routing, wire-protocol, manifest and PERMISSION_DENIED-diagnostic text. Also drop `submitted`/ from the outcome enumeration "(`uploaded`/`downloaded`/`submitted`/`refused`/`failed`)".
- `CLAUDE.md` — **edit** — Line 78: DELETE the entire `globus_task_status(project, env, task_id)` table row.
- `CLAUDE.md` — **edit** — Line 197: DELETE the paragraph "For huge transfers (multi-GB .sif images, full datasets), every primitive accepts `async_globus=True` ... The escape hatch when the agent's ~10-minute stream-watchdog would kill a sync wait." NOTE: if the _SYNC_WAIT_S_DEFAULT ceiling is addressed in the same commit, replace this paragraph with the real ceiling + remedy rather than leaving a silent gap.
- `scripts/render_decision_tree.py` — **edit** — Lines 217-220: delete the `N("globus async → 'submitted' (task_id) → MUST confirm via globus_task_status", kind="q", outcome="loop", *[...])` node and its two children (later SUCCEEDED -> 'uploaded' / later FAILED -> 'failed'). Keep the sibling `globus sync → SUCCEEDED` (216) and the consent/accessible-folders node (221).
- `docs/scenario_decision_tree.md` — **edit** — REGENERATE from scripts/render_decision_tree.py after the edit above. Removes lines 234-236 (the `globus async → "submitted"` branch and its two sub-branches).
- `docs/scenario_decision_tree.json` — **edit** — REGENERATE alongside scenario_decision_tree.md from scripts/render_decision_tree.py.
- `docs/outcomes_ledger.json` — **edit** — REGENERATE via `python3 scripts/extract_outcomes.py` (agent/mcp_tools + agent/skills are in SWEEP_DIRS, so the removed terminals drop automatically). Removes 8 entries: transfer.submitted_async (~1863), transfer.globus_submitted_async (~1873), transfer.task_status_ok (~2603), globus_task_status.project_or_env_not_found (~2863), globus_task_status.access_denied (~2873), transfer.task_status_not_ssh (~4753), transfer.task_status_not_globus (~4763), transfer.task_status_bad_uuid (~4773). Required: tests/test_outcome_tags.py::test_dashboard_renders_every_terminal_deterministically asserts the dashboard has exactly one <li> per ledger entry, so a stale ledger keeps rendering dead terminals. NOTE: if the sync_wait_exceeded message is rewritten, its code transfer.globus_sync_wait_exceeded stays — do not drop it.
- `docs/outcomes_dashboard.html` — **edit** — REGENERATE via scripts/render_outcomes_dashboard.py after the ledger regen (it is a pure projection of the ledger).

### tests affected

- tests/integration/honesty/L14_compute_env_safety/test_globus_provider.py::TestAsyncReturn::test_async_return_skips_polling — DELETE (whole class TestAsyncReturn, 579-603)
- tests/integration/honesty/L14_compute_env_safety/test_globus_provider.py::TestManifestReconciliation::test_succeeded_finalizes_submitted_to_uploaded — DELETE
- tests/integration/honesty/L14_compute_env_safety/test_globus_provider.py::TestManifestReconciliation::test_failed_finalizes_submitted_to_failed — DELETE
- tests/integration/honesty/L14_compute_env_safety/test_globus_provider.py::TestManifestReconciliation::test_active_poll_leaves_manifest_submitted — DELETE (plus helper _write_submitted, 652-663; whole class 646-745)
- tests/integration/honesty/L14_compute_env_safety/test_globus_provider.py::TestTaskStatusPrimitive::test_task_status_happy_path — DELETE
- tests/integration/honesty/L14_compute_env_safety/test_globus_provider.py::TestTaskStatusPrimitive::test_task_status_refuses_bad_uuid — DELETE (parametrized over bad_task_id)
- tests/integration/honesty/L14_compute_env_safety/test_globus_provider.py::TestTaskStatusPrimitive::test_task_status_refuses_non_globus_env — DELETE (whole class 752-807)
- tests/integration/honesty/L14_compute_env_safety/test_globus_provider.py::TestSubmitArgvShape::test_upload_argv_is_local_to_remote — UPDATE, do NOT delete: uses async_return=True (line 80) only to skip the poll; asserts LIVE sync argv shape. Monkeypatch _task_show -> SUCCEEDED instead.
- tests/integration/honesty/L14_compute_env_safety/test_globus_provider.py::TestSubmitArgvShape::test_download_argv_is_remote_to_local — UPDATE, same reason (async_return=True at line 111)
- tests/test_c4_crash_safety.py::_battery — UPDATE: remove the ('globus_task_status', B.globus_task_status, ...) entry at lines 113-114; the battery drives every bridge MCP tool, so a dangling attr ref is an AttributeError at collection
- tests/test_outcome_tags.py::test_dashboard_renders_every_terminal_deterministically — WILL FAIL unless docs/outcomes_ledger.json + docs/outcomes_dashboard.html are regenerated (asserts one <li> per ledger entry and that every ledger code appears in the HTML)
- tests/test_outcome_tags.py::test_seaworthy_scope_is_well_defined_and_measurable — recheck after ledger regen (iterates the ledger; transfer.task_status_ok is outcome=proven, therefore load-bearing, and disappears from the meter denominator)
- tests/test_outcome_tags.py::test_fully_tagged_files_stay_fully_tagged — NOT affected (ratchet only fails on NEW untagged raw dict returns; agent/skills/transfer.py and transfer_providers.py are not in FULLY_TAGGED anyway)
- tests/integration/honesty/L14_compute_env_safety/test_phase2_schema_load.py — NOT affected: tests the globus CONFIG schema (sync). KEEP ALL (test_globus_with_all_fields_loads, test_globus_type_without_globus_block_refused, test_globus_block_missing_required_field_refused, test_globus_block_bad_uuid_refused, test_globus_block_empty_name_refused, test_globus_block_unknown_subkey_refused).
- tests/test_acquire_data.py — NOT affected: its globus reference (line 5) is the globus-accessible staging-dir guard, unrelated to async.
- tests/test_transfer_download_paths.py — NOT affected: no async/globus_task_status reference found.

### docs affected

- CLAUDE.md:77 — upload/download signature `async_globus=False` + "poll via globus_task_status" sentence + `submitted` in the outcome enumeration
- CLAUDE.md:78 — the entire globus_task_status primitive table row (DELETE)
- CLAUDE.md:197 — the "For huge transfers ... async_globus=True ... The escape hatch when the agent's ~10-minute stream-watchdog would kill a sync wait" paragraph (DELETE; consider replacing with the real 540s ceiling)
- scripts/render_decision_tree.py:217-220 — the globus-async decision node + its 2 children (source of the generated trees)
- docs/scenario_decision_tree.md:234-236 — REGENERATE
- docs/scenario_decision_tree.json — REGENERATE
- docs/outcomes_ledger.json — REGENERATE (8 entries drop: lines ~1863, ~1873, ~2603, ~2863, ~2873, ~4753, ~4763, ~4773)
- docs/outcomes_dashboard.html — REGENERATE (pure ledger projection; test_dashboard_renders_every_terminal_deterministically enforces parity)
- docs/hpc_bridge_phase2.md:492 — "Returns Globus task_id; agent polls with `globus_task_status` (a small secondary primitive)". This is inside a DEFERRED design section for a never-built refdata-pull primitive (the surrounding text says "Defer rationale: ..."). Historical design doc — update the line or leave; not a live contract.
- docs/stress_findings.md:179 — "bridge.globus_task_status — same KeyError, inlined + unguarded → refused." A DATED findings log of fixed crashes. LEAVE AS-IS (rewriting history in a log is worse than a stale name).
- docs/audit_2026-07-16.md:230-234 and 724-731 — the audit that raised this claim. LEAVE AS-IS (dated record).
- scripts/seaworthy_scope.py:36 — the token "globus" in a name list. LEAVE: it matches the surviving sync-globus terminals, not async.

### surprises

FOUR things worth your attention; the first is the one that should change the commit.

1. *** THE ESCAPE HATCH IS THE ONLY REMEDY FOR A REAL, REACHABLE FAILURE — AND IT HAS ALREADY FIRED TWICE. *** The claim's premise ("largest transfer 1.0GB in 1m25s vs ~600s watchdog, so the need never materialized") is wrong on both numbers. The cap is `_SYNC_WAIT_S_DEFAULT = 540` (transfer_providers.py:429), applied as `wait_cap = min(timeout, 540)` (:569), and `timeout` is NOT exposed on the MCP upload/download surface — so an agent can NEVER wait past 540s. That cap ALREADY BOUND TWICE in production (2026-07-06T23-16-31Z_download_2ff67f7581.json, 2026-07-07T00-43-06Z_download_f996bfa478.json, both outcome "failed", both took 9m09s), and both times the error text told the operator to do the exact thing this deletion removes. Measured globus throughput (888.4MB/94s ≈ 9.4 MB/s) makes 540s a hard ~5.0 GB ceiling, and stage_apptainer.py:174 pushes .sif/.tar images through this path — a CUDA/GPU image over ~5GB hits the wall with, post-deletion, no remedy named at all. DELETE is still right (async is broken 3 ways, was never used once in 92 transfers, and would NOT have rescued either real failure — a stalled ACTIVE task stays stalled). But do not ship the deletion alone: in the SAME commit, rewrite `transfer.globus_sync_wait_exceeded` (transfer_providers.py:600-611) to name a remedy that still exists (the task survives server-side: `globus task wait <task_id>` by hand, then re-run), and decide deliberately whether to raise _SYNC_WAIT_S_DEFAULT or plumb `timeout` to the MCP surface. Otherwise you replace a broken escape hatch with a dead-end that points at two deleted symbols — a fresh instance of the exact "the copy that runs is the stale one" disease this audit is subtracting.

2. NOT DEAD, JUST UNUSED — classify it honestly. `globus_task_status` IS an @mcp.tool (bridge_tools.py:192) and `async_globus` IS an exposed MCP parameter (bridge_tools.py:77, :133). Nothing internal ever passes async_return=True (the only two call sites in the repo are test lines 80 and 111). So this is reachable-from-MCP surface with zero real use — a deliberate feature removal, not dead-code cleanup. It is defensible under "existing installs are NOT precious", but it is a user-visible tool-surface change (67 → 66 @mcp.tool), not a silent tidy. Also note the task brief's "65 @mcp.tool functions" is off by two: actual count is 67.

3. TWO TESTS ARE TRAPS — they LOOK async but guard the live sync path. TestSubmitArgvShape::test_upload_argv_is_local_to_remote and ::test_download_argv_is_remote_to_local pass `async_return=True` purely as a cheap way to exit after submit without polling, then assert the `globus transfer` argv shape (endpoint order, --encrypt-data, --format json, --label) of the LIVE sync provider. Deleting them with the rest of the async tests would silently drop the only coverage of the argv construction that all 92 real transfers depend on. They must be REWRITTEN (monkeypatch `_task_show` → SUCCEEDED), not removed.

4. THE CLAIM'S GUARDRAILS ARE CORRECT AND CONFIRMED. The sync globus provider is not merely live, it is the ONLY transport ever used: all 92 records are `"transport": "globus"`, and projects_access.yaml:140-146 declares a real env with `data_transfer: type: globus` (UNC/Longleaf endpoint UUIDs). scp_head_node remains the reachable default via `get_transfer_provider` (transfer_providers.py:1098) for any ssh env without a data_transfer block. Neither is touched by this edit set. One adjacent nuance the claim missed: keep the `task_id` parameter of `_write_transfer_manifest` (transfer.py:563) — it reads as async-only but the SYNC globus journal still records `globus_task_id` (transfer.py:1083), which is what made the two 549s failures diagnosable at all. Removing it would delete real forensic value.

---

## resolver.route + env_freeze.build_env_from_tools

- **verdict** `DELETE` · **LOC** 186 · claim accurate: partly · MCP-reachable: False · risk: low
- **refutation**: upheld

### evidence
```
VERDICT CORRECT; one number wrong. Real LOC (ast): resolver.py:736-832 route = 97 (claim's ~97 OK); env_freeze.py:703-791 build_env_from_tools = 89, NOT 189. Combined 186 (+8 for _R_TIERS block env_freeze.py:693-700 = 194).

(1) route() callers — exactly ONE in agent/: `grep -rn "\.route(" --include=*.py agent/` -> agent/skills/env_freeze.py:748 (inside build_env_from_tools). Rest are tests. No __all__ in either module; no dynamic dispatch (`grep getattr(.*rout|"route"` -> only env_freeze.py:770 `stage="route"`, a string literal).

(2) build_env_from_tools callers — ZERO in agent/ and scripts/:
  grep -rn "build_env_from_tools" agent/mcp_server.py agent/mcp_tools/ -> NONE
  callers = tests/test_invariants.py:4605,4627,4631,4749 + tests/integration/honesty/L15_real_build/test_real_container_build.py:53 (tests only).
  MCP surface = 69 @mcp.tool (claim said 65). Only resolver entry from MCP is agent/mcp_tools/env_tools.py:132 `_ms._resolver.resolve(...)` (resolve_tool) — resolve, never route.

(3) SUBPROCESS class checked (per instructions): agent/mcp_server.py:484 spawns `python -m agent.skills.freeze_runner`; freeze_runner.py:71-73 does `from agent.mcp_server import freeze; result = freeze(**args)` — reaches freeze() ONLY, never build_env_from_tools. scripts/capability_probe.py:115 calls `resolver.resolve(...)` only, never route.

(4) "never registers in EnvCache" CONFIRMED: `EnvCache.register` (freeze.py:578) has exactly 2 call sites — agent/skills/freeze_from_image.py:213 and agent/mcp_tools/freeze_tools.py:526. build_env_from_tools returns eb.run() + eb.request_key() (env_freeze.py:789-791) with no register. run_step_in_container/stage_apptainer_image resolve via env_cache.lookup_verified (stage_apptainer.py:327) -> the returned request_key is a dead key.

(5) "writes no deliverables" CONFIRMED: `grep -c "ENV.html|attestation|render_" agent/skills/env_build.py` = 0. Deliverables are written in freeze_tools.py / freeze_from_image.py only.

(6) "route() DEFERS on author_image/authors_recipe" CONFIRMED at resolver.py:818-829: returns {"kind":"defer"} naming freeze_from_image / build_env_from_authors_recipe as the real executors. build_env_from_tools maps any non-conda/pip/tool kind to `refused("build.route_no_tier")` (env_freeze.py:769) -> the declarative path structurally cannot execute the reliability gate's top two tiers.

(7) L15 repoint VERIFIED EQUIVALENT (executed, no build): build_env_image({}, name=..., conda_deps=["pigz"], primary_tools=["pigz"]) ->
  _freeze.non_conda_installs({}) = [] ; installed_packages({}) = [] ; plan_conda(['pigz'],[]) = ['pigz'] ; ensure_python_for_pip(...) = ['pigz'] ; verify = [('pigz', _conda_presence_check('pigz'))] (env_freeze.py:666) — the identical EnvBuild plan, and it returns the same BuildResult + result["request_key"] (env_freeze.py:688-690). Every existing L15 assertion (success/image/image_digest/content_digest/verifications/honesty_violations/check_build/validation_locus) holds unchanged.

(8) Share
```

### edits

- `agent/skills/resolver.py` — **remove_symbol** — Delete `def route(decision, platform='linux/amd64')` — lines 736-832 inclusive (97 lines), from `def route(` through the final `return {"kind": "defer", ... "no automatable tier found"}` block ending at 832. Its `from agent.skills import install_commands as ic` is a function-local import (line 750) and dies with it. Do NOT touch _pick_platform_asset (resolver.py:353) — still used by resolve() at :430,:443. Next symbol `def resolve(` starts at 835.
- `agent/skills/env_freeze.py` — **remove_symbol** — Delete `def build_env_from_tools(...)` — lines 703-791 inclusive (89 lines), from `def build_env_from_tools(` through `    return result` at 791. KEEP the import at line 31 (`from agent.skills import resolver as _resolver`) — still used at :288,:289,:310,:311 for resolve_linux_asset/sha256_of_url. KEEP `Callable` in the typing import at :27 (still used at :288-311).
- `agent/skills/env_freeze.py` — **remove_symbol** — Delete `_R_TIERS` and its 7-line comment header — lines 693-700 inclusive (blank line 692 + comment 693-699 + `_R_TIERS = {"cran", "bioconductor", "r_github"}` at 700). It becomes dead on removal of build_env_from_tools: its ONLY consumer was env_freeze.py:766. The LIVE R-toolchain injection is a separate path — env_freeze.py:483 via _TOOLCHAIN_SPECS['r_install'] keyed on install_method type — and is unaffected. Do NOT delete _TOOLCHAIN_SPECS (env_freeze.py:38).
- `tests/integration/honesty/L15_real_build/test_real_container_build.py` — **edit** — REPOINT, DO NOT DELETE (repo's only genuine container-build test). Line 53: replace `res = env_freeze.build_env_from_tools(name="bioinf_l15_realbuild", tools=["pigz"])` with `res = env_freeze.build_env_image({}, name="bioinf_l15_realbuild", conda_deps=["pigz"], primary_tools=["pigz"])`. Verified equivalent: identical EnvBuild plan (conda ['pigz'], verify [('pigz', _conda_presence_check('pigz'))]) and same BuildResult + request_key. All assertions on lines 55-79 hold unchanged. Also rename the test fn (line 44) `test_real_build_from_tools_is_honest_and_validated_in_image` -> `test_real_build_is_honest_and_validated_in_image` and drop 'from_tools' from the docstring.
- `tests/integration/honesty/L15_real_build/conftest.py` — **edit** — Line 4 docstring: `A real `freeze()` / `build_env_from_tools()` writes to two singletons that are` -> replace `build_env_from_tools()` with `build_env_image()`. Cosmetic; no code change.
- `tests/test_invariants.py` — **remove_symbol** — Delete `test_resolver_route_maps_every_tier` — lines 3644-3675 inclusive.
- `tests/test_invariants.py` — **remove_symbol** — Delete `test_every_rscript_shelling_tier_injects_the_r_toolchain` — lines 3678-3712 inclusive. NOTE: this is the 2026-07-16 audit's own r_github/_R_TIERS regression lock; it guards ONLY the dead route()->_R_TIERS path (the live install_method-keyed R injection at env_freeze.py:483 is a different code path), so it dies with the deletion and locks nothing live.
- `tests/test_invariants.py` — **remove_symbol** — Delete `test_build_env_from_tools_assembles_plan_across_tiers` — lines 4581-4620 inclusive.
- `tests/test_invariants.py` — **remove_symbol** — Delete `test_build_env_from_tools_refuses_ambiguous_and_unroutable` — lines 4623-4633 inclusive.
- `tests/test_invariants.py` — **remove_symbol** — Delete `test_build_env_from_tools_bare_name_for_unpinned_conda` — lines 4733-4751 inclusive.
- `tests/test_synthesis.py` — **remove_symbol** — Delete `test_route_synthesis_hands_off_to_agent` — lines 254-259 inclusive.
- `tests/test_synthesis.py` — **remove_symbol** — Delete `test_route_spack_is_a_tool_spec` — lines 509-511 inclusive.
- `tests/test_synthesis.py` — **edit** — PARTIAL EDIT — KEEP the test. In `test_r_github_tier_beats_synthesis_for_r_package` (lines 331-341), delete ONLY the trailing route assertion: line 340 comment `# route() yields a real Rscript remotes::install_github spec` and line 341 `assert "install_github" in (resolver.route(d)["spec"]["command"])`. Lines 331-339 assert resolve() tier ranking (d['chosen']=='r_github', install_call), which is LIVE — must survive.
- `tests/test_synthesis.py` — **edit** — After removing the three route() usages above, verify the module-level `from agent.skills import resolver` (line 232) is still needed — it is (resolver.resolve is used elsewhere in the file). Keep it.
- `docs/outcomes_ledger.json` — **edit** — Regenerate, do not hand-edit: run `python scripts/extract_outcomes.py` (writes docs/outcomes_ledger.json, per script line 149). Drops the two now-deleted terminals at lines 3712-3721 (`build.resolve_ambiguous`, func build_env_from_tools) and 3722-3731 (`build.route_no_tier`). Both are outcome=refused/source=helper/named_in_test=false -> not load-bearing, so scripts/seaworthy_scope.py meter is unchanged. NB the ledger's recorded `where` values (env_freeze.py:690 / :713) are ALREADY STALE vs HEAD (actual refused() calls sit at :746 and :769), confirming the ledger is a generated artifact that drifted. Re-render the dashboard afterwards (scripts/render_outcomes_dashboard.py) or tests/test_outcome_tags.py::test_dashboard_renders_every_terminal_deterministically will compare a fresh ledger against a stale HTML.
- `docs/audit_2026-07-16.md` — **edit** — OPTIONAL / historical record — recommend LEAVING lines 172, 199, 314, 385-389, 705 as-is (they are the audit narrative that justified this deletion; rewriting history obscures provenance). If the repo convention is to close out audit items, tick item 15 at line 314 (`expose or delete build_env_from_tools`) as DONE-by-deletion. No CLAUDE.md edit needed — its build_env_from_tools advertisement was already removed at commit fb57ac0 (verified: `grep -c build_env_from_tools CLAUDE.md` = 0).

### tests affected

- tests/integration/honesty/L15_real_build/test_real_container_build.py::test_real_build_from_tools_is_honest_and_validated_in_image — REPOINT to env_freeze.build_env_image (verified equivalent), do NOT delete: repo's only genuine (unmocked) container-build test
- tests/integration/honesty/L15_real_build/conftest.py — docstring-only edit (line 4)
- tests/test_invariants.py::test_resolver_route_maps_every_tier — DELETE (3644-3675)
- tests/test_invariants.py::test_every_rscript_shelling_tier_injects_the_r_toolchain — DELETE (3678-3712)
- tests/test_invariants.py::test_build_env_from_tools_assembles_plan_across_tiers — DELETE (4581-4620)
- tests/test_invariants.py::test_build_env_from_tools_refuses_ambiguous_and_unroutable — DELETE (4623-4633)
- tests/test_invariants.py::test_build_env_from_tools_bare_name_for_unpinned_conda — DELETE (4733-4751)
- tests/test_synthesis.py::test_route_synthesis_hands_off_to_agent — DELETE (254-259)
- tests/test_synthesis.py::test_route_spack_is_a_tool_spec — DELETE (509-511)
- tests/test_synthesis.py::test_r_github_tier_beats_synthesis_for_r_package — KEEP, drop trailing lines 340-341 only (resolve() ranking assertions are live)
- tests/test_outcome_tags.py::test_dashboard_renders_every_terminal_deterministically — NOT edited, but will FAIL if the ledger is regenerated without re-rendering docs/outcomes_dashboard.html (it asserts html li-count == len(ledger))
- tests/test_invariants.py::test_conda_presence_check_validates_libraries_honesty_safe (4693), ::test_ensure_python_for_pip_injects_python_and_pip_when_flag_bearing (2391), ::test_pick_platform_asset_disambiguates_os_and_arch (2210) — UNAFFECTED (shared helpers survive)

### docs affected

- docs/outcomes_ledger.json — regenerate via `python scripts/extract_outcomes.py` (2 terminals removed: build.resolve_ambiguous @3712-3721, build.route_no_tier @3722-3731); then re-render docs/outcomes_dashboard.html
- docs/outcomes_dashboard.html — re-render via scripts/render_outcomes_dashboard.py to stay in sync with the regenerated ledger
- docs/audit_2026-07-16.md:172,199,314,385-389,705 — historical audit narrative; recommend LEAVE (optionally tick item 15 @314 as done-by-deletion)
- CLAUDE.md — NO EDIT NEEDED: the `build_env_from_tools` advertisement in the freeze row was already removed at commit fb57ac0 (grep count = 0 at HEAD)

### surprises

FIVE things worth flagging.

1. LOC CLAIM WRONG (verdict unaffected): build_env_from_tools is 89 LOC (env_freeze.py:703-791), not ~189. route is 97 (correct). Combined 186; +8 with the _R_TIERS block = 194 lines removed.

2. THE CLAIM MISSED AN ADJACENT DEAD SYMBOL: `_R_TIERS` (env_freeze.py:700, comment 693-699) has exactly ONE consumer — env_freeze.py:766, inside build_env_from_tools — so it dies with the deletion. This is the strongest argument FOR deleting, not against: _R_TIERS is a SECOND, resolver-tier-keyed copy of the R-toolchain-injection truth, duplicating the LIVE install_method-keyed copy at env_freeze.py:483 (`_TOOLCHAIN_SPECS.get(x["type"])`). Textbook ONE TRUTH IN N PLACES — and the copy that drifted is the dead one. Its own comment records this: the 2026-07-16 audit had to FIX `r_github` into _R_TIERS after it silently baked images with no R. That audit fix, plus its regression lock (test_every_rscript_shelling_tier_injects_the_r_toolchain, 3678-3712), only ever protected the unreachable path. Deleting removes the duplicate and the maintenance obligation.

3. THE CLAIM'S DOC RADIUS IS STALE: CLAUDE.md no longer mentions build_env_from_tools — `git log -S` shows the "declarative sibling ... straight from tool NAMES (resolve→route→build)" line was removed at fb57ac0 ("tier 5"), one of the two commits the claim predates. So the "advertised but unreachable" doc gap is ALREADY closed; only the generated ledger still names it. No CLAUDE.md edit.

4. THE LEDGER IS ITSELF DRIFTED — same disease, in the audit's own instrument: docs/outcomes_ledger.json records these terminals at `env_freeze.py:690` and `:713`, but at HEAD they live at :746 and :769. The ledger is generated (scripts/extract_outcomes.py) and no test re-extracts from code to compare — tests/test_outcome_tags.py only reads the JSON file. So a stale ledger is invisible to CI. Worth a separate finding: there is no code-vs-ledger drift-lint, despite the ledger being the coverage dashboard's source of truth.

5. NOT A SURPRISE BUT WORTH CONFIRMING: I specifically checked the subprocess class of caller you warned about. mcp_server.py:484 spawns `python -m agent.skills.freeze_runner`, and freeze_runner.py:71-73 imports and calls ONLY `freeze` — it does not reach build_env_from_tools. scripts/capability_probe.py imports resolver but calls only `resolve` (line 115), never `route`. So unlike the scp_head_node / GPU false positives, this one really is dead: zero non-test callers by every access path (import, dynamic dispatch, __all__, subprocess, scripts/).

CONFIDENCE NOTE ON THE ONE THING THAT COULD HURT: L15 is the repo's only unmocked container-build test, and its provenance docstring says writing it surfaced a real crash (pigz's banner emitting gzip magic → UnicodeDecodeError). I did not merely assert the repoint is possible — I executed the pure functions and confirmed build_env_image({}, name=..., conda_deps=["pigz"], primary_tools=["pigz"]) yields the byte-identical EnvBuild plan and the same result["request_key"]. The repoint requires Docker to run for real, which I could not do read-only; run `pytest -m integration_docker tests/integration/honesty/L15_real_build/` after the edit to confirm on real bytes.

---

## env_manager_retired_version_probes

- **verdict** `DELETE` · **LOC** 170 · claim accurate: yes · MCP-reachable: False · risk: low
- **refutation**: upheld

### evidence
```
CLAIM CONFIRMED at HEAD (40cf9ca, fix/tier0-honesty-gates). LOC is EXACTLY 170 (AST, pure def bodies; 177 incl. trailing blank separators) — the claim's "~170" is precise.

[1] ZERO CALLERS. Repo-wide grep (*.py/*.md/*.yaml/*.json/*.sh, excl. envs/.git/data) for all 7 symbols returns ONLY self-definitions + two intra-block calls that die with the block:
  - env_manager.py:1420  list_conda_packages -> list_conda_package_records (both deleted)
  - env_manager.py:1496  list_explicit_conda_packages -> export_environment_yml (caller deleted; callee LIVE, survives)
  install_pip: `grep -rn "install_pip("` and `grep -rnE "\.install_pip\b"` => NO call sites. All 30+ "install_pip" hits are the SEPARATE live MCP tool `install_pip_package` (agent/mcp_tools/env_tools.py:1083). env_manager.py:296 is a DOCSTRING mention inside apply(); apply() does NOT dispatch to it.
  tests/test_c4_crash_safety.py:165 uses `E.install_pip_package` where E = agent.mcp_tools.env_tools (line 101) — unrelated.

[2] NOT REACHABLE FROM MCP. 69 @mcp.tool functions; none reach these. No dynamic dispatch: getattr/quoted-name greps return NOTHING. No subprocess/`python -m` caller — agent/skills/freeze_runner.py (spawned at mcp_server.py:484) imports only `from agent.mcp_server import freeze` and never touches EnvManager. No callers in scripts/ (grep => NONE) or tests/integration/ (grep => NONE).

[3] LIVE SIBLING CORRECTLY EXCLUDED. Claim is right that export_environment_yml is live: freeze_tools.py:412 `_ms._env_mgr.generate_lock` -> generate_lock (1633) -> generate_conda_lock (1579) -> export_environment_yml (1516). Post-deletion it retains exactly 1 live caller. lock_engine/generate_lock/generate_conda_lock also live (tests/test_invariants.py:3047,4550,4662).

[4] GIT PICKAXE CORROBORATES PROVENANCE. `git log -S` shows ONE commit orphaned all five probes simultaneously: c4eca59 "Phase E: retire the host build path; spec_writer is Layer-2 only" — precisely the retirement CLAUDE.md describes.

[5] EMPIRICAL CONTROLLED DELETION (the proof). Two git-archive HEAD trees, md5-verified identical to HEAD, deletion applied to mutant ONLY (`diff -rq` confirmed env_manager.py is the sole differing source file):
  collection:  base 698  ==  mut 698
  full suite:  base 2 failed, 695 passed, 1 skipped
               mut  2 failed, 695 passed, 1 skipped   <-- IDENTICAL
  The 2 failures are PRE-EXISTING in base and unrelated (test_c4_crash_safety[upload]/[download]: FileNotFoundError ~/.bioinf/projects_access.yaml, a gitignored user file absent from a git-archive copy).
  Mutated module imports cleanly; all 7 attrs gone; export_environment_yml/generate_conda_lock/generate_lock/lock_engine retained.

[6] NO TEST NAMES ANY OF THE 7 SYMBOLS. env_manager.py IS in tests/test_outcome_tags.py FULLY_TAGGED (line 213), but the ratchet test_fully_tagged_files_stay_fully_tagged only flags UNTAGGED terminals — removing 2 tagged terminals passes. docs/terminal_coverage.json marks BOTH install_pip terminals (env_
```

### edits

- `agent/skills/env_manager.py` — **remove_symbol** — APPLY IN DESCENDING LINE ORDER so earlier ranges stay valid. (1) r_package_version lines 1656-1683 (28 LOC).
- `agent/skills/env_manager.py` — **remove_symbol** — (2) export_explicit_lock lines 1562-1577 (16 LOC).
- `agent/skills/env_manager.py` — **edit** — (3) Line 1583, inside LIVE generate_conda_lock docstring: remove the dangling reference to the deleted symbol — 'environment.yml — the portable lock artifact (vs export_explicit_lock, which is bit-exact but single-arch).' Drop the parenthetical. Do NOT touch the function body.
- `agent/skills/env_manager.py` — **remove_symbol** — (4) list_explicit_conda_packages lines 1486-1514 (29 LOC). NOTE: its body calls export_environment_yml at :1496 — the CALLER dies, the callee is LIVE and MUST remain (def at 1516-1560).
- `agent/skills/env_manager.py` — **remove_symbol** — (5) list_pip_packages lines 1457-1484 (28 LOC).
- `agent/skills/env_manager.py` — **remove_symbol** — (6) list_conda_package_records lines 1422-1455 (34 LOC). Sole caller is list_conda_packages, deleted next.
- `agent/skills/env_manager.py` — **remove_symbol** — (7) list_conda_packages lines 1413-1420 (8 LOC).
- `agent/skills/env_manager.py` — **remove_symbol** — (8) install_pip lines 255-281 (27 LOC). Contains the 2 outcome-tagged terminals: proven('env_manager.pip_installed') @266 and broke('env_manager.pip_install_failed') @274. Do NOT confuse with the LIVE MCP tool install_pip_package (agent/mcp_tools/env_tools.py:1083).
- `agent/skills/env_manager.py` — **edit** — (9) Line 296, inside LIVE apply() docstring: 'install()/install_pip() (and, as the re-spine proceeds, every other install tier) delegate execution here' -> 'install() (and, as the re-spine proceeds, every other install tier) delegates execution here'. Docstring only; no behavior change.
- `docs/outcomes_ledger.json` — **edit** — REQUIRED REGENERATION (generated artifact, keyed by file:line). Two entries become phantoms pointing at deleted code: {'code':'env_manager.pip_installed','where':'agent/skills/env_manager.py:266','end_line':273} and {'code':'env_manager.pip_install_failed','where':'agent/skills/env_manager.py:274','end_line':281}. ALSO: removing install_pip (255-281) shifts EVERY remaining env_manager terminal below line 281 up by 27 (70 of the 72 env_manager entries, e.g. env_manager.binary_sha256_mismatch where:1193 -> 1166). Ledger total 515 -> 513. Regenerate with scripts/extract_outcomes.py. NOTHING in the test suite enforces this — it will drift silently if skipped.
- `docs/terminal_coverage.json` — **edit** — REQUIRED REGENERATION (by_where keys are file:line). Drop phantom keys 'agent/skills/env_manager.py:266' and ':274' (both currently False = never executed). Remaining env_manager keys >281 (72 total, e.g. :1101, :1106, :1183, :1279, :1296) shift up by 27. Must be regenerated in lockstep with outcomes_ledger.json or scripts/seaworthy_scope.py summarize() joins ledger->overlay on stale keys.

### tests affected

- (none)

### docs affected

- docs/outcomes_ledger.json
- docs/terminal_coverage.json

### surprises

[1] NO TESTS NEED DELETING OR UPDATING — zero tests name any of the 7 symbols. Controlled base-vs-mutant run is byte-for-byte identical (2F/695P/1S both sides, 698 collected both sides). CLAUDE.md needs NO edit either: its only "install_pip" hit (line 56) is the SEPARATE live MCP tool install_pip_package. The claim's framing ("CLAUDE.md says the host/finalize path is RETIRED") is background, not a doc edit.

[2] REAL FINDING — THE SEAWORTHY TRUST METER COUNTS A DEAD FUNCTION'S TERMINAL. Ran scripts/seaworthy_scope.py: env_manager.pip_installed (install_pip:266) is outcome='proven' => is_load_bearing=True, arena='local'. It sits in the Seaworthy v1 local_total DENOMINATOR — i.e. the trust meter reserves credit for a terminal in a function NOTHING CAN CALL, and terminal_coverage.json confirms it has never executed (False). Deleting + regenerating shrinks local_total by 1 and makes the meter MORE honest. (Its sibling pip_install_failed is outcome='broke' => not load-bearing.) This is a live instance of the audit's own thesis, in the instrument that MEASURES the honesty contract.

[3] THE LEDGER IS "ONE TRUTH IN N PLACES" — AND UNGUARDED. docs/outcomes_ledger.json + docs/terminal_coverage.json key every terminal by file:LINE NUMBER, but NO test re-derives them from source. I verified test_outcome_tags.py reads both as static JSON and joins them to each other, never to the code — so they stay mutually consistent while BOTH drift from reality. Any deletion above line 281 silently staleifies 70 env_manager entries and the suite stays green. The extractor is not wired into CI or any hook (grep of .github/.githooks/.pre-commit-config/Makefile => nothing). This is the exact disease the audit named, in the audit's own instrumentation. Worth a separate finding: add a drift test asserting each ledger 'where' still resolves to its recorded 'code' in source.

[4] METHODOLOGICAL — PARALLEL SUBAGENT COLLISION CORRUPTED MY FIRST RUN (orchestrator should act). My first experiment showed 6 tests "vanishing" (698->692: test_content_digest_*, test_lookup_tag_by_digest_*) and an extra failure. That was NOT my deletion. A sibling verification subagent — evidently checking freeze.content_digest_parts and biocontainers.lookup_tag_by_digest — wrote into the SAME session scratchpad using the same obvious dir name, deleting those symbols AND their tests from my tree. Proven by mtime: my edit 13:04:10 vs their three files all at 13:10:37, and by baseline flapping 6F -> 2F across identical re-runs. Re-run in a uniquely-named dir gave 698==698 and identical pass counts. RECOMMENDATION: give each subagent a unique scratch subdir — concurrent siblings sharing scratchpad/{probe,baseline} will silently corrupt each other's verdicts, and a less careful agent would have reported "deleting these 7 probes breaks 6 digest tests" and wrongly returned KEEP. I removed probe/ and baseline/ during cleanup; if a sibling was mid-flight there, its tree is gone.

[5] TRAP FOR THE APPLIER: list_explicit_conda_packages (dead, DELETE) CALLS export_environment_yml (live, KEEP) at :1496, and they are ADJACENT (1486-1514 vs 1516-1560). A range-based delete that overshoots by one block silently kills the live conda-lock chain (freeze_tools.py:412). Delete by AST symbol, not by eyeballed range.

[6] The 2 pre-existing suite failures (test_c4_crash_safety[upload]/[download]) are a scratch artifact, not a repo bug: they need the gitignored ~/.bioinf/projects_access.yaml, absent from a git-archive copy. They fail identically before and after, so they do not affect this verdict.

---

## dead-code-batch-7-symbols

- **verdict** `DELETE` · **LOC** 130 · claim accurate: partly · MCP-reachable: False · risk: low
- **refutation**: upheld

### evidence
```
All 7 classifications CONFIRMED at HEAD 40cf9ca (branch fix/tier0-honesty-gates). Ruled out the false-dead classes that burned the audit twice: no getattr dynamic dispatch (only `getattr(ret,"tool_found")` / `_drafts` / `is_image_runner`), no `import *` anywhere, no __all__ in any of the 6 files, zero hits in scripts/ and .claude/, 0 hits in docs/capability_map.json. Checked the `python -m` subprocess class: agent/skills/freeze_runner.py reaches `freeze` via `from agent.mcp_server import freeze` (line 71) — the MCP surface, not these symbols. `git log -p -2` (fb57ac0 tier5, 40cf9ca tier6) added NO callers; 40cf9ca's +46 to container_build.py added `registry_manifest_digest`, untouched write_file.

ZERO CALLERS AT ALL (5):
1. output_validator.infer_type — agent/validators/output_validator.py:481-504 (24 LOC, claim said 23). Only definition site in repo. DIVERGENCE CONFIRMED vs live run_tools._infer_validator_type (agent/mcp_tools/run_tools.py:23): dead twin maps .txt/.tsv -> "log"; live twin maps txt->txt, tsv->tsv. A third twin exists: spec_writer._infer_type_from_basename:386 (docstring admits "Mirrors _infer_validator_type") — LIVE at spec_writer.py:354, NOT deletable. So this truth lives in 3 places; deleting the dead one leaves 2.
2. transfer._build_remote_target — agent/skills/transfer.py:314-326 (13). Live scp provider transfer_providers.py:179-181 re-implements it inline verbatim.
3. transfer._remote_sha256_ssh — agent/skills/transfer.py:790-811 (22). Live provider re-implements inline at transfer_providers.py:206-230.
   NOTE: scp_head_node IS live (transfer_providers.ScpHeadNodeProvider) — the audit's earlier error. But it imports only `_scp_argv, _remote_sha256_cmd, _parse_sha256sum_output` (transfer_providers.py:171,251) — NOT these two. Its own comment (transfer_providers.py:135-137) claims "we import them here so this is strictly a packaging change, not a duplication" — FALSE for these two: the provider extraction duplicated them and orphaned the originals.
4. container_build.write_file — agent/skills/container_build.py:691-711 (21). Feeds self.longtail (line 708), which IS consumed (container_build.py:453,779). Sibling producer ContainerBuild.run:714 is live via env_build.py:134 `self.cb.install(spec)`. write_file has ZERO producers: env_freeze tier dispatch (lines 257-279) covers source/synthesized/cargo/go/perl/r_install/spack/pip — no authored-files tier. `grep authored|base64 agent/skills/env_freeze.py` = 0 hits; env_build.py has no authored handling.
5. test_runner._find_available_genome_fasta — agent/skills/test_runner.py:178-195 (18). Only definition site; no twin.

TESTS-ONLY (2):
6. provenance.check_grounding — agent/skills/provenance.py:97-117 (21). Callers: tests/test_provenance.py:78,88 ONLY. Its docstring ("The synthesis tier and env_honesty call this so an ungrounded install can never ship") is FALSE on both counts: the live synthesis tier calls _synth.validate_submission (env_tools.py:488), which re-implements the AGE
```

### edits

- `agent/validators/output_validator.py` — **remove_symbol** — Delete `@staticmethod def infer_type(filename)` lines 481-504 inclusive (the @staticmethod decorator at 481 through `return "any"` at 504). Self-contained; no helpers become dead. Live replacements remain: run_tools._infer_validator_type:23 and spec_writer._infer_type_from_basename:386.
- `agent/skills/transfer.py` — **remove_symbol** — Delete `_build_remote_target` lines 314-326 inclusive. Live inline equivalent: transfer_providers.py:179-181. Guard is redundant (verified: _validate_remote_abs_path rejects space/tab).
- `agent/skills/transfer.py` — **remove_symbol** — Delete `_remote_sha256_ssh` lines 790-811 inclusive. Do NOT touch _remote_sha256_cmd:282 or _parse_sha256sum_output:288 — both LIVE (imported by transfer_providers.py:171,251). Removes outcome terminal `transfer.remote_sha256_unparseable`; live provider has its own `transfer.scp_upload_verify_unparseable`.
- `agent/skills/container_build.py` — **remove_symbol** — Delete `write_file` lines 690-711 inclusive (include the section comment `# -- DECLARE: an authored file (patch / config / wrapper) ---` at line 690). Do NOT touch `run` at 714 or self.longtail at 510 — both LIVE. Removes outcome terminals container_build.write_file_failed (707) and container_build.write_file_ok (711).
- `agent/skills/test_runner.py` — **remove_symbol** — Delete `_find_available_genome_fasta` lines 178-195 inclusive.
- `agent/skills/provenance.py` — **remove_symbol** — Delete `check_grounding` lines 97-117 inclusive. Do NOT touch `ground` or `external_refs` (LIVE via synthesis.py:166) or the AGENT_AUTHORED/EXTRACTED/GENERATOR constants.
- `agent/skills/compute_access.py` — **remove_symbol** — Delete `get_job_manager` lines 657-667 inclusive.
- `agent/skills/compute_access.py` — **edit** — ADJACENT DEAD CODE THE CLAIM MISSED: delete `_DEFAULT_JOB_MANAGER_BY_TYPE` line 118 — its ONLY consumer is get_job_manager:667. KEEP `VALID_JOB_MANAGERS` line 114 — LIVE, used by the config validator at 259-261.
- `tests/test_provenance.py` — **edit** — Delete lines 65-88: the section header `# -- check_grounding (the gate) ---` (65) plus test_check_grounding_flags_only_ungrounded_authored (66-82) and test_check_grounding_clean_passes (84-88). KEEP the `ground`/`external_refs` tests above (through line 62) — those cover the LIVE primitive.
- `tests/integration/honesty/L14_compute_env_safety/test_phase2_schema_load.py` — **edit** — In class TestJobManager (starts 752): delete ONLY the 4 tests that call get_job_manager — test_accepts_valid_enum_values (759-764, 2 params), test_ssh_env_defaults_to_slurm (773-778), test_local_env_defaults_to_bash_not_slurm (780-787), test_explicit_bash_allowed_on_ssh_env (789-795). MUST KEEP test_refuses_unsupported_scheduler (766-771) — it tests the LIVE config validator (load_access raising ConfigError), not the accessor. Net: 10 -> 5 test cases; class survives. Update the class docstring (753-757), which describes the type-based default that no longer exists.
- `tests/CHEAT_GUARDS.md` — **edit** — Line 133 — DO NOT delete the row; the L10.a guard is REAL, only its attribution is wrong. Re-point from `check_grounding` + `tests/test_provenance.py` to the live gate: `synthesis.validate_submission` grounds every AGENT_AUTHORED command against the fetched corpus (agent/skills/synthesis.py:165-170) — `tests/test_synthesis.py::test_validate_submission_grounds_authored_command`.
- `CLAUDE.md` — **edit** — Line 62 (the freeze row): remove the claim `· half-baked (authored files baked)` from the covered-tier list. It is ALREADY false at HEAD — write_file was its only implementation and has zero producers; env_freeze's tier dispatch (257-279) has no authored-files branch. Deleting write_file does not remove the capability; it removes the vestige of a capability that was never wired. If the capability is WANTED, that is a separate FIX (wire write_file into a half-baked tier) — do not silently keep dead code to make a doc line true.
- `docs/outcomes_ledger.json` — **edit** — Regenerate via `python scripts/extract_outcomes.py` after deletion. Three entries drop: container_build.write_file_failed, container_build.write_file_ok, transfer.remote_sha256_unparseable. Then re-render docs/outcomes_dashboard.html via scripts/render_outcomes_dashboard.py, or tests/test_outcome_tags.py::test_dashboard_renders_every_terminal_deterministically will fail on the ledger/dashboard terminal-count mismatch.
- `docs/audit_2026-07-16.md` — **edit** — Lines 710-711 — update the dead-code inventory to record these as removed, and correct `OutputValidator.infer_type (23` to 24 LOC.

### tests affected

- tests/test_provenance.py::test_check_grounding_flags_only_ungrounded_authored — DELETE (tests the dead twin)
- tests/test_provenance.py::test_check_grounding_clean_passes — DELETE (tests the dead twin)
- tests/integration/honesty/L14_compute_env_safety/test_phase2_schema_load.py::TestJobManager::test_accepts_valid_enum_values — DELETE (2 params)
- tests/integration/honesty/L14_compute_env_safety/test_phase2_schema_load.py::TestJobManager::test_ssh_env_defaults_to_slurm — DELETE
- tests/integration/honesty/L14_compute_env_safety/test_phase2_schema_load.py::TestJobManager::test_local_env_defaults_to_bash_not_slurm — DELETE (encodes a user-caught bug, but the default is unreachable: nothing reads job_manager)
- tests/integration/honesty/L14_compute_env_safety/test_phase2_schema_load.py::TestJobManager::test_explicit_bash_allowed_on_ssh_env — DELETE
- tests/integration/honesty/L14_compute_env_safety/test_phase2_schema_load.py::TestJobManager::test_refuses_unsupported_scheduler — KEEP (5 params; tests the LIVE config validator, not the accessor)
- tests/test_synthesis.py::test_validate_submission_grounds_authored_command — KEEP, UNCHANGED (this is the real L10.a coverage; it is why deleting check_grounding loses no guard)
- tests/test_outcome_tags.py::test_dashboard_renders_every_terminal_deterministically — will FAIL unless the ledger AND dashboard are both regenerated (3 terminals drop)
- tests/test_outcome_tags.py::test_seaworthy_scope_is_well_defined_and_measurable — re-run after ledger regen (iterates ledger entries; the 3 dropped terminals include one `proven`)

### docs affected

- tests/CHEAT_GUARDS.md:133 — L10.a credits `check_grounding`; must be re-pointed to synthesis.validate_submission (UPDATE, not delete — the guard is real)
- CLAUDE.md:62 — freeze row claims '· half-baked (authored files baked)'; write_file was its only (unwired) implementation
- docs/audit_2026-07-16.md:710-711 — dead-code inventory entry; also states infer_type is 23 LOC (actual 24)
- docs/outcomes_ledger.json:383,387,1417,2143,2147 — 3 terminals for write_file/_remote_sha256_ssh; regenerate
- docs/outcomes_dashboard.html — generated from the ledger; re-render or the determinism test fails
- agent/skills/install_commands.py:13 — module docstring says the half-baked tier records 'authored files via ContainerBuild.write_file'; stale once write_file is gone (code comment, not a doc file, but must be edited in the same commit)
- agent/skills/transfer_providers.py:135-137 — comment claims the provider imports the helpers 'so this is strictly a packaging change, not a duplication'; false for the two deleted functions. Fix the comment while deleting them.

### surprises

1. THE CLAIM UNDERSTATES ITS OWN CASE — three of these are the exact disease the audit named, and the doc/docstring credits the DEAD copy in each case, which is worse than mere dead code (it is a false claim of a gate):
   - `check_grounding`'s docstring asserts "The synthesis tier and env_honesty call this so an ungrounded (possibly hallucinated) install can never ship." NEITHER does. The live gate is an inline re-implementation at synthesis.py:165-170. tests/CHEAT_GUARDS.md:133 credits the L10.a cheat guard to the dead copy. The guard IS enforced and IS tested (test_synthesis.py:122-131) — but a reader auditing L10.a is sent to dead code. Deleting removes a lie; the guard is untouched.
   - `transfer_providers.py:135-137` states the provider imports the helpers "so this is strictly a packaging change, not a duplication." It duplicated `_build_remote_target` and `_remote_sha256_ssh` and left the originals orphaned. The inline copy at :179-181 also DROPPED _build_remote_target's whitespace guard — I verified empirically this opens no gap (_validate_remote_abs_path rejects ' ' and '\t'), but it is the classic drift signature: the copy that runs is the one missing a check.

2. ADJACENT DEAD CODE THE CLAIM MISSED: `_DEFAULT_JOB_MANAGER_BY_TYPE` (compute_access.py:118) — sole consumer is get_job_manager:667. Delete with it. `VALID_JOB_MANAGERS`:114 must SURVIVE (live validator at :259).

3. A THIRD LIVE TWIN OF infer_type: the claim frames it as a dead twin of run_tools._infer_validator_type. There is also `spec_writer._infer_type_from_basename` (agent/skills/spec_writer.py:386, LIVE at :354) whose own docstring says "Mirrors _infer_validator_type in run_tools". So this one truth lives in 3 places; deleting the dead one still leaves TWO live copies free to drift. That is the real remaining defect here and this deletion does not fix it — recommend a follow-up to collapse spec_writer's copy onto run_tools'.

4. write_file IS AN ADVERTISED-BUT-UNWIRED CAPABILITY, not just dead code — flagging per instruction 4, though it does NOT block deletion. CLAUDE.md:62 lists "half-baked (authored files baked)" as a covered freeze tier. write_file is its only implementation and has zero producers; env_freeze's tier dispatch has no authored-files branch. So the capability is ALREADY absent in effect — the audit's signature finding ("gates present in code, absent in effect") applied to a feature. Deleting breaks no real user path (there is no path). But do not delete silently: either drop the CLAUDE.md claim (recommended — the code becomes honest) or, if the user actually wants half-baked baking, this is a FIX (wire it) rather than a DELETE. The decision belongs to the user; the honest default is delete + correct the doc.

5. get_job_manager is a pre-declared accessor for the deferred bash-execution feature — it matches the user's own stated anti-pattern ("no pre-declared schemas for unimplemented features", feedback_architecture_discipline). Deleting is aligned with that preference. Caveat worth surfacing: test_local_env_defaults_to_bash_not_slurm documents "the bug the user caught" (a local machine must not silently claim slurm). That safety default lives ONLY in get_job_manager and is currently unreachable. Deleting discards the encoding of a user-caught bug — trivial to reinstate when bash execution ships, but the user may prefer to KEEP this one on sentimental/roadmap grounds. It is the only one of the 7 where I would accept a KEEP.

6. NOT A RISK (checked because it looked like one): the outcomes drift-lint does not block this — it is scoped to _TAGGED_FUNCS = {"seal_workflow"} in workflow_tools.py. The ledger's `where` line numbers are already stale (records container_build.py:668/672, actual 707/711), so nothing enforces ledger↔source agreement; only the dashboard↔ledger terminal count is enforced. Regenerate both in the same commit.

---

## spack

- **verdict** `DELETE` · **LOC** 122 · claim accurate: yes · MCP-reachable: True · risk: low
- **refutation**: upheld

### evidence
```
ALL 5 SUB-CLAIMS VERIFIED AT HEAD (40cf9ca, branch fix/tier0-honesty-gates).

(1) LIVE ON THE MCP SURFACE — this is NOT dead code, it is live-and-broken:
  agent/mcp_tools/env_tools.py:527  `@mcp.tool()` / `def install_spack_package` (52 LOC, 527-578)
  agent/mcp_server.py:664           `install_spack_package,` re-exported from env_tools
  Reachable chain: resolve_tool -> resolver.route/`_install_call` -> install_spack_package -> freeze -> env_freeze._map_install -> ic.spack. No subprocess/`python -m` spawn involved (checked: freeze_runner.py has no spack ref).

(2) resolve_tool("vep") -> spack STILL HAPPENS AT HEAD. Live run, no stubs:
  $ python -c "from agent.skills import resolver as R; d=R.resolve('vep'); print(d['chosen'], d['install_call'])"
  CHOSEN: spack
  AVAIL: {'conda': False, 'pip': False, 'cran': False, 'spack': True}
  INSTALL_CALL: install_spack_package(env, "vep")  # Spack curated recipe; best on a native amd64 host

(3) PROBE/BUILD LAYOUT DRIFT IS REAL AND IS A LIVE FALSE POSITIVE (curl, HTTP codes):
  PROBED  (resolver.py:261-262, spack-packages/develop, v1.0 layout)
    samtools 200 | bzip2 200 | vep 200 | py-numpy 404 | py_numpy 200 | r-ape 404 | r_ape 200
  BUILT   (install_commands.py:320, spack/spack @ v0.22.1, var/spack/repos/builtin/packages)
    samtools 200 | bzip2 200 | vep 404 | py-numpy 200 | ensembl-vep 404
  => `vep` probes AVAILABLE but DOES NOT EXIST at the pinned build ref. resolve_tool hands the agent a confident `install_spack_package(env,"vep")` that dead-ends at `spack install --fail-fast vep` inside the freeze build container. Drift runs BOTH ways: py-*/r-* probe 404 (v1.0 uses underscores) while the v0.22.1 builder could have built them. Textbook "one truth in N places, the copy that runs is the stale one".

(4) NEVER REAL-BUILD-TESTED — confirmed by git:
  $ git log --oneline --all -S"spack" -- agent/
  66cffb7 Buildable Spack tier (mechanism spiked; full freeze validation pending native host)
  Every test is a string-assert on the generated command (tests/test_synthesis.py:371-380 asserts `"spack install --fail-fast bzip2" in cmd`). No test ever runs spack. Zero corpus artifacts: `grep -rln spack env_reports/ envs/ pipelines/ docker_images/ .cache/` -> EMPTY. No user path exists to break.

(5) CANNOT ANCHOR IDENTITY — resolver.py:615 comment + tests/test_identity.py:160-162 encode the vep false alarm; probe_spack (resolver.py:252-263) returns only {available, package}, never a `summary`, so assess_identity always falls to reason="no_description".

DELETING IS A STRICT IMPROVEMENT — simulated (probe_spack stubbed False), live:
  chosen: None | recommended_repo: Ensembl/ensembl-vep | n discovered: 5
  rationale: "no registry hit — DISCOVERED 5 candidate repo(s) via github search... Recommended: Ensembl/ensembl-vep (564*). Confirm with github_repo='Ensembl/ensembl-vep' ... to install via synthesis."
  Spack is acting as a CATCH-ALL FALSE FLOOR at TIER_ORDER position 9: it intercepts names the registries m
```

### edits

- `agent/mcp_tools/env_tools.py` — **remove_symbol** — Delete lines 527-578 entirely, INCLUDING the `@mcp.tool()` decorator at line 527: `def install_spack_package(env_name, tool_name, package='', spack_ref='v0.22.1', evidence='', pipeline_id='', step=0)`. This is the live MCP tool (52 LOC). Also edit line 4 (module docstring: remove `spack / ` from 'conda / git-source / spack / release-binary / perl / cargo / go / jar /') and line 86 (resolve_tool docstring ladder: `> binary > spack > synthesis >` -> `> binary > synthesis >').
- `agent/mcp_server.py` — **edit** — Line 664: remove `    install_spack_package,` from the `from agent.mcp_tools.env_tools import (...)` block. Line 533: remove `install_spack_package,` from the comment `#                        synth_build, install_spack_package,` (reflow to `#                        synth_build, install_release_binary,`).
- `agent/mcp_tools/__init__.py` — **edit** — Line 59: remove `install_spack_package, ` from the trailing noqa comment listing env_tools exports.
- `agent/skills/resolver.py` — **remove_symbol** — Six sites. (a) Lines 252-263: delete `def probe_spack(name, timeout=12)` (12 LOC). (b) Line 55: drop `"spack",` from TIER_ORDER (list spans 54-56). (c) Lines 72-74: delete the `"spack": ...` entry from _TIER_RATIONALE (3 lines). (d) Lines 725-726: delete the `if tier == "spack": return f'install_spack_package(env, "{tool}")  # Spack curated recipe...'` branch. (e) Lines 790-792: delete the `if tier == "spack": return {"kind": "tool", "tier": tier, "spec": ic.spack(...)}` branch in route(). (f) Lines 888-891: delete the 3-line comment + `availability["spack"] = probe_spack(tool, timeout)`. Also edit lines 609-615 in assess_identity(): the parenthetical `(Live campaign: the only false alarm across 30 real tools was `vep` via the spack tier, which carries no metadata at all.)` is now moot AND factually wrong (see surprises) — delete the parenthetical, KEEP the surrounding comment and the no_description branch itself.
- `agent/skills/install_commands.py` — **remove_symbol** — Delete lines 298-332: `def spack(name, *, package='', spack_ref='v0.22.1', evidence='')` (35 LOC). Sole callers are resolver.route (deleted above) and env_freeze._map_install_spec (deleted below).
- `agent/skills/env_freeze.py` — **remove_symbol** — Two branches. (a) Lines 383-386: delete `if t == "spack": return {"spec": ic.spack(name, package=..., spack_ref=..., evidence=...)}` in `_map_install_spec` (306-473). (b) Lines 277-278: delete `if tier == "spack": return "spec_pinned_tofu", False` in `_replay_assurance` (241-281). Delete these AFTER the tests below, or test_c5_assurance.py::test_replay_assurance_matrix[spack] fails first.
- `agent/skills/env_recipe_render.py` — **remove_symbol** — Delete lines 126-127: `if t == "spack": return [f"# {name}: Spack package", f"spack install {im.get('package') or name}"]`. Also edit line 15 docstring: remove `/spack` from '(cargo/go/perl/r/synthesized/spack)'.
- `agent/skills/env_report_html.py` — **remove_symbol** — Delete line 265: `"spec_pinned_tofu": ("na", "⚠ spack spec (unverified, TOFU)"),` from _ASSURANCE_BADGE. MUST be paired with the tests/test_c5_assurance.py:84 _EMITTED edit — otherwise test_every_emitted_assurance_has_a_badge[spec_pinned_tofu] fails on `assert a in _ASSURANCE_BADGE`.
- `agent/skills/env_report_helpers.py` — **edit** — Line 54 docstring: remove `/ spack` from 'for source / synthesized / script-repo / spack tiers — the ref IS the pinned'. Comment-only; no code.
- `tests/test_synthesis.py` — **remove_symbol** — Delete 5 tests + 1 header, HIGH line numbers first: lines 514-521 test_probe_spack_live_advisory; 509-511 test_route_spack_is_a_tool_spec; 498-506 test_rank_spack_below_binary_above_synthesis; 490-495 test_map_install_routes_spack; 370-381 the `# -- buildable Spack tier ---` header + test_spack_generator_store_under_opt_tools_and_relocation. Then EDIT line 291: `for fn in ("probe_conda", "probe_pypi", "probe_cran", "probe_bioconductor", "probe_spack"):` -> drop `, "probe_spack"` (monkeypatch.setattr on a deleted attr raises AttributeError).
- `tests/test_identity.py` — **edit** — DO NOT DELETE test_missing_description_reads_as_unchecked_not_as_a_wrong_tool (149-168) — REWRITE it. The no_description branch it guards SURVIVES spack's removal; VERIFIED live: assess_identity('foo','pip',{'pip':{'available':True,'summary':''}},'') -> reason='no_description', and the same for a conda-forge entry with summary=''. Replace line 160 `monkeypatch.setattr(R, "probe_spack", lambda n, t=12: {"available": True, "package": "vep"})` + line 162 `assert d["chosen"] == "spack"` with a pip (or conda-forge) probe stubbed to {'available': True, 'latest': '1.0.0', 'summary': ''} and `assert d['chosen'] == 'pip'`; keep the reason/UNCHECKED/'may not be the' asserts verbatim. Rewrite the docstring at 149-158 (it retells the vep-via-spack story). Also EDIT line 42: drop `monkeypatch.setattr(R, "probe_spack", lambda n, t=12: {"available": False})` from the _stub helper.
- `tests/test_invariants.py` — **edit** — Delete 4 monkeypatch stub lines (high-first): 2049, 2027, 2002, 1960 — each `monkeypatch.setattr(r, "probe_spack", lambda n, t=12: {...})`. Then EDIT line 3694: drop `"spack": {"package": "x"},` from the `probes` dict in the TIER_ORDER-coverage test. That test iterates TIER_ORDER asserting route() covers every tier — it stays GREEN once spack leaves both TIER_ORDER and the probes dict.
- `tests/test_authors_sources.py` — **edit** — Delete line 110: `monkeypatch.setattr(R, "probe_spack", lambda n, t=12: {"available": False})`.
- `tests/test_c4_crash_safety.py` — **edit** — Delete line 157: `("install_spack_package", E.install_spack_package, dict(env_name=BAD_ENV, tool_name="t"), True),` from the crash-safety parametrize list.
- `tests/test_c5_assurance.py` — **edit** — Delete line 50: `("spack", {}, ("spec_pinned_tofu", False)),` from the test_replay_assurance_matrix parametrize. Edit line 45 comment `# perl / r / spack: TOFU ...` -> `# perl / r: TOFU ...`. Edit line 84: drop `"spec_pinned_tofu", ` from the _EMITTED set and fix its trailing comment `# perl/r/spack/pip` -> `# perl/r/pip`. REQUIRED — without it test_every_emitted_assurance_has_a_badge[spec_pinned_tofu] fails once env_report_html.py:265 is removed.
- `docs/outcomes_ledger.json` — **edit** — REGENERATE, do not hand-edit: `python scripts/extract_outcomes.py`. Removes the `install.spack_declared` entry (lines 1892-1899, `where: agent/mcp_tools/env_tools.py:530`). tests/test_outcome_tags.py::test_dashboard_renders_every_terminal_deterministically and ::test_seaworthy_scope_is_well_defined_and_measurable read this file and will drift against the code otherwise.
- `docs/capability_map.json` — **edit** — Generated snapshot from scripts/capability_probe.py; `spack` appears in `available[]` at lines 27, 54, 265, 296, 359. STALE-ONLY — no test asserts against it. Regenerate opportunistically (`python scripts/capability_probe.py`); not a blocker.
- `CLAUDE.md` — **edit** — Line 53 (the resolve_tool row): `(full order: author_image > authors_recipe > conda > pip/cran/bioc > binary > spack > synthesis > source > manual)` -> remove `spack > `. Only spack reference in CLAUDE.md.
- `docs/audit_2026-07-16.md` — **edit** — The audit's own findings; mark DONE rather than deleting the history. Lines: 187 (probe/build layout drift finding), 315 ('fix or drop the spack tier'), 445 + 451-453 ('Known residual: the spack tier carries no metadata... never real-build-tested'), 718-719 ('Spack: DROP. All 5 claims verified...'), 729 ('Delete install_spack_package...'). NB line 451-453's stated reason is wrong — see surprises.

### tests affected

- tests/test_synthesis.py::test_spack_generator_store_under_opt_tools_and_relocation (DELETE, 371-380)
- tests/test_synthesis.py::test_map_install_routes_spack (DELETE, 490-495)
- tests/test_synthesis.py::test_rank_spack_below_binary_above_synthesis (DELETE, 498-506)
- tests/test_synthesis.py::test_route_spack_is_a_tool_spec (DELETE, 509-511)
- tests/test_synthesis.py::test_probe_spack_live_advisory (DELETE, 514-521)
- tests/test_synthesis.py::_dead_registries (EDIT helper, line 291 — drop "probe_spack" from the stub tuple)
- tests/test_identity.py::test_missing_description_reads_as_unchecked_not_as_a_wrong_tool (REWRITE, 149-168 — re-vehicle onto a summary-less pip/conda-forge entry; DO NOT delete, the branch survives)
- tests/test_identity.py::_stub (EDIT helper, line 42 — drop the probe_spack stub)
- tests/test_invariants.py (EDIT: delete stub lines 1960, 2002, 2027, 2049)
- tests/test_invariants.py:3694 TIER_ORDER route-coverage test (EDIT: drop "spack": {"package": "x"} from the probes dict)
- tests/test_authors_sources.py (EDIT: delete stub line 110)
- tests/test_c4_crash_safety.py (EDIT: delete parametrize entry line 157, install_spack_package)
- tests/test_c5_assurance.py::test_replay_assurance_matrix[spack] (DELETE param, line 50; comment line 45)
- tests/test_c5_assurance.py::test_every_emitted_assurance_has_a_badge[spec_pinned_tofu] (EDIT _EMITTED line 84 — MANDATORY, fails otherwise)
- tests/test_outcome_tags.py::test_dashboard_renders_every_terminal_deterministically (regenerate docs/outcomes_ledger.json)
- tests/test_outcome_tags.py::test_seaworthy_scope_is_well_defined_and_measurable (regenerate docs/outcomes_ledger.json)

### docs affected

- CLAUDE.md:53 — drop `spack > ` from the resolve_tool tier ladder (only ref)
- docs/audit_2026-07-16.md:187,315,445,451-453,718-719,729 — mark DONE; NB the stated reason at 451-453 is factually wrong (see surprises)
- docs/outcomes_ledger.json:1892-1899 — install.spack_declared; REGENERATE via scripts/extract_outcomes.py
- docs/capability_map.json:27,54,265,296,359 — generated probe snapshot; regenerate via scripts/capability_probe.py (stale-only, no test asserts)
- agent/mcp_tools/env_tools.py:4,86 — module + resolve_tool docstrings
- agent/skills/env_recipe_render.py:15 — module docstring
- agent/skills/env_report_helpers.py:54 — docstring
- agent/mcp_server.py:533 — export comment
- agent/mcp_tools/__init__.py:59 — noqa comment

### surprises

FOUR THINGS THE CLAIM GOT WRONG OR MISSED.

1. THE CLAIM'S REASON #5 IS FACTUALLY WRONG (and so is the code comment and the test that encode it). "publishes no metadata so it can NEVER anchor tool identity" — Spack DOES publish a description; OUR PROBE THROWS IT AWAY. The v1.0 package.py carries a class docstring:
     class Vep(Package):
         """Ensembl Variant Effect Predictor (VEP) determines the effect of your variants
         (SNPs, insertions, deletions, CNVs or structural variants) on genes, transcripts..."""
     homepage = "https://useast.ensembl.org/info/docs/tools/vep/index.html"
   probe_spack (resolver.py:252-263) does a HEAD-only `_head_ok(url)` and discards the body. So resolver.py:615's "the spack tier ... carries no metadata at all" and tests/test_identity.py:157's docstring both blame Spack for our own probe's laziness. The verdict is unchanged (DELETE), but do NOT carry that sentence forward into the commit message or the audit doc — it is exactly the same class of defect the audit is hunting (a stale explanation that outlives the fact). Same root cause as the reliability-gate 401 the audit found: the probe asks a question whose answer it then throws away.

2. THE REAL HARM IS BIGGER THAN "A MIS-PROBE IS A FALSE POSITIVE" — SPACK IS A CATCH-ALL FALSE FLOOR. At TIER_ORDER position 9 it answers `available: True` for any bare C/C++ name in spack-packages, which means resolver.py:971 (`if decision["chosen"] is None and not github_repo:` -> probe_github_search) NEVER FIRES for those names. Verified live: with spack stubbed out, resolve('vep') returns recommended_repo='Ensembl/ensembl-vep' (564*) and "Confirm with github_repo='Ensembl/ensembl-vep' ... to install via synthesis." So the tier is not merely useless — it is actively SUPPRESSING the correct answer the router already knows how to find. Deleting it makes the router strictly better, not merely smaller. That is the strongest argument for DELETE and the claim doesn't make it.

3. ADJACENT DEAD THING THE CLAIM MISSED: `spec_pinned_tofu`. It is emitted ONLY by the spack branch of env_freeze._replay_assurance (line 277-278) and consumed ONLY by env_report_html.py:265's badge + test_c5_assurance.py:84's _EMITTED set. It dies with the tier. If you delete the branch but leave the badge and _EMITTED, test_every_emitted_assurance_has_a_badge[spec_pinned_tofu] passes vacuously and you keep a badge no code can produce — a new "one truth in N places" seed. Delete all three together.

4. ADJACENT THING THAT IS **NOT** SPACK — DO NOT TOUCH IT. agent/skills/authors_sources.py:40 `_ENV_SPECS = ("environment.yml", "environment.yaml", "conda-lock.yml", "spack.yaml", ...)` matches on the *filename* `spack.yaml` when scanning a tool's repo for env specs. It belongs to the authors-recipe completeness gate and is unrelated to the spack install tier. A naive `grep -rl spack | xargs sed` would break the reliability gate that commit 40cf9ca just brought to life.

CONFIRMED NOT-A-RISK: no user path breaks. Zero artifacts under env_reports/ envs/ pipelines/ docker_images/ .cache/ reference spack; git 66cffb7 says "full freeze validation pending native host" — the tier has never built anything, on any host, ever. This is not the scp_head_node / GPU-submission situation (live code miscalled dead); it is the inverse — a tier that is REGISTERED AND CALLABLE but has never once produced an artifact.

---

## freeze.content_digest_parts + freeze.content_digest_from_spec + freeze.has_conda_packages + biocontainers.lookup_tag_by_digest

- **verdict** `DELETE` · **LOC** 93 · claim accurate: yes · MCP-reachable: False · risk: low
- **refutation**: upheld

### evidence
```
LOC via ast (claim's numbers are EXACT): freeze.py:111-144 content_digest_parts=34; freeze.py:147-156 content_digest_from_spec=10 (=44); freeze.py:303-317 has_conda_packages=15; biocontainers.py:121-154 lookup_tag_by_digest=34. Total 93.

CALLERS (repo-wide grep, all *.py/*.md/*.yaml/*.json/*.txt/*.sh):
- content_digest_parts: def freeze.py:111; ONLY call = freeze.py:156 (inside content_digest_from_spec, itself dead). Zero live callers.
- content_digest_from_spec: def freeze.py:147; callers = tests/test_invariants.py:1717,1728,1731,1734,1738,1742,1750,1756 ONLY. freeze_tools.py:237 and freeze.py:170 are COMMENTS naming it as the REPLACED-old-path. Live anchor is record_content_digest (freeze_tools.py:417).
- has_conda_packages: def freeze.py:303; callers = tests/test_invariants.py:2510,2511,2514 ONLY. freeze.py:324 is a docstring mention. Live twin = requested_conda_specs (freeze.py:320) called at freeze_tools.py:342.
- lookup_tag_by_digest: def biocontainers.py:121; callers = tests/test_invariants.py:5482,5501,5510,5527 ONLY. The only non-test importer of the module is mcp_server.py:85 (`_biocontainers`), whose sole use is freeze_tools.py:251 `resolve_biocontainer`.

NEGATIVE CHECKS (the classes of caller module-grep misses):
- `python -m` / subprocess spawns enumerated: agent/__main__.py, mcp_server.py:484 (`python -m agent.skills.freeze_runner`). freeze_runner.py imports ONLY `from agent.mcp_server import freeze` (line 71) — no reference to any of the 4 symbols.
- getattr/dynamic dispatch on freeze/biocontainers/_ms: zero hits.
- `__all__` in freeze.py / biocontainers.py: none (no re-export surface).
- scripts/: only outcome-tag string literals ("freeze.adopt_honesty" etc.), no symbol use.

ORPHAN CHECK (deleting lookup_tag_by_digest breaks nothing): its helpers _quay_tags (biocontainers.py:107), _version_key (75), _build_number (68), mulled_v2_name (34) are ALL also called by the LIVE resolve_biocontainer (lines 186, 196, 180) — none becomes dead. Do NOT delete them.
KEEP compute_content_digest (freeze.py:104): LIVE at env_build.py:203 + freeze_tools.py:240 + integration test L13.

EXECUTABLE PROOF (scratch copy of HEAD, repo untouched): removed all 4 symbols + 6 tests + the 1 mixed-test edit → full unit suite 689 passed / 2 failed / 1 skipped. Pristine baseline of same HEAD → 695 passed / 2 failed / 1 skipped. Delta = exactly -6 (the 6 deleted tests). The 2 failures (test_c4_crash_safety.py::test_primitive_never_crashes_on_hostile_input[upload|download]) are PRE-EXISTING in baseline — caused by gitignored ~/.bioinf/projects_access.yaml being absent from `git archive`, NOT by the deletion.
```

### edits

- `agent/skills/freeze.py` — **remove_symbol** — Delete lines 111-158 inclusive — this removes BOTH content_digest_parts (111-144) and content_digest_from_spec (147-156) plus their separating/trailing blank lines. Lines 109-110 (two blanks after compute_content_digest's return at 108) remain, so `def record_content_digest` (currently 159) lands at 111 with correct PEP8 spacing. KEEP compute_content_digest (104-108) — it is live.
- `agent/skills/freeze.py` — **remove_symbol** — Delete lines 303-319 inclusive — has_conda_packages (303-317) plus trailing blanks 318-319. Lines 301-302 (blanks after non_conda_installs' `return out` at 300) remain, so `def requested_conda_specs` (currently 320) keeps correct spacing. APPLY THIS EDIT BEFORE the 111-158 edit (bottom-up) or recompute: after the 111-158 deletion these lines shift to 255-271.
- `agent/skills/biocontainers.py` — **remove_symbol** — Delete lines 121-156 inclusive — lookup_tag_by_digest (121-154) plus trailing blanks 155-156. Lines 119-120 (blanks after _quay_tags' `return []` at 118) remain, so `def resolve_biocontainer` (currently 157) keeps correct spacing. Do NOT touch _quay_tags/_version_key/_build_number/mulled_v2_name — all live via resolve_biocontainer.
- `tests/test_invariants.py` — **remove_symbol** — Delete lines 1713-1758 inclusive — test_content_digest_is_stable_and_sensitive (1713-1742) + test_content_digest_from_spec_is_degenerate_on_a_draft (1745-1756) + separating/trailing blanks. Lines 1711-1712 remain as the two-blank separator before `def test_record_content_digest_picks_the_what_was_got_anchor` (currently 1759). That surviving test (1759-1774) covers the LIVE replacement, so the regression memory is preserved.
- `tests/test_invariants.py` — **edit** — In test_non_conda_installs_reads_draft_install_steps (2502-2514) delete ONLY lines 2510-2514 (the five lines beginning `assert freeze.has_conda_packages(d) is False   # only bootstrap python present` through `assert freeze.has_conda_packages(d) is True`). KEEP lines 2502-2509 — they cover non_conda_installs, which is LIVE (freeze_tools.py:330). This is an EDIT, not a delete: the test function must survive.
- `tests/test_invariants.py` — **remove_symbol** — Delete lines 5471-5530 inclusive — the four lookup_tag_by_digest tests (5471-5489, 5492-5502, 5505-5511, 5514-5528) plus trailing blanks 5529-5530. Lines 5469-5470 remain as the separator before `def test_env_mutating_pipeline_steps_detects_pip_install` (currently 5531). KEEP test_biocontainer_version_key_ranks_by_version_then_build (5440-5452) — it covers _version_key, which stays live.
- `docs/audit_2026-07-16.md` — **edit** — Lines 709-710: the dead-code inventory lists `content_digest_parts`/`content_digest_from_spec` (44) and `lookup_tag_by_digest` (34). Mark them DONE/struck once deleted. NOTE: the list omits `has_conda_packages` (15) — add it, or record that this verification found it dead too.
- `agent/skills/freeze.py` — **edit** — OPTIONAL but recommended (dangling references to deleted symbols): line 170 inside record_content_digest's docstring reads `This replaces content_digest_from_spec(draft) on the freeze path, which…` — reword to describe the rule without naming the deleted function (e.g. 'Supersedes the old finalized-spec digest, which read packages[]/lock_sha256 a live draft lacks…'). Line 324 inside requested_conda_specs' docstring reads `…matching has_conda_packages's view.` — reword to state the rule directly ('excluding the bootstrap python from a create step').
- `agent/mcp_tools/freeze_tools.py` — **edit** — OPTIONAL (dangling reference): lines 237-239 comment names `content_digest_from_spec(draft)` as the old path. Reword to describe the rationale without the deleted symbol name. No code change — line 240 uses compute_content_digest, which stays live.

### tests affected

- tests/test_invariants.py::test_content_digest_is_stable_and_sensitive (DELETE — lines 1713-1742; exercises only content_digest_from_spec)
- tests/test_invariants.py::test_content_digest_from_spec_is_degenerate_on_a_draft (DELETE — lines 1745-1756; asserts the BUG in the deleted function; its live counterpart test_record_content_digest_picks_the_what_was_got_anchor at 1759-1774 survives and keeps the regression memory)
- tests/test_invariants.py::test_non_conda_installs_reads_draft_install_steps (EDIT, do NOT delete — lines 2502-2514; strip only 2510-2514, the has_conda_packages asserts; the non_conda_installs coverage at 2502-2509 is live)
- tests/test_invariants.py::test_lookup_tag_by_digest_returns_matching_tag (DELETE — 5471-5489)
- tests/test_invariants.py::test_lookup_tag_by_digest_returns_none_when_no_match (DELETE — 5492-5502)
- tests/test_invariants.py::test_lookup_tag_by_digest_handles_network_failure (DELETE — 5505-5511)
- tests/test_invariants.py::test_lookup_tag_by_digest_picks_highest_version_on_collision (DELETE — 5514-5528)
- tests/test_invariants.py::test_biocontainer_version_key_ranks_by_version_then_build (KEEP — 5440-5452; covers _version_key, still live via resolve_biocontainer)
- tests/test_invariants.py::test_build_number_ranking (KEEP — 1670-1676; covers _build_number, still live)
- tests/integration/honesty/L13_recipe_determinism/test_content_digest_determinism.py (KEEP, UNAFFECTED — imports compute_content_digest only, which stays)

### docs affected

- docs/audit_2026-07-16.md:709-710 — the dead-code inventory naming content_digest_parts/content_digest_from_spec (44) and lookup_tag_by_digest (34); omits has_conda_packages (15)
- agent/skills/freeze.py:170 — record_content_digest docstring names the deleted content_digest_from_spec (in-code doc, dangles after deletion)
- agent/skills/freeze.py:324 — requested_conda_specs docstring names the deleted has_conda_packages (in-code doc, dangles after deletion)
- agent/mcp_tools/freeze_tools.py:237 — comment names the deleted content_digest_from_spec (dangles after deletion)
- CLAUDE.md — NO references to any of the four symbols (verified by grep); no change needed

### surprises

The claim is accurate but UNDERSTATES the disease on two of three items — both are the exact "one truth in N places, the copy that runs is the stale one" pattern, with the dead copy ALREADY drifted.

1. content_digest_parts is not merely unused — it is the STALE THIRD DEFINITION of "what identifies an env", with a DIVERGENT key vocabulary from both live copies:
   - DEAD freeze.py:111-144 → {lock, sources, binaries, artifacts, platform, accel}
   - LIVE env_build.py:194-203 (EnvBuild.content_digest, the build anchor) → {lock, longtail, platform, engine, base, +apt_snapshot}
   - LIVE freeze_tools.py:240-243 (request-based fallback anchor) → {tools, platform, accel}
   Three definitions, three vocabularies. Deleting the dead one is the hardening.

2. has_conda_packages has ALREADY DRIFTED FROM ITS LIVE TWIN — this is a latent defect, not just dead weight. Its live twin is requested_conda_specs (freeze.py:320 → freeze_tools.py:342, where `if not conda_deps` IS the conda-layer boolean). requested_conda_specs received the R6 fix (freeze.py:349-352: skip install_steps with returncode != 0, plus move-to-end dedup). has_conda_packages did NOT: its loop at 309-312 checks `tool=="conda" and subcommand=="install" and installed_packages` with NO returncode check, so it returns True for an env whose ONLY conda install FAILED. If anyone ever wired it back in, it would provision a conda layer for a failed install. Deleting it removes a booby trap.

3. lookup_tag_by_digest's REASON TO EXIST is already gone. Its docstring (biocontainers.py:127) says "backfill adopt_source on a freeze record written before the resolver's output was preserved" — but freeze_tools.py:464 now preserves adopt_source at write time, and the legacy no-adopt_source case is handled by graceful degradation in the renderer (env_report_html.py:303-304, 464-467; covered by tests/integration/correctness/test_env_report_html_structure.py:278 "Legacy adopt records (no adopt_source…)"). The migration it was written for is solved elsewhere; it was never called.

NOTHING dead was missed adjacent to these, and two near-misses must NOT be swept in: compute_content_digest (freeze.py:104-108) is LIVE (env_build.py:203, freeze_tools.py:240, L13 integration test), and the biocontainers helpers _quay_tags/_version_key/_build_number/mulled_v2_name are all shared with the live resolve_biocontainer — deleting lookup_tag_by_digest orphans none of them.

PROCESS NOTES: (a) The working tree at HEAD is NOT clean — 5 files carry uncommitted changes (agent/mcp_tools/workflow_tools.py, agent/models/core_data.py, agent/skills/run_dashboard_html.py, agent/skills/spec_writer.py, agent/skills/user_guide.py). None is a deletion target; I did not modify the repo. Worth knowing before anyone commits this prune. (b) The repo interpreter is /Users/ao33/miniforge3/bin/python — /usr/bin/python3 has no pytest. (c) tests/test_c4_crash_safety.py::test_primitive_never_crashes_on_hostile_input[upload|download] fail on any `git archive` copy because ~/.bioinf/projects_access.yaml is gitignored — not a regression, and a trap for anyone validating this prune in a clean checkout.

RISK is low, not none, only because of the doc-comment rewording obligation (3 in-code comments will name deleted symbols) and because deleting test_content_digest_from_spec_is_degenerate_on_a_draft removes an explicit written record of a real past regression (4 distinct envs → identical content_digest). That memory survives in test_record_content_digest_picks_the_what_was_got_anchor, which guards the live replacement — but if you want the history kept, preserve the degeneracy rationale as a comment on record_content_digest. No user path breaks: zero MCP reachability, zero live callers, suite green.

---

## agent/skills/env_vendor.py

- **verdict** `DELETE` · **LOC** 77 · claim accurate: yes · MCP-reachable: False · risk: none
- **refutation**: upheld

### evidence
```
VERIFIED AT HEAD 40cf9ca (fix/tier0-honesty-gates).

LOC (wc + ast, not the claim's number): `wc -l` = 77. ast: module docstring = 62 lines (raw span lines 1-64 incl. delimiters — claim's "~64" is right); non-blank = 57; the ONLY executable code is lines 66-77 (11 lines): 2 imports + `materialize(install_steps, dest_dir)` at 71-77, whose whole body is `return refused(...)`.

ZERO PRODUCTION IMPORTERS — confirmed:
  $ grep -rn "env_vendor|audit_proof|vendor_manifest|BIOINF_VENDOR_DIR" --include="*.py" agent/ scripts/ | grep -v agent/skills/env_vendor.py
  → (empty)
Only 4 non-test files mention the string at all, all generated/history: docs/terminal_coverage.json, docs/outcomes_ledger.json, docs/outcomes_dashboard.html, docs/audit_2026-07-16.md.

`materialize` callers repo-wide: exactly ONE — tests/test_invariants.py:5251. (All other "materialize" grep hits are unrelated: container_build.materialize_lines, prose about materializing envs.)

SUBPROCESS / `python -m` SPAWN CLASS CHECKED (the freeze_runner trap): the only `python -m agent.skills.*` spawn is agent/mcp_server.py:484 → `python -m agent.skills.freeze_runner`. No env_vendor spawn, no runpy, no importlib.import_module/__import__ of env_vendor anywhere.

MCP REACHABILITY: 69 @mcp.tool decorators; none reach env_vendor. The stub's own docstring:51 states the integration point ("`mcp_server.freeze` gains a `mode: "default"|"audit_proof"` arg") — that arg DOES NOT EXIST; grep for audit-mode args in agent/ returns only env_vendor.py's own lines 2/12/51/77. Unreachable by construction.

NOT A REAL USER PATH: the stub is a no-op. Runtime proof:
  >>> env_vendor.materialize([], '/tmp/x')
  {'success': False, 'stage': 'vendor_materialize', 'reason': "...future heavy-mode feature...", 'outcome': 'refused', 'code': 'env_vendor.not_implemented'}
Its stated replacement IS live and stays live: container_build.py:92 `_SWH_CLONE_SCRIPT` + :126 `_swh_clone_install_lines()`. Deleting env_vendor removes nothing a user can invoke.

TEST-ONLY CALLERS (2), both currently passing (verified with /Users/ao33/miniforge3/bin/python -m pytest):
  tests/test_invariants.py::test_env_vendor_stub_returns_explicit_not_implemented — 1 passed
  tests/test_outcome_tags.py::test_fully_tagged_files_stay_fully_tagged — 1 passed

HISTORY: file touched by only 2 commits ever (57e6e99 SWH-fallback, 295ecdf outcome-tag wave 2). Neither of the two recent commits that postdate the claim touched it — the claim is NOT stale.
```

### edits

- `agent/skills/env_vendor.py` — **delete_file** — Delete the whole file (77 lines). Only 11 lines are executable (66-77); `materialize()` is a pure `return refused(...)` no-op with zero production callers. Its documented replacement (container_build._SWH_CLONE_SCRIPT:92 / _swh_clone_install_lines:126) is live and unaffected.
- `tests/test_outcome_tags.py` — **edit** — MANDATORY, DO NOT SKIP — delete line 220 exactly: `    "agent/skills/env_vendor.py",` from the FULLY_TAGGED list (declared at line ~197). This is NOT optional cleanup: test_fully_tagged_files_stay_fully_tagged calls `mod.harvest(ROOT / rel)` over every entry, and harvest() on a deleted-but-under-root path RAISES FileNotFoundError (verified empirically: `harvest(ROOT/'agent/skills/DOES_NOT_EXIST.py')` -> FileNotFoundError [Errno 2]). Deleting the module without this edit HARD-FAILS the test suite with an error, not a clean assertion.
- `tests/test_invariants.py` — **remove_symbol** — Remove test_env_vendor_stub_returns_explicit_not_implemented — ast-exact range lines 5246-5253 inclusive (def at 5246, last line is the `assert "audit_proof" in r["reason"] ...` at 5253). Also remove the trailing blank separator lines up to the `# ====` banner at ~5256 to keep spacing. Preceding test (test_jq_in_build_not_runtime_apt, ends ~5244) is unrelated — do not touch.
- `docs/outcomes_ledger.json` — **edit** — Regenerate, do not hand-edit. Carries exactly 1 phantom entry after deletion: {'code': 'env_vendor.not_implemented', 'outcome': 'refused', 'source': 'helper', 'func': 'materialize', 'where': 'agent/skills/env_vendor.py:73', 'end_line': 77, 'named_in_test': False}. Regenerate via scripts/extract_outcomes.py — it globs SWEEP_DIRS (extract_outcomes.py:128 `sorted(d.glob('*.py'))`), so the entry drops automatically once the file is gone. NON-BLOCKING: the ledger tests (test_outcome_tags.py:86,116,132) read the CHECKED-IN json, not a fresh sweep, so they pass either way — this is drift cleanup, not a break.
- `docs/terminal_coverage.json` — **edit** — Regenerate via scripts/measure_terminal_coverage.py. Contains 1 phantom key in by_where: 'agent/skills/env_vendor.py:73'. Non-blocking (read as checked-in data).
- `docs/outcomes_dashboard.html` — **edit** — Regenerate via scripts/render_outcomes_dashboard.py (1 env_vendor mention). It renders FROM the ledger, so regenerate AFTER outcomes_ledger.json. Non-blocking.
- `docs/audit_2026-07-16.md` — **edit** — OPTIONAL / RECOMMEND LEAVE AS-IS. Three references: line 170 ('env_vendor.py — 77 lines, 64 of them a docstring... Zero live importers'), line 314 (action item 15 'Delete env_vendor.py'), line 712 ('env_vendor.py (77)'). This is a dated historical audit record, not live documentation — the lines describe the state that motivated the deletion. If the repo convention is to tick off completed audit items, mark item 15 at line 314 DONE rather than deleting the text.

### tests affected

- tests/test_invariants.py::test_env_vendor_stub_returns_explicit_not_implemented — DELETE (lines 5246-5253). Sole behavioral caller of env_vendor.materialize; it only asserts the stub refuses. Currently passes (1 passed in 0.07s); it tests nothing once the stub is gone.
- tests/test_outcome_tags.py::test_fully_tagged_files_stay_fully_tagged — UPDATE (drop line 220 from FULLY_TAGGED). Currently passes (1 passed in 0.14s). WILL ERROR with FileNotFoundError, not fail cleanly, if the module is deleted without this edit.
- tests/test_outcome_tags.py::test_dashboard_renders_every_terminal_deterministically — NO CODE CHANGE, but it asserts html1.count('<li class=') == len(ledger) against the CHECKED-IN docs/outcomes_ledger.json. Safe if the ledger is left alone; if you regenerate the ledger (recommended), this test re-derives from the new ledger and still passes. Just don't half-regenerate (ledger without dashboard).
- tests/test_outcome_tags.py::test_seaworthy_scope_is_well_defined_and_measurable — NO CHANGE NEEDED. Cross-checks ledger vs terminal_coverage.json by_where. env_vendor.not_implemented is source='helper', not 'proven'/'invariant', and is not in sc.FIREWALL_CODES, so it is NOT load-bearing and does not move the Seaworthy meter. Removing it from both json files together is consistent; removing it from only one is also safe here since the check iterates the ledger, but regenerate both.

### docs affected

- docs/audit_2026-07-16.md:170
- docs/audit_2026-07-16.md:314
- docs/audit_2026-07-16.md:712
- docs/outcomes_ledger.json (1 phantom entry — regenerate via scripts/extract_outcomes.py)
- docs/terminal_coverage.json (1 phantom by_where key — regenerate via scripts/measure_terminal_coverage.py)
- docs/outcomes_dashboard.html (1 mention — regenerate via scripts/render_outcomes_dashboard.py, after the ledger)
- CLAUDE.md — NO references. Grepped for env_vendor/audit_proof/audit-proof/HEAVY mirror: zero hits. No CLAUDE.md edit required.

### surprises

SURPRISE 1 (the one the claim missed — a caller a module-level import grep CANNOT see): tests/test_outcome_tags.py:220 references the module as a STRING LITERAL inside the FULLY_TAGGED list, not an import. The ratchet test feeds every entry to `harvest(ROOT / rel)`, which does an unguarded read → deleting the file without editing line 220 produces a FileNotFoundError ERROR in the suite. This is the same caller class as the freeze_runner `python -m` spawn you warned about: a name-as-data reference. Verified empirically rather than assumed. It costs one line to fix, so the verdict stays DELETE.

SURPRISE 2 (favorable — the stub is a no-op, so there is no "live code called dead" repeat here): unlike the scp_head_node provider and GPU submission, env_vendor has no behavior at all to lose. `materialize()` returns `refused(...)` unconditionally; there is no branch, no side effect, no I/O. Even if a caller existed, it could only ever receive a refusal. The feature it documents was never wired: the freeze `mode="audit_proof"` arg named at its own docstring:51 does not exist anywhere in agent/.

SURPRISE 3 (adjacent, NOT part of this claim — flagging, not adjudicating): the audit's neighboring item at docs/audit_2026-07-16.md:171-172 says `env_freeze.build_env_from_tools` is "documented but unreachable". My grep is consistent with that — it is DEFINED at agent/skills/env_freeze.py:703, has real tests (test_invariants.py:4581, :4623, and :3680 guards its `_R_TIERS` behavior), and is referenced by resolver.py:815 (comment) and :827 (an error-message string) — but NO agent/mcp_tools/*.py exposes it. Meanwhile CLAUDE.md's freeze row advertises it as a live declarative sibling ("**`env_freeze.build_env_from_tools(name, tools, …)`** is the *declarative* sibling"). That is exactly this repo's systemic disease shape (truth in N places, the doc claiming a surface the MCP layer doesn't have) — but it is materially DIFFERENT from env_vendor: it has a real implementation and real tests, so it is a FIX/EXPOSE candidate, not a DELETE. Do not let this verdict's DELETE bleed onto it; it needs its own verification pass.

SURPRISE 4 (staleness check came back clean): the claim predates two commits, but `git log --all -- agent/skills/env_vendor.py` shows the file has been touched by exactly TWO commits in its entire history (57e6e99, 295ecdf), neither recent. The claim is not stale.

---

## agent/models/__init__.py + KNOWN_PIPELINES

- **verdict** `DELETE` · **LOC** 47 · claim accurate: yes · MCP-reachable: False · risk: low
- **refutation**: upheld

### evidence
```
STALENESS CHECK (claim predates 40cf9ca, fb57ac0) — NOT STALE:
  $ git log --oneline -3 --name-only  → neither 40cf9ca (tier 6) nor fb57ac0 (tier 5) touches agent/models/*
  $ git log --oneline -S "KNOWN_PIPELINES" --all → fb57ac0, fbbfe0c. fb57ac0 is a FALSE ALARM: its only KNOWN_PIPELINES hit is the audit prose it added (docs/audit_2026-07-16.md:709). Confirmed by walking every file in that commit: "HIT: docs/audit_2026-07-16.md" only.

LOC (wc, not the claim):
  $ wc -l agent/models/__init__.py → 43   (claim's "~43" is exact)
  KNOWN_PIPELINES block = core_data.py:96-99 (4 lines). Total removable = 47.

IMPORTERS — AST scan (ast.walk over every .py, skipping .git/__pycache__/envs/node_modules), not grep:
  PACKAGE-LEVEL (from agent.models import X / import agent.models): 0
  STAR IMPORTS: []
  `from X import models` (ImportFrom(module='agent'|'.', names=['models'])): []   ← the form the first AST pass would have missed
  SUBMODULE (from agent.models.core_data import X): 11 sites / 9 files:
    agent/models/__init__.py (the re-export itself), agent/skills/core_test_data.py,
    agent/skills/resources.py, agent/skills/spec_writer.py, scripts/gen_manifest.py,
    scripts/gen_provenance.py, tests/integration/correctness/test_add_core_pod5_data.py,
    tests/integration/honesty/L5_run_honesty/test_end_to_end_seal.py, tests/test_invariants.py
  → every real consumer bypasses the package __init__ and imports core_data directly. Claim confirmed.

DYNAMIC / SUBPROCESS REACHABILITY (the freeze_runner class of caller):
  $ grep -rn "'agent\.models'|\"agent\.models\"" . → 0   (no dotted-path string, so no importlib.import_module / __import__ reaches it)
  $ grep -rn "python -m|-m agent" --include=*.py → only `python -m agent` (agent/__main__.py), `python -m agent.skills.freeze_runner` (mcp_server.py:484). Neither imports agent.models package-level; freeze_runner's transitive imports hit core_data directly if at all.

KNOWN_PIPELINES — every reference in the repo (grep -rn, all file types, incl. untracked):
  agent/models/__init__.py:9    (the re-export)
  agent/models/__init__.py:31   (the __all__ entry)
  agent/models/core_data.py:96  (the definition)
  docs/audit_2026-07-16.md:709  (audit prose)
  → zero code references outside the re-export. Claim confirmed. After edit 1, refcount = 0.

RE-EXPORT IS NOT BROKEN, JUST UNUSED: all 19 names still resolve in core_data (AST top-level symbol check → missing: []). So this is dead weight, not a latent ImportError.

TOOLING/COUNT-ASSERTION SWEEP (would a module-enumerating tool break?):
  $ grep -rn "models/__init__|agent/models" . --exclude-dir=.git --exclude-dir=envs → 2 hits: CLAUDE.md:140 (points at agent/models/core_data.py — the MODULE, which survives; unaffected) and docs/audit_2026-07-16.md:708.
  $ grep -rn "models" docs/capability_map.json scripts/capability_probe.py scripts/measure_terminal_coverage.py scripts/extract_outcomes.py → 0
  $ grep -rn "__all__" tests/ scripts/ → 0
  → no coverage/capabilit
```

### edits

- `agent/models/__init__.py` — **delete_file** — Remove the file (all 43 lines: the 21-line re-export block + the 20-line __all__). Zero importers at HEAD (AST-verified, incl. star-import and `from agent import models` forms). NOTE ON EXECUTION FORM — prefer `: > agent/models/__init__.py` (truncate to 0 bytes) over `git rm`: siblings agent/skills/__init__.py and agent/validators/__init__.py are BOTH 0-byte, and agent/__init__.py is 0-byte, so truncation is the repo's existing convention and keeps agent/models a regular package. `git rm` also works (verified empirically: agent.models resolves as a namespace package on py3.9 and all 11 `from agent.models.core_data import ...` sites still import), but it would make agent/models the only namespace subpackage under agent/ and would be silently dropped by a future setuptools find_packages() — latent, not live, since no packaging config exists today.
- `agent/models/core_data.py` — **remove_symbol** — Delete lines 96-100 inclusive — the KNOWN_PIPELINES block (96: `KNOWN_PIPELINES: frozenset[str] = frozenset({`, 97-98: the ten names, 99: `})`) plus the trailing blank line 100. Result: line 94 `}` (closing the preceding dict) + blank 95 + blank 101 + line 102 `# ---...` section header = PEP8-correct two blank lines before the `Install method` block. Do edit 1 first (or together): __init__.py:9 and :31 are the only other references, so refcount hits 0.

### tests affected

- (none)

### docs affected

- docs/audit_2026-07-16.md:709 — the dead-code inventory line listing `KNOWN_PIPELINES` (43). This is a historical audit record, not live documentation; recommend LEAVING IT (or ticking it off in place). Do not rewrite the audit's findings as if they were never found.
- CLAUDE.md:140 — NOT affected, listed only to close it out: it points at `agent/models/core_data.py` (the FileType union), i.e. the module, which survives both edits. No CLAUDE.md line references `agent/models/__init__.py` or `KNOWN_PIPELINES`.

### surprises

1. "ZERO IMPORTERS" UNDERSELLS IT — THE FILE ACTUALLY RUNS. Nothing imports `agent.models` by name, but Python imports a parent package before its submodule, so `agent/models/__init__.py` EXECUTES on all 11 `from agent.models.core_data import ...` sites today. It is inert (pure re-export, no side effects, no logging, no registration), so removal is safe — but the claim's framing ("zero importers") would be wrong for any __init__ with side effects. The reason this one is deletable is that it has none, not that it never runs. Verified by reading all 43 lines.

2. THE REPO CONVENTION ARGUES FOR TRUNCATE, NOT rm. agent/__init__.py, agent/skills/__init__.py, agent/validators/__init__.py are all 0-byte; agent/mcp_tools/__init__.py (5000 B) is load-bearing (it registers the @mcp.tool submodules by import side effect — the actual MCP surface). agent/models/__init__.py is the ONLY __init__ in the tree that re-exports without being wired to anything. `: > agent/models/__init__.py` lands the subtraction and leaves the package shape uniform. This is why I rated risk "low" not "none": the only risk in the whole candidate is choosing `rm` and creating the tree's lone namespace package.

3. ADJACENT FINDING THE CLAIM MISSED — KNOWN_PIPELINES IS HALF OF A DRIFTED PAIR, AND IT IS THE DEAD HALF. `scripts/gen_provenance.py:62` defines `_PIPELINE_TOOLS: dict[str, list[str]]` — an independent, hardcoded pipeline→tools map (bwa_samtools, freebayes, star, fastqc, bcftools, minimap2 = 6 entries) that is LIVE (consumed by `_discover_version` at gen_provenance.py:~68). KNOWN_PIPELINES is the 10-entry frozenset of the same concept (same 6 + gatk, featurecounts, trimmomatic, fastp) and is dead. This is the audit's exact disease shape — ONE TRUTH, N PLACES, drifting in N-1 — except here the copy that RUNS is the smaller one and the stale copy is the one nobody calls. Deleting KNOWN_PIPELINES removes the drifted duplicate and does not touch the live map, so it is a strict improvement. Worth flagging separately: BOTH lists are stale against the current tool-agnostic architecture (the system resolves arbitrary tools via resolve_tool; a hardcoded 6- or 10-tool allowlist encodes a closed world the product left behind). `_PIPELINE_TOOLS` is live and out of scope here, but it is the next candidate in this family.

4. `git log -S KNOWN_PIPELINES --all` FLAGS THE TIER-5 COMMIT (fb57ac0, 2 commits before HEAD) — a false positive that would look exactly like the staleness the brief warned about. It is only the audit doc adding the word. Chased it to ground by walking every file in that commit for the token: sole hit is docs/audit_2026-07-16.md. Recording it so the next reader does not re-litigate it.

5. NO scp_head_node/GPU-CLASS TRAP HERE. The two things the audit previously mis-called dead were live via a caller that module-level grep misses (subprocess/`python -m`). I checked that class explicitly for this candidate: the only `python -m` spawns are `python -m agent` (agent/__main__.py) and `python -m agent.skills.freeze_runner` (agent/mcp_server.py:484), and no dotted-path STRING "agent.models" exists anywhere in the repo, so no importlib/getattr path reaches the re-export. This candidate is genuinely unlike those two.

---

## install_method.docker_pull+manual / PipelineSpec.docker / DockerBuild

- **verdict** `PARTIAL` · **LOC** 34 · claim accurate: partly · MCP-reachable: True · risk: low
- **refutation**: REFUTED — PARTIALLY REFUTED — the core thesis holds, but one member of the deletion set is genuinely reachable and the proposal as written breaks real things.

CONFIRMED (I attacked these and could not break them): docker_pull/manual have zero producers — verified no on-disk records at ANY extension (incl. drafts/EnvCache), no scripts/.claude/hook/shell refs, no star-imports of agent.models, no getattr/disp

### evidence
```
CORE THESIS CONFIRMED. Zero producers for both values (whole repo, .py):
  $ grep -rn "['\"]docker_pull['\"]" --include=*.py .
  agent/models/core_data.py:120  (the Literal itself)
  agent/skills/freeze.py:298     (the consumer arm)
  $ grep -rn "['\"]manual['\"]" --include=*.py .
  core_data.py:121 (Literal), core_data.py:427 (SampleMeta.source — different field), resolver.py:56,79 (resolver TIER vocab, a SEPARATE namespace from install_method.type)
No writer anywhere; no on-disk record either:
  $ grep -rn "type: docker_pull|type: manual" --include=*.yaml --include=*.json .  -> 0 hits
The adopt/author-image paths use a DIFFERENT vocabulary (build_method: "adopt"/"adopt-image"/"authors-dockerfile"/"container-native", freeze_tools.py:383,439,735) so docker_pull is vestigial, superseded.
freeze.py:298 `if t not in ("conda","docker_pull")` — non_conda_installs IS live (env_freeze.py:560, freeze_tools.py:260 <- freeze MCP tool), but the docker_pull arm can never fire. Removing it is a behavior-identical no-op.
DockerBuild: zero instantiations —
  $ grep -rn "DockerBuild(" --include=*.py .   -> core_data.py:1048 (the class def) ONLY
(NB: DockerBuilder in agent/skills/docker_builder.py is a DIFFERENT, LIVE class — mcp_server.py:84,113. Do not confuse.)
REAL LOC (AST at HEAD, not the claim's guess): DockerBuild = 21 LOC (core_data.py:1048-1068), NOT ~22 for the whole cluster. Full cluster = 21 (class) + 1 (PipelineSpec.docker:1185) + 2 (models/__init__.py:5,27) + 2 (InstallMethod.docker_image:141-142) + ~8 reader lines = 34 LOC.

CLAIM WRONG #1 — resources.py:170 does NOT "always report docker_image None". list_installed_pipelines is 100% BROKEN and never reaches the docker read. PipelineSpec has NO producer at all (grep "PipelineSpec" shows only from_yaml at resources.py:169 + the class def), and pipelines_dir resolves to ./env_reports which holds only *.workflow.yaml / *.recipe.yaml. Executed read-only:
  $ python -c "from agent.skills.resources import list_pipelines; ..."
  count = 7
  ERROR cluster_refdata_validation.workflow.yaml -> 3 validation errors for PipelineSpec pipeline_name Field required
  ERROR samtools_cluster_rung3.recipe.yaml -> 5 validation errors for PipelineSpec ...
  (7/7 errors; zero successes)
The try/except at resources.py:190 swallows every ValidationError into {"file":..., "error":...}. The tool is dead-by-exception, not dead-by-None.

CLAIM WRONG #2 — there are FOUR readers, not 3. The claim missed agent/skills/freeze.py:135 (`docker = spec.get("docker")...`) inside content_digest_parts, which feeds `"platform"` into the CONTENT DIGEST (freeze.py:142). Adjacent dead code the claim missed: content_digest_parts (HEAD 111-144, 34 LOC) + content_digest_from_spec (HEAD 147-156, 10 LOC) = 44 LOC are TEST-ONLY. The freeze path was migrated to record_content_digest; freeze_tools.py:237-240 comment says so verbatim ("The old content_digest_from_spec(draft) read finalized-only fields..."). Production callers: 0.

MCP REACHABILITY (th
```

### edits

- `agent/models/core_data.py` — **edit** — HEAD:120-121 — FIX, do NOT merely delete. The Literal drifts in BOTH directions: it lists docker_pull+manual (0 producers) AND OMITS synthesized+spack (REAL producers — env_tools.py:498 writes {"type":"synthesized"} via MCP synth_build; env_tools.py:554 writes {"type":"spack"} via MCP install_spack_package). Replace with: type: Literal["conda", "jar", "pip", "r_install", "source", "binary", "perl", "cargo", "go", "synthesized", "spack"] = "conda". Deleting the 2 dead values without ADDING the 2 live ones leaves the same disease.
- `agent/models/core_data.py` — **edit** — HEAD:6 — docstring drift. Replace 'InstallMethod (conda | jar | pip | r_install | docker_pull | source | manual)' with the true vocabulary: 'InstallMethod (conda | jar | pip | r_install | source | binary | perl | cargo | go | synthesized | spack)'.
- `agent/models/core_data.py` — **remove_symbol** — HEAD:141-142 — delete the comment '# docker_pull — tool only available as a pulled image (no conda/JAR path)' and the field 'docker_image: Optional[str] = None' on InstallMethod. Zero readers (grep docker_image: only RuntimeEnvironment.docker_image:239 — a DIFFERENT field, leave it — and DockerBuild.image_tag).
- `agent/models/core_data.py` — **remove_symbol** — HEAD:1048-1068 — delete `class DockerBuild(BaseModel)` in full (21 LOC). Zero instantiations repo-wide.
- `agent/models/core_data.py` — **remove_symbol** — HEAD:1185 — delete field 'docker: Optional[DockerBuild] = None' from PipelineSpec. Safe for any legacy yaml: PipelineSpec sets model_config = ConfigDict(extra="allow") (HEAD:1156), so a stray docker: block would land in extras rather than raise. Zero such yamls exist on disk anyway.
- `agent/models/__init__.py` — **edit** — Delete line 5 ('    DockerBuild,' in the import from core_data) and line 27 ('    "DockerBuild",' in __all__). No external importer: grep 'import.*DockerBuild' -> 0 hits outside these.
- `agent/skills/freeze.py` — **edit** — HEAD:298 — change `if t not in ("conda", "docker_pull"):` to `if t != "conda":`. Behavior-identical (no package can have type docker_pull). non_conda_installs itself is LIVE — do NOT delete the function.
- `agent/skills/freeze.py` — **remove_symbol** — ADJACENT DEAD — CLAIM MISSED THIS. Delete content_digest_parts (HEAD:111-144, 34 LOC) and content_digest_from_spec (HEAD:147-156, 10 LOC) = 44 LOC. Zero production callers; superseded by record_content_digest (HEAD:159+) per the comment at freeze_tools.py:237-240. This removes the 4th docker reader (freeze.py:135/142) for free. KEEP compute_content_digest (HEAD:104) — it IS live (env_build.py:203, freeze_tools.py:240).
- `agent/skills/user_guide.py` — **edit** — HEAD:171-172 — collapse the dead docker lookup. Replace `docker = spec.get("docker") or {}` + `tag = docker.get("image_tag") or f"{name}:{version or 'latest'}"` with `tag = f"{name}:{version or 'latest'}"`. Behavior-identical in production (spec never carries docker). NOTE: this else-branch already fabricates a registry tag that may not exist — pre-existing honesty smell, out of scope, flag separately.
- `agent/mcp_tools/workflow_tools.py` — **edit** — HEAD:766 — delete the summary line `"docker": draft.get("docker") is not None,` from show_pipeline_draft's summary dict (always False).
- `agent/mcp_tools/workflow_tools.py` — **edit** — HEAD:801 — docstring lists 'docker, env_status, pipeline_status, docker_status, usage_verified, ...' as blocked keys; drop 'docker,' and 'docker_status,' to match the new BLOCKED_PATCH_KEYS.
- `agent/skills/pipeline_state.py` — **edit** — HEAD:390-391 — remove "docker" and "docker_status" from BLOCKED_PATCH_KEYS; HEAD:365 — drop 'docker_status,' from the comment. LOW PRIORITY / OPTIONAL: this is NOT a security change (patch is whitelist-first — the unknown-keys branch at 424-433 still refuses them). It only flips the outcome tag blocked_keys -> unknown_keys. If the outcomes ledger pins pipeline_state.blocked_keys coverage, keeping these two costs nothing. Recommend doing it ONLY together with the test update below.
- `agent/skills/resources.py` — **edit** — DO NOT just delete lines 170/178/179 — that is tidying a corpse. list_installed_pipelines is ENTIRELY BROKEN (7/7 ValidationError, verified by execution). Requires a separate FIX-or-DELETE decision from the user: (a) DELETE the MCP tool list_installed_pipelines + list_pipelines + the PipelineSpec model (it has zero producers since the finalize_pipeline/save_pipeline_spec retirement CLAUDE.md documents), or (b) FIX it to glob *.workflow.yaml and parse WorkflowSpec. Either way the docker read dies with it. Do not ship (a) or (b) inside this deletion without asking.

### tests affected

- tests/test_invariants.py::test_content_digest_is_stable_and_sensitive (HEAD lines 1713-1742, 30 LOC) — DELETE. Imports content_digest_from_spec and asserts docker.platform participates in the digest (spec4 flips docker.platform -> expects a different digest). Pins the dead function AND the dead docker field.
- tests/test_invariants.py::test_content_digest_from_spec_is_degenerate_on_a_draft (HEAD lines 1745-1756, 12 LOC) — DELETE. Documents content_digest_from_spec's degeneracy as a known trap; moot once the function is gone.
- tests/test_invariants.py::test_render_user_guide_without_freeze_falls_back_to_docker (HEAD lines 2135-2140, 6 LOC) — DELETE. Sets s["docker"] = {"image_tag": "bwa_samtools:1.21"} and asserts the tag is rendered — a state production can never reach. Will FAIL once user_guide.py:171 is collapsed. Textbook test-pins-dead-code.
- tests/test_invariants.py::test_patch_pipeline_blocks_runtime_captured_keys (HEAD lines 223-241) — UPDATE ONLY IF the pipeline_state edit is taken. Line 235 lists "docker" and "docker_status" in the loop; assertion at 238-239 requires key in r["rejected_keys"]. Removing them from BLOCKED_PATCH_KEYS makes them land in unknown_keys instead -> this test FAILS. Either drop the two strings from the line-235 list, or skip the pipeline_state edit.
- tests/integration/honesty/L13_recipe_determinism/test_content_digest_determinism.py — NO CHANGE NEEDED. Verified: it imports only compute_content_digest (line 33), which stays live. Do not touch.
- tests/test_invariants.py:1233, 2502 (test_non_conda_installs_reads_draft_install_steps), 2560 (test_non_conda_installs_includes_cargo_and_go), 2943 (test_non_conda_installs_includes_perl), 4200 (test_non_conda_installs_does_not_filter_failed_steps_for_adopt_decision), 6718, 6857 — NO CHANGE NEEDED. None assert on docker_pull; the freeze.py:298 tuple edit is behavior-identical. Re-run to confirm.
- BASELINE: all 8 of the above currently PASS (pytest -k, 8 passed in 0.08s) using /Users/ao33/miniforge3/bin/python — the repo's default `python3` has no pytest.

### docs affected

- CLAUDE.md:141 — 'Schema cheatsheet' line: `install_method.type`: `conda | jar | pip | r_install | binary | source | perl | cargo | go | docker_pull | manual`. Doubly wrong: lists docker_pull+manual (0 producers) and OMITS synthesized+spack (real producers via MCP synth_build / install_spack_package). Replace with: `conda | jar | pip | r_install | binary | source | perl | cargo | go | synthesized | spack`.
- agent/models/core_data.py:6 — module docstring 'Single source of truth for ... InstallMethod (conda | jar | pip | r_install | docker_pull | source | manual)'. Same both-directions drift. This file CLAIMS to be the single source of truth while being stale — the exact disease the audit named.
- agent/mcp_tools/workflow_tools.py:801 — patch_pipeline docstring enumerates 'docker, ..., docker_status' as blocked keys; update only if the pipeline_state edit is taken.
- docs/audit_2026-07-16.md:713-715 — the audit entry itself; amend with the two corrections (list_installed_pipelines is broken-by-exception, not None-reporting; 4 readers not 3) so the next reader isn't misled.
- experiments/fastp_findings.md:14 — mentions 'docker_status=built'. HISTORICAL experiment record of a retired host path. Leave as-is (do not rewrite history), no code impact.
- agent/skills/resources.py:6 + agent/mcp_tools/__init__.py:18,65 + agent/mcp_server.py:549 — module docstrings advertising list_installed_pipelines. Only touch if the tool is removed under the separate FIX-or-DELETE decision.

### surprises

FOUR surprises; the 3rd is the most important and inverts part of the claim.

1) THE LITERAL IS DRIFTED IN BOTH DIRECTIONS — the claim only saw half. core_data.py:120-121 lists docker_pull+manual with ZERO producers AND OMITS synthesized+spack which have REAL producers: env_tools.py:498 writes {"type":"synthesized"} (MCP synth_build) and env_tools.py:554 writes {"type":"spack"} (MCP install_spack_package). Both are consumed downstream by live dispatchers (env_freeze.py:262,277,369,383; env_recipe_render.py:86,126). It never explodes ONLY because InstallMethod is never pydantic-validated in production (PipelineSpec has no producer) — so the Literal is a latent trap: revive validation and every synth_build/spack freeze fails. This is the audit's disease exactly: the truth defined in core_data.py:120 (never runs), CLAUDE.md:141 (never runs), and the dispatch if/elif chains (which DO run and are correct). The copy that RUNS is fine; the two copies that CLAIM authority are stale. FIX, don't just subtract.

2) ADJACENT DEAD CODE THE CLAIM MISSED (+44 LOC) — content_digest_parts (freeze.py:111-144) and content_digest_from_spec (freeze.py:147-156) have ZERO production callers, replaced by record_content_digest; freeze_tools.py:237-240 documents the migration in a comment. This is where the claim's missed 4th docker reader lives (freeze.py:135/142). Bonus: the dead code's own docstring at freeze.py:150-155 and a dedicated test (test_content_digest_from_spec_is_degenerate_on_a_draft) exist purely to warn people not to call it — 44 LOC + 2 tests + a cautionary docstring maintained to protect a function nothing calls. Delete the function, delete the warning.

3) A "DEAD READER" IS ACTUALLY A BROKEN LIVE MCP TOOL — the claim says resources.py:170 "always reports docker_image None". It never reports anything. list_installed_pipelines (MCP-wired at mcp_server.py:712) returns 7/7 errors today because PipelineSpec has zero producers and pipelines_dir (./env_reports) now holds only WorkflowSpec/recipe yamls. Executed it read-only to confirm. The try/except at resources.py:190 silently converts every failure into a per-file error dict, so the tool "succeeds" with count=7 while parsing nothing — it fails exactly like the 8 gates the audit found "present in code, absent in effect". This is NOT a docker problem and must not be quietly swept up in a docker deletion. It is a live user-facing MCP tool that is 100% broken and needs its own FIX-or-DELETE call from the user. Flagging loudly per instruction 4 — though note the "real user path" here is already broken, so deleting the docker lines neither helps nor harms it.

4) pipeline_state.py:390-391 IS REDUNDANT, NOT PROTECTIVE — worth stating because the claim implies removing it has security weight. patch() is whitelist-first in effect (409/410 + the unknown branch at 424-433), so BLOCKED_PATCH_KEYS is belt-and-braces over PATCHABLE_KEYS. Removing docker/docker_status changes only the refusal message/outcome tag. Lowest-value edit in the set; safe to skip.

NON-SURPRISE (guarding against the audit's scp/GPU-style false-positives): DockerBuilder (agent/skills/docker_builder.py, mcp_server.py:84,113) is LIVE run-side container machinery and is NOT DockerBuild. Do not let a careless grep for "Docker" take it. Likewise resolver.py's "manual"/"spack" TIER names are a separate vocabulary from install_method.type — resolver.py:56 "manual" is live and must stay.

PROCESS CAVEAT: the working tree is dirty AND changed underneath me mid-session (DockerBuild shifted 1048->1085 between two greps; 5 files modified vs HEAD — another session is likely editing concurrently). The uncommitted diff does not touch docker/install_method, so the verdict stands, but every line number I give is HEAD-stable via `git show HEAD:<file>` and will NOT match the current working tree. Re-anchor before applying.

---
