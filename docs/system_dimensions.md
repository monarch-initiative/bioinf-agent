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
| 3b | Validation honesty — pip/Python | Talos | ⬜ | generalize functional_check to install_pip_package |
| 4 | Name collisions (same-name, different tool) | talos/cellranger | ⬜ | guard fires only w/ github_repo; bare-name silent. Fuzzy. |
| 5 | Reference data / caches (big DB, indexed) | **VEP** | 🔄 | probing — custom fasta/gff or tiny cache, not the 15GB human cache |
| 6 | Perl plugins / CPAN tier | VEP plugins | ⬜ | install_perl_package + plugin load |
| 7 | Auxiliary services (redis/postgres/spark) | TBD | ⬜ | `start_service` primitive exists, untested in a real probe |
| 8 | Multi-component pipelines (Nextflow) | Talos | ⬜ | renderer + per-project pipelines exist; untested for a real install |

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
