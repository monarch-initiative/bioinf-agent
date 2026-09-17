# bioinf-agent

An AI-driven bioinformatics assistant in two halves. **Half 1** builds and validates
tool environments — installed, run against real data, and frozen as content-addressed
container images — and acquires the reference/test data they need. **Half 2** runs
*projects*: which environment a piece of work needs, where its data lives, wired
together over SLURM/bash/Nextflow on your laptop or your cluster.

Two properties distinguish it. Every artifact is held to an **honesty contract** —
environments are validated *inside the image that ships*, every report is rendered
purely from the verified record, and nothing an agent merely claims is taken on faith
(full contract: [CLAUDE.md](CLAUDE.md)). And **the system goes to the data**: you grant
it access to the directories where your data already lives — you never upload your data
into it.

---

## Setup

You need two things running: **Docker** (the daemon) and an **MCP client** —
[Claude Code](https://claude.com/claude-code) is the one this is developed against.
Everything else is self-served.

```bash
git clone https://github.com/monarch-initiative/bioinf-agent && cd bioinf-agent
./scripts/setup.sh          # ~4 min
```

`setup.sh` does five things, in order: finds conda (**asks** before installing a
private miniforge under the repo if the machine has none); **asks** where the artifact
store goes; creates the repo-local runtime env at `./.conda_runtime` and installs the
agent into it, editable and with the Globus CLI (editable on purpose — the code must
stay attached to this checkout); pulls the core toolkit and chr22 test data; runs the
systems check. It is idempotent — re-run it any time.

**The two questions it asks:**

1. **Install a private miniforge?** Only if no conda exists. Nothing outside the repo
   dir, no shell integration; delete `.miniforge/` to remove it.
2. **Where should the artifact store go?** The directory every generated artifact
   lives in — envs, images, reports, sealed specs, reference data. Press Enter for the
   offered default; the answer is recorded in `.bioinf_workspace`, and `--check`
   prints every resolved location. Never inside the checkout: artifacts outlive any
   clone. (The code and prompts call this directory *the workspace* — it is a
   per-machine store shared by all your sessions and projects, not a per-session
   thing.)

Scripted installs answer both up front: `BIOINF_WORKSPACE=/path ./scripts/setup.sh --yes`.

| | |
|---|---|
| `./scripts/setup.sh --check` | systems check — PASS/FAIL per requirement, every FAIL names its fix |
| `./scripts/setup.sh --full`  | also fetch the full read-dataset corpus (multi-GB) |
| `./scripts/setup.sh --yes`   | non-interactive consent, for CI and scripted installs |

---

## First drive

The server is registered in [.mcp.json](.mcp.json) as `bioinf`, so Claude Code finds it
when launched from the repo root. **Approve the server when prompted** (first launch
only), then ask for what you want:

```bash
claude
> Install samtools 1.21 and freeze it into an HPC-shippable image.
```

Headless — **the tool grant is required**; without it every call is denied:

```bash
claude -p "Install samtools 1.21 and freeze it into an HPC-shippable image. \
Report the freeze_request_key and the ENV report path." \
  --allowedTools "mcp__bioinf__*"
```

The deliverables land in the artifact store's `reports/` (`--check` prints the path):

| Artifact | What it is |
|----------|-----------|
| `{name}.ENV.html` | the env report — requested vs installed, evidence, validation locus |
| `{name}.recipe.md` / `.recipe.yaml` | the build recipe — rebuild the image with no agent involved |
| `{name}.attestation.json` | in-toto/SLSA provenance |

Two situations ask something of you:

- **A first freeze often reports `degraded`, with named reasons.** That is the contract
  talking, not a failure: the env is registered and shippable, and the tag says how much
  was *observed*. Each reason names what would close it — act on it or accept it.
- **License-gated tools** (Novoalign-class) are never fetched by the agent. Download the
  artifact under your own license and hand the local path over; the agent freezes it
  license-gated, so the record carries `redistributable: false` and the image is
  delivered tarball-only.

Any MCP client works, not just Claude Code — the tool surface is plain MCP (no client:
`python -m agent` runs the server directly). The sealed artifacts (`workflow.yaml` +
`recipe.yaml` + image digest) are designed to be self-contained — the spec re-checks
its own invariants standalone — so another agent or system can consume them without
this repo in the loop.

---

## Declaring compute (optional)

Local install/freeze/seal needs none of this. Declare compute when you want production
runs on your own machine, or a cluster:

```bash
./scripts/config.sh              # the menu: compute envs, projects, directories, save
./scripts/config.sh --web        # the same menu in the browser
./scripts/config.sh --show       # print the current configuration
./scripts/config.sh --validate   # rc 0 when the agent's loader accepts it
```

The menu writes `projects_access.yaml` at the artifact-store root: which compute
environments exist — `type: local` (this machine) and/or `type: ssh` (a cluster) —
and, for remote machines, exactly which of your directories the agent may touch.
Permissions are discrete grants (`file_name_only`, `upload`, `download`, `exec`);
anything not listed is denied. Every save is validated by the agent's own loader.
Hand-editing the file is fine, but a menu save rewrites it (dropping hand-written
comments) — the previous version is kept as `.bak`.

**What you'll need to type, per scenario:**

- **Local env** — nothing: press Enter through the defaults.
- **Cluster env** — your ssh host (or `~/.ssh/config` alias) and username; optionally
  your SLURM account/partition (discoverable later with `cluster_partitions`, so blank
  is fine). Zone paths default to a sensible layout under your cluster scratch.
- **Globus** (optional; the default scp wire needs no setup) — a one-time sequence the
  menu cannot do for you: `globus login` (browser OAuth), install
  [Globus Connect Personal](https://www.globus.org/globus-connect-personal) and grant
  it your local folders (Preferences → Access). The menu then *finds* the endpoint
  UUIDs for you — it detects your local endpoint and searches the remote by name.
- **An agent configuring this non-interactively** writes the YAML directly
  ([schema](agent/skills/projects_access.yaml.example)) and checks it with `--validate`.

**Before driving a cluster:** open `ssh <your-host>` in a separate terminal and leave
it open. Every bridge call rides that ControlMaster socket in BatchMode — nothing ever
prompts for a password. `--check` shows whether a live session exists.

---

## How it works

| Layer | Verb | What it produces |
|-------|------|------------------|
| **1 — the environment** | `freeze` | a digest-addressed, HPC-shippable image, built and validated *inside the bytes that ship*; an identical later request returns it by hash |
| **2 — the workflow** | `seal_workflow` | a machine-verified `WorkflowSpec` + run dashboard: real data ran, every output validated, every input traced, the how-to re-executed |

The flow the agent walks: `start_pipeline` → install primitives (conda first;
`resolve_tool` ranks the options) → `run_pipeline_step` on test data → `freeze` →
`run_step_in_container` (validated == shipped) → `seal_workflow`.
`list_installed_pipelines()` shows what is already solved — ask before solving a tool
twice.

On a cluster, the same frozen env drives two deliberately separate chains: **validate**
(`run_step_on_cluster` — stage the `.sif`, submit, poll, fetch, record a sealable step)
and **produce** (`stage_apptainer_image` → `submit_workflow_job` → `cluster_job_status`
→ `download` — submit-and-document; the agent does not babysit day-long jobs). All
transfers are checksum-verified end to end. Playbooks:
[Phase A](docs/hpc_bridge_phase_a_playbook.md) ·
[Phase B](docs/hpc_bridge_phase_b_playbook.md).

`config/agent_config.yaml` holds conda channels, default Python and the install
timeout; it rarely changes.

---

## Tests

pytest lives in the runtime env, so run it on that interpreter:

```bash
./.conda_runtime/bin/python -m pytest                                           # the project suite
./.conda_runtime/bin/python -m pytest -m "not live and not integration_docker"  # fast hermetic tier (what CI runs)
./.conda_runtime/bin/python -m pytest -m live                                   # opt-in: hits real package registries
```

## Docs

- [CLAUDE.md](CLAUDE.md) — the full honesty contract, every primitive, and the protocol.
- [docs/](docs/) — architecture, the HPC bridge, and the outcomes/intent dashboards.

## License

BSD 3-Clause — see [LICENSE](LICENSE).
