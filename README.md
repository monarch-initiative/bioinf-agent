# bioinf-agent

Install a bioinformatics tool once, get back an artifact you never have to second-guess.

`bioinf-agent` installs bioinformatics tools into isolated conda environments,
**validates them inside the very image it ships**, packages them as HPC-shippable
containers, and emits a machine-verified spec. It's designed to be **a solved
component**: call it once per tool/version, get a trustworthy, content-addressed
artifact, and never look at the install again.

The distinguishing property is an **honesty contract** — nothing the agent claims is
taken on faith. An environment is built and validated *inside the image it ships*, so
"install" and "ship" are one event; reports, recipes, and provenance are rendered
**purely from the verified record** — they cannot present a requested version as an
installed one. (See [CLAUDE.md](CLAUDE.md) for the full contract.)

---

## Requirements

| Need | Why |
|------|-----|
| **conda / miniforge** on `PATH` | creates every environment — including the agent's own runtime env |
| **Docker** (daemon running) | `freeze` builds/adopts the shippable image and validates *inside* it |
| **An MCP client** (e.g. [Claude Code](https://claude.com/claude-code)) | the agent is an MCP server; the client drives it |

Python is **not** something you manage: setup creates a repo-local runtime env
(`./.conda_runtime/`, Python 3.11) and every launcher resolves it by path.
An HPC cluster (SLURM + Apptainer) is **optional** — see [HPC bridge](#hpc-bridge-optional).

---

## Setup — two commands

```bash
git clone https://github.com/monarch-initiative/bioinf-agent && cd bioinf-agent
./scripts/setup.sh          # ~4 min: runtime env + deps + core toolkit + chr22 test data
```

What it does, in order: creates `./.conda_runtime/` (the agent's runtime env) →
editable-installs the agent into it → bootstraps `envs/bioinf_core_tools`
(samtools/bcftools/seqkit/bwa) and the chr22 reference under `data/core_test_data_hg38/`
→ runs the systems check. It is idempotent — re-run it anytime.

```bash
./scripts/setup.sh --check  # systems check only: PASS/FAIL per requirement, every FAIL names its fix
./scripts/setup.sh --full   # setup + the full read-dataset corpus (multi-GB; only if you need it)
```

> **Editable install, on purpose.** This is a workspace-rooted service, not a
> site-packages library — it reads and writes `config/`, `data/`, `env_reports/`,
> `envs/`, and `docker_images/` relative to the repo root. `setup.sh` installs it
> editable into the runtime env; a plain `pip install .` into site-packages would
> relocate the code away from those directories and config loading would fail.

---

## First drive

The MCP server is registered by [.mcp.json](.mcp.json) (server name `bioinf`) — Claude
Code discovers it automatically when launched from the repo root. Approve the server if
prompted, then ask for what you want:

```bash
claude
> Install samtools 1.21 and freeze it into an HPC-shippable image.
```

Headless / scripted (note the MCP tool grant — without it every call is denied):

```bash
claude -p "Install samtools 1.21 and freeze it into an HPC-shippable image. \
Report the freeze_request_key and the ENV report path." \
  --allowedTools "mcp__bioinf__*"
```

Either way, the deliverables land in `env_reports/`:

| Artifact | What it is |
|----------|-----------|
| `samtools_env.ENV.html` | the env report — requested vs installed, evidence commands, validation locus |
| `samtools_env.recipe.md` / `.recipe.yaml` | the build recipe — rebuild the image with no agent involved |
| `samtools_env.attestation.json` | in-toto/SLSA provenance |

A first freeze often self-reports **`degraded`** with named disclosure reasons (e.g.
"evidence is a presence probe, not a functional run"). That is the honesty contract
talking, not a failure: the artifact states exactly what was and wasn't observed, and
what stronger evidence would take.

Sanity checks that don't need an MCP client:

```bash
python -m agent          # direct server launch (from the repo root, runtime env active) — prints the startup banner
bioinf-mcp               # same entry point, installed as a console script in the runtime env
```

---

## How it works — three environment layers, two lifecycles

| Layer | Where | Role |
|-------|-------|------|
| **runtime env** | `./.conda_runtime/` | runs the MCP server itself; created by `setup.sh`, per-clone |
| **iteration envs** | `envs/` | per-tool conda envs the agent spins up while solving an install (plus `bioinf_core_tools`, the bootstrap toolkit) |
| **frozen images** | Docker daemon / `docker_images/` | **the product**: content-addressed images validated inside the bytes that ship (Apptainer `.sif` on HPC) |

On top of the frozen image sit the two lifecycles:

- **Layer 1 — the environment** (`freeze`). Solved *once*: build with the install
  primitives (conda / pip / R / JAR / binary / source / cargo / go / perl), then
  `freeze()` produces the digest-addressed, HPC-shippable image and registers it in an
  on-disk cache — a later identical request returns it by hash. *Validated == shipped.*
- **Layer 2 — the workflow** (`seal_workflow`). *Consumes* a frozen env by digest,
  validates the run-side invariants, and writes a machine-verified `WorkflowSpec` plus
  an HTML run dashboard (`{name}.RUN.html`) rendered from the passing run.

The flow the agent walks, tool by tool:

1. `start_pipeline(name, description)` → a `pipeline_id` threaded through every call.
2. Install primitives build the env (conda first; `resolve_tool` ranks the tiers when unsure).
3. `run_pipeline_step(...)` — run the tool on test inputs; outputs are auto-validated.
4. `freeze(env, tools, ...)` — **Layer 1**; returns a `freeze_request_key`.
5. `run_step_in_container(freeze_request_key, ...)` — re-run *inside* the frozen image.
6. `seal_workflow(pipeline_id, freeze_request_key)` — **Layer 2**; writes the sealed spec + dashboard.

`list_installed_pipelines()` shows what's already solved here — ask before solving a tool twice.

---

## HPC bridge (optional)

To drive a real SLURM cluster, author a `projects_access.yaml` at the repo root
(gitignored — it's personal). Start from the annotated template:

```bash
cp agent/skills/projects_access.yaml.example projects_access.yaml
# fill in: your cluster's ssh host, scratch/common-data paths, per-project directories
./scripts/setup.sh --check   # confirms the file parses and shows what it declares
```

It declares your compute environments (ssh host, transfer protocol, SLURM defaults) and
per-project authorized directories with discrete permissions. Every bridge primitive
(`snapshot_project`, `run_step_on_cluster`, `submit_workflow_job`, …) is gated by that
file, and every transfer is checksum-verified. Playbooks:
[Phase A](docs/hpc_bridge_phase_a_playbook.md) (read + small transfers),
[Phase B](docs/hpc_bridge_phase_b_playbook.md) (submit → poll → fetch).

---

## Tests

```bash
pytest                                          # the project suite (scoped to tests/)
pytest -m "not live and not integration_docker" # the fast hermetic honesty tier (what CI runs)
pytest -m live                                  # opt-in: hits real package registries over the network
```

## Docs

- [CLAUDE.md](CLAUDE.md) — the full honesty contract, every primitive, and the protocol.
- [docs/](docs/) — architecture, the HPC bridge, and the outcomes/intent dashboards.

## License

BSD 3-Clause — see [LICENSE](LICENSE).
