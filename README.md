# bioinf-agent

Install a bioinformatics tool once, get back an artifact you never have to second-guess.

`bioinf-agent` installs bioinformatics tools into isolated conda environments,
**validates them inside the very image it ships**, packages them as HPC-shippable
containers, and emits a machine-verified spec. It's designed to be **a solved
component**: call it once per tool/version, get a trustworthy, content-addressed
artifact, and never look at the install again.

The distinguishing property is an **honesty contract** — nothing the agent claims is
taken on faith. An environment is built and validated *inside the image it ships*, so
"install" and "ship" are one event; every tool's evidence command is re-run inside that
image before the env is registered. Reports, recipes, and provenance are rendered
**purely from the verified record** — they cannot present a requested version as an
installed one. (See [CLAUDE.md](CLAUDE.md) for the full contract.)

---

## Requirements

This is the true minimum to get to a validated environment:

| Need | Why | Notes |
|------|-----|-------|
| **Python ≥ 3.10** | runs the agent / MCP server | CI tests on 3.11; the built tool-envs use 3.11 |
| **conda / miniforge** | solves and builds the isolated tool environments | `conda` must be on `PATH` |
| **Docker** | `freeze` builds/adopts the shippable image and validates *inside* it | daemon must be running for `freeze`, `run_step_in_container`, `freeze_from_image` |
| **An MCP client** | drives the tool surface (e.g. Claude Code) | the agent is an MCP server |

The HPC bridge (submitting to a real SLURM cluster) is optional and configured
separately via `projects_access.yaml` — see the [HPC bridge](#hpc-bridge-optional) note.

---

## Install

> **This is a workspace-rooted service, not a site-packages library.** It reads and
> writes `config/`, `data/`, `env_reports/`, `envs/`, and `docker_images/` relative to
> the repo root. Install **editable** from the checkout and launch from there — a plain
> `pip install .` into site-packages would relocate the code away from those directories
> and config loading would fail.

```bash
git clone https://github.com/monarch-initiative/bioinf-agent
cd bioinf-agent
pip install -e ".[dev]"          # editable install + the test extra (pytest)
```

### Bootstrap the core test data (one-time)

```bash
./scripts/setup_core_test_data.sh            # core_tools env + chr22 + read datasets + a phenopacket
./scripts/setup_core_test_data.sh --minimal  # env skeleton + chr22 only; skips the multi-GB read/long-read/pod5 pulls
```

Use `--minimal` when you want a working agent + reference genome quickly and don't need
the full read-dataset corpus (it keeps the `core_tools` env and the chr22 reference, and
skips the large data downloads + the smoke test).

### Register the MCP server

The server is registered in [.mcp.json](.mcp.json) (server name `bioinf`, launched via
`scripts/start_mcp_server.sh`). Point your MCP client at that, or launch directly:

```bash
python -m agent      # canonical launch
bioinf-mcp           # same entry point, installed as a console script
```

---

## The two layers

The system has two lifecycles, solved independently:

- **Layer 1 — the environment** (`freeze`). Solved *once*, frozen, and
  content-addressed. Build it with the install primitives (conda / pip / R / JAR /
  binary / source / cargo / go / perl), then `freeze()` produces a digest-addressed,
  HPC-shippable image registered in an on-disk cache. A later identical request returns
  it by hash — no re-solve. Non-conda installs are built **and validated inside the ship
  image**, so *validated == shipped*.

- **Layer 2 — the workflow** (`seal_workflow`). *Consumes* a frozen env by digest,
  validates the run-side invariants, pins the env, and writes a machine-verified
  `WorkflowSpec` plus an HTML run dashboard rendered from the passing run.

Each `freeze` also writes deliverables **rendered purely from the verified record**: an
env report (`.ENV.html`), an in-toto/SLSA attestation (`.attestation.json`), and a build
recipe in both machine (`.recipe.yaml`) and human (`.recipe.md`) form — for *every*
install path, so a frozen env is always reproducible.

---

## Quickstart flow

Driven through the MCP tool surface (the agent picks the right primitive):

1. `start_pipeline(name, description)` → a `pipeline_id` threaded through every call.
2. Compose install primitives to build the env (conda first, then the others).
3. `run_pipeline_step(...)` — run the tool against test inputs; every output is
   auto-validated.
4. `freeze(env, tools, ...)` — **Layer 1**: build/adopt the content-addressed,
   HPC-shippable image. Returns a `freeze_request_key`.
5. `run_step_in_container(freeze_request_key, ...)` — re-run steps *inside* the frozen
   image so the recorded run is the one that ships.
6. `seal_workflow(pipeline_id, freeze_request_key)` — **Layer 2**: validate, pin, and
   write the `WorkflowSpec` + run dashboard.

`resolve_tool(tool, version?)` is the place to start when you're unsure which install
tier a tool needs; `list_installed_pipelines()` shows what's already been built here.

---

## HPC bridge (optional)

To drive a job on a real SLURM cluster, author a `projects_access.yaml` at the repo root
(gitignored — it's personal; see `agent/skills/projects_access.yaml.example` for an
annotated template). It declares your compute environments (ssh host, scratch/common-data
zones, transfer protocol) and per-project authorized directories. The bridge primitives
(`snapshot_project`, `run_step_on_cluster`, `submit_workflow_job`, …) are all gated by
that file, and every transfer is checksum-verified. See the
[Phase-A](docs/hpc_bridge_phase_a_playbook.md) and
[Phase-B](docs/hpc_bridge_phase_b_playbook.md) playbooks.

---

## Tests

```bash
pytest                                          # the project suite (scoped to tests/)
pytest -m "not live and not integration_docker" # the fast hermetic honesty tier (what CI runs)
pytest -m live                                  # opt-in: hits real package registries over the network
```

---

## Docs

- [CLAUDE.md](CLAUDE.md) — the full honesty contract, every primitive, and the protocol.
- [docs/](docs/) — architecture, the HPC bridge, and the outcomes/intent dashboards.

## License

BSD 3-Clause — see [LICENSE](LICENSE).
