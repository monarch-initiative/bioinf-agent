# Stress findings — the running log

Adversarial probes against the load-bearing surface (see
[seaworthiness.md](seaworthiness.md)). Every real finding lands here so nothing
is lost across sessions. Status: `open` · `fixed` · `certified` (test banked) ·
`wontfix` (with reason).

---

## C-CERTIFIED (firewalls proven to fire)

### CERT-1 — supply-chain sha256 firewall
`env_manager.binary_sha256_mismatch`. Adversarial test drives
`install_release_binary` with a mismatched hash and asserts it **refuses before
staging** (outcome=refused, both hashes surfaced, no wrapper written). The wrong
binary never becomes runnable. Test:
`tests/test_invariants.py::test_install_release_binary_sha256_mismatch_is_refused_firewall`.
Moved dark → verified. **Status: certified.**

### CERT-2 — install→ship integrity firewall (closes F2 + F1)
`build.binary_integrity_mismatch` (added to `FIREWALL_CODES`, load-bearing).
Freeze re-fetches a binary asset and, **when it re-fetches the SAME asset the
install anchored** (URL-keyed), compares the re-fetch hash against the recorded
install-time `asset_sha256` and **refuses on mismatch** — the swapped-bytes
supply-chain gate that `VALIDATED_IN_IMAGE` cannot catch (a swapped binary runs
fine). Six adversarial tests in `tests/test_invariants.py`:
mismatch→refused · authenticated-chain→verified · TOFU→unverified ·
cross-platform-ship→unverified-not-refused · install records `asset_authenticated`
· firewall code survives the `build_env_image` call site (as `inner_code`).
Meter 34/124 → **35/125**. **Status: certified.**

**KEY REFINEMENT surfaced during the fix (not in the original F2 plan):** the
firewall is **URL-keyed**, NOT "fires whenever any sha was anchored." The dominant
real case is the **cross-platform bridge** (agent installs on darwin, ships
linux): `resolve_linux_asset` re-fetches a *different* asset than the install
anchored, so there is **no install-time hash for it to compare** — comparing
would false-refuse every mac-host build. So: same-asset re-fetch → compare +
refuse (the firewall); different-asset (cross-platform) → ships pinned to the
freeze re-fetch, **disclosed as unverified** (`unanchored_cross_platform`). This
makes Part 3 (`degraded`/unverified disclosure) the *common* path, not the edge.
Assurance is a 4-level disclosure on each shipped binary, rendered in the ENV
report: `authenticated` (✓ checksum-verified) · `pinned_tofu` · `unanchored_
cross_platform` · `unanchored`. `install_release_binary` now records
`asset_authenticated: bool` (was a publisher checksum matched) as the honest
anchor freeze reads to tell authenticated from trust-on-first-use.

---

### CERT-3 — C2 COMPLETE: all 8 named firewalls fire under adversarial test
The remaining 6 named firewalls now each have an adversarial test triggering
their reject branch (`tests/test_invariants.py`):
- `freeze.recipe_load_failed` — unreadable recipe path → refused
- `freeze.recipe_invalid` — YAML dict with no `name` → refused before rebuild
- `freeze.recipe_not_reproduced` — rebuild succeeds but content_digest DRIFTS →
  broke, not a green `recipe_verified` (rebuild mocked)
- `container_build.validation_in_image_failed` — in-image evidence rc≠0 → broke
  (validated==shipped; docker `_sh` mocked to fail every check)
- `env_build.verification_in_image_failed` — EnvBuild-level in-image verify fails
  → broke (cb.validate_in_image mocked to report failure)
- `freeze.adopt_honesty` — adopt a pure-conda samtools while claiming accel=cuda
  with no toolkit_version → refused at the adopt gate (I12), never a fake
  POLICY_CLEAN badge (the dorado-stress D2 hole; biocontainer resolver + cache
  mocked, no docker)

**C2 (every firewall fires) is GREEN on the LOCAL surface: 8/8 firewalls
certified.** Meter 35/125 → **41/125 (33%)**. Method that unlocked the deep
freeze_tools terminals offline: the `@mcp.tool()`-decorated functions
(`freeze`, `verify_env_recipe`) are plain callables, so they drive directly with
the heavy deps (biocontainer resolve / recipe rebuild / docker `_sh`) mocked —
reaching the specific reject LINE (what certification requires) without a real
build. **Status: certified.**

---

## FINDINGS (real gaps)

### F1 — `proven` conflates verified with unverified installs  ·  status: FIXED (subsumed by CERT-2)
**Where:** `install_release_binary` (and systemically, see below).
**What:** when no `sha256` is supplied, the checksum firewall is silently
skipped and the result is still `proven · binary_installed` — byte-identical in
shape to a checksum-verified install. There is no flag distinguishing "matched
the publisher's checksum" from "ran whatever came down the wire."
**Why it matters (full-auto):** a subagent trusts the green and cannot tell a
cryptographically-verified install from an unverified one. It proceeds blind.
**Systemic:** not a one-off. Confirmed the same "pinned-what-we-got,
verified-nothing → full `proven`" pattern in:
- `install_release_binary` — `sha256` optional.
- `install_git_repo` — `ref` optional; clones the default branch, pins the SHA
  it *got*, never verified against an intended ref.
(conda/pip/cargo/go are OK — the lock / content_digest / toolchain capture pins
the resolved identity, so `proven` there is honest.)
**Proposed fix (C5):** return `degraded("…installed_unverified", …)` — same
successful install, but the outcome tells the truth about its assurance level
(first real use of the `degraded` class). One check before landing: confirm
`freeze`/seal treat a `degraded` install as valid-but-flagged (they should —
degraded is non-fatal), so unverified binaries stay freezable, just marked.

### F2 — binary tier has NO integrity chain install→ship  ·  status: FIXED + certified (CERT-2)
**Where:** `freeze` binary re-fetch (`agent/skills/env_freeze.py:271-283`) +
`install_release_binary`.
**What:** freeze re-fetches the asset and anchors the ship image to *whatever the
URL serves at freeze time* — `hh = sha256_of_url(la["url"])` then
`release_binary(..., sha256=hh["sha256"])`. It never compares against the
install-time recorded `asset_sha256` (grepped: `asset_sha256` is read NOWHERE
downstream — dead provenance) and never against a publisher checksum. Chain:
install hashes A → freeze re-fetches + hashes B, uses B → container verifies
against B. If A ≠ B (mutated release asset / compromised mirror between install
and freeze), nobody notices. `VALIDATED_IN_IMAGE` doesn't save it — that proves
the binary RUNS, not that it's UNTAMPERED (a malicious binary runs fine).
**Why it matters (full-auto):** a supply-chain false-green — the shipped frozen
artifact an autonomous agent trusts can contain a swapped binary, reported
`proven`/`frozen`. This is the deeper hole that made F1's `degraded`-relabel a
band-aid: the gap lives in the freeze re-fetch, not the install return.
**The correct fix (integrity chain) — 3 parts:**
1. **Freeze compare-and-refuse:** freeze compares the re-fetched hash against the
   recorded install-time `asset_sha256`; **refuse on mismatch** (the real
   install→ship supply-chain firewall; fires whenever any sha was anchored).
2. **Assurance propagates:** carry `verified: bool` through
   `install_method → freeze record → attestation / ENV report` so the shipped
   artifact discloses whether its components were checksum-verified.
3. **`degraded` only when genuinely unanchored** (no publisher sha anywhere) —
   subsumes F1; its honest non-band-aid role, artifact says so.
Each part gets an adversarial test (mutate-between-install-and-freeze → refuse;
unanchored → degraded + disclosed). Verify freeze/seal accept `degraded`
installs (valid-but-flagged).

### A1-O1 — adopt path doesn't introspect the biocontainer SBOM  ·  status: open (enhancement)
**Where:** `freeze` ADOPT path (pure-conda + published biocontainer).
**What:** the adopted image is pinned by digest (the truth) but not cracked open
to enumerate its package closure; the ENV report shows `resolved_packages` /
`system_packages` count 0 and *honestly discloses* "not introspected in-locus".
**Why it matters:** not a contract violation (the report doesn't lie), but an
adopted env's report is less informative than a built one — no package
inventory for a human or downstream audit.
**Proposed fix:** pull + inspect the adopted image to populate its SBOM, so
adopted and built envs' reports are equally complete. Low priority.

### CERT-4 — C1 (validators): all 22 output validators certified against false-greens
Every `OutputValidator._ok` proven now has TWO adversarial anchors
(`tests/test_validator_certification.py`): a VALID fixture → proven + the
specific `_ok` code (real green, executed + named), and a MALFORMED
plausible-but-wrong fixture of the SAME type → NOT proven (passed=False). The
validators are the honesty-critical core of Layer 2 (seal I3/I4 rest on them);
a false green means an autonomous agent trusts corrupt output. Covers the text
fallbacks (sam/fastq/fasta/vcf), the magic-byte checks (bai/bigwig), the
structural parsers (bed/counts/gtf/gfa/json/jsonl/html/tsv/txt), the
tool-success paths (sam_ok/sam_quickcheck_ok/vcf_ok/seqkit_stats_ok, `_run_tool`
mocked deterministic), and the opt-in `empty_allowed`. Notable false-green
attacks blocked: `touch foo.html` (html_no_prefix), a binary blob renamed .txt
(txt_binary), ragged tabular, non-int BED coords, seq/qual length mismatch.
Meter 41/125 → **63/125 (50%)**. **Status: certified.**

### CERT-5 — C1 (verify anti-cheat gate): env_manager.verified certified
`EnvManager.verify` is the honesty gate deciding whether an install actually
happened. Its green (`env_manager.verified`) now has a valid-case test (all
three gates hold → proven) plus two FALSE-GREEN attacks that must NOT reach
proven (`tests/test_verify_installation_cheatguards.py`): the **echo cheat**
(`echo '1.21'` exits 0 but names no token → verify_rejected) and the
**library-only cheat** (names the tool + exits 0 but nothing installed, no
`which`/registry anchor → verify_rejected). Plus a real-failure case
(tool present, check rc≠0 → verify_failed, not a green). The three runtime
truth-sources (run_in_env / evidence.cli_which / _package_in_registry) are
mocked so every branch is deterministic. Meter 63/125 → **64/125 (51%)**.
**Status: certified.**

### CERT-6 — C4 crash-safety: 64-tool harness + 9 crashes fixed
`tests/test_c4_crash_safety.py` drives EVERY agent-facing MCP tool with hostile
input (missing env/project/pipeline, bad paths, empty/malformed/wrong-type args)
and asserts each returns an interpretable dict — an outcome tag where a gated
action was attempted — never an uncaught exception (which under full-auto is a
dead end: the agent gets a traceback it can't branch on). A safety net
neutralises every external boundary (subprocess/Popen/urllib/requests) and
scopes pipeline-state + job writes to tmp; a ratchet asserts every `@mcp.tool()`
has a battery entry (it already caught a missed tool, `check_gpu`).

**9 real crashes found and fixed:**
1. `transfer.upload`/`download` — `KeyError` on unknown project/env (the auth
   lookup raises; handler didn't catch it) → `refused`.
2. `bridge.globus_task_status` — same `KeyError`, inlined + unguarded → `refused`.
3. `job_manager.start` — `ProcessLookupError` from `os.getpgid` on a
   fast-exiting child (a real race, not just the fake) → race-safe; also now
   refuses empty command + nonexistent env before spawning.
4. `data.download_reference_database` — built a doomed empty-URL download instead
   of validating → refuses empty name/url/local_path up front.
5. `package_search` — `JSONDecodeError` on a 200-but-non-JSON registry response
   (rate-limit/proxy page) → `broke` tag (both `.json()` sites).
6. `workflow.write_pipeline_provenance` — pydantic `ValidationError` → `refused`.
7. `validate_output(files=[None])` — `AttributeError` on a non-dict batch entry
   → records it failed and continues.
8. `mark_step_validated(step="x")` — `TypeError` on `1 <= step <= len` → coerces
   int, refuses `mark_validated.bad_step`.

Query/probe/idempotent tools (resolve_tool, search_package, select_test_data,
check_gpu, check_service_health, discard_pipeline_draft, install_pipeline_brief)
return interpretable data dicts by design — the harness requires no-crash + dict
for them, a tag only where a gated action was attempted. All 8 fixes are
mutation-verified (reverting each fails its test). **Status: certified.**

**PROCESS LESSON:** `git checkout -- <file>` during mutation testing reverts to
HEAD and **silently clobbers uncommitted fixes**. It wiped two fixes mid-session
(re-applied). Mutation-test with in-memory backup/restore, OR commit first.

### CERT-7 — L15 real-build tier + a real UTF-8 crash mocks couldn't catch
`tests/integration/honesty/L15_real_build/` is the FIRST test that drives an
actual container-native Docker build end-to-end (pigz, ~52s). It asserts the
honesty contract passes on the REAL record (`check_build() == []`, pigz
validated IN the shipped image, digests resolved) — the literal validated==
shipped guarantee on genuine bytes, and it exercises the docker-only build
terminals (`container_build.started/frozen/pixi_*`, real
`env_build.verified_in_image`) that every mocked build test can't reach.

**FINDING (fixed):** writing it surfaced a crash no mock could — `pigz`'s
banner probe emits gzip magic (0x1f **0x8b**) to stdout, and
`subprocess.run(text=True)` raised `UnicodeDecodeError`, killing the whole build
during validation. Any tool whose `--version`/banner emits non-UTF-8 bytes would
have crashed a real build. Fixed `container_build._sh` AND
`output_validator._run_tool` with `errors="replace"` (returncode is the verdict;
the text is diagnostic). Regression-locked docker-free
(`test_container_build_sh_survives_non_utf8_output`,
`test_output_validator_run_tool_survives_non_utf8`). This is the payoff of the
real-build tier: it exercises paths mocks stub out, where real-world tool
behaviour lives. Meter 76 → **77/125**. **Status: certified.**
