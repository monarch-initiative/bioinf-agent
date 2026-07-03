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
