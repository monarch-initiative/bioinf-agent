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

---

## FINDINGS (real gaps)

### F1 — `proven` conflates verified with unverified installs  ·  status: open
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
