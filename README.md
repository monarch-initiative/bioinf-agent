# bioinf-agent

Install a bioinformatics tool once, get back an artifact you never have to second-guess.

`bioinf-agent` installs bioinformatics tools into isolated conda environments,
**validates them inside the very image it ships**, packages them as HPC-shippable
containers, and emits a machine-verified spec. Call it once per tool/version, get a
content-addressed artifact, and never look at the install again.

The distinguishing property is an **honesty contract**: nothing is taken on faith. The
environment is built and validated *inside the image it ships*, so "install" and "ship"
are one event, and every report is rendered **purely from the verified record** — it
cannot present a requested version as an installed one. Full contract: [CLAUDE.md](CLAUDE.md).

---

## Setup

You need **Docker** (daemon running) and an **MCP client** — [Claude Code](https://claude.com/claude-code)
is the one this is developed against. Python and conda are not yours to manage: setup
creates a repo-local runtime env, and if the machine has no conda at all it offers to
install a private miniforge under the repo.

```bash
git clone https://github.com/monarch-initiative/bioinf-agent && cd bioinf-agent
./scripts/setup.sh          # ~4 min — runtime env, deps, core toolkit, chr22 test data
./scripts/config.sh         # optional — only if you want to drive an HPC cluster
```

`setup.sh` is idempotent; re-run it any time.

| | |
|---|---|
| `./scripts/setup.sh --check` | systems check — PASS/FAIL per requirement, every FAIL names its fix |
| `./scripts/setup.sh --full` | also fetches the full read-dataset corpus (multi-GB) |
| `./scripts/setup.sh --yes` | non-interactive consent, for CI and scripted installs |

> **Editable install, on purpose.** This is a workspace-rooted service, not a
> site-packages library: it reads `config/` and `scripts/` from the checkout it was
> installed from. A plain `pip install .` would relocate the code away from them.
>
> **Setup also asks where your WORKSPACE goes** — the directory the agent writes
> everything into. It is never inside the checkout: envs, images, reports and sealed
> specs outlive any clone, and a system whose product is auditable artifacts cannot keep
> them in its own git tree. The answer is recorded in `.bioinf_workspace`;
> `./scripts/setup.sh --check` prints every resolved location.

---

## First drive

The server is registered in [.mcp.json](.mcp.json) as `bioinf`, so Claude Code finds it
when launched from the repo root. Approve it if prompted, then ask for what you want:

```bash
claude
> Install samtools 1.21 and freeze it into an HPC-shippable image.
```

Headless — note the tool grant, without which every call is denied:

```bash
claude -p "Install samtools 1.21 and freeze it into an HPC-shippable image. \
Report the freeze_request_key and the ENV report path." \
  --allowedTools "mcp__bioinf__*"
```

The deliverables land in your workspace's `reports/` — `./scripts/setup.sh --check`
prints the path:

| Artifact | What it is |
|----------|-----------|
| `{name}.ENV.html` | the env report — requested vs installed, evidence commands, validation locus |
| `{name}.recipe.md` / `.recipe.yaml` | the build recipe — rebuild the image with no agent involved |
| `{name}.attestation.json` | in-toto/SLSA provenance |

A first freeze often reports **`degraded`** with named reasons. That is the contract
talking, not a failure: the environment is registered and shippable, and the tag says how
much was *observed*. The commonest reason is inherent to the route the docs prefer — a
pre-built BioContainer carries no record of the binaries it shipped. The outcome
distinguishes that from a gap you can close, and names what would close it.

Without an MCP client:

```bash
python -m agent      # direct launch from the repo root — prints the startup banner
./scripts/config.sh --show     # what the HPC bridge is configured to reach
```

---

## Configuration

Two files, and only one of them is yours to edit by hand.

**`projects_access.yaml`** (at your workspace root — it's personal, and it describes a
compute world that outlives any checkout) is the agent's
command-and-control file: which clusters exist, which projects may use them, and exactly
which directories the agent may list, upload to, download from, or run jobs in. Every
bridge primitive is gated by it, and nothing in it is inferred. Author it with the menu:

```bash
./scripts/config.sh              # the menu: compute envs, projects, directories, save
./scripts/config.sh --show       # print the current configuration
./scripts/config.sh --validate   # rc 0 when the agent's loader accepts it
```

The menu validates every save against `compute_access.load_access` — the same loader the
agent enforces at drive time — so it cannot write a file that fails later. It keeps the
previous version as `projects_access.yaml.bak`; hand-editing is fine, but a menu save
rewrites the file and drops hand-written comments. The annotated schema reference is
[projects_access.yaml.example](agent/skills/projects_access.yaml.example).

Permissions are **discrete, not a ladder**: `file_name_only`, `upload`, `download` and
`exec` are granted one by one, and `upload` does not imply `download`. Anything not
listed is denied.

**`config/agent_config.yaml`** holds conda channels, the default Python, and the install
timeout. Edit it by hand; it rarely changes.

---

## How it works

Three environment layers:

| Layer | Where | Role |
|-------|-------|------|
| **runtime env** | `./.conda_runtime/` | runs the MCP server itself; created by `setup.sh`, per-clone |
| **iteration envs** | `<workspace>/environments/conda/` | per-tool conda envs used while solving an install |
| **frozen images** | Docker / `<workspace>/environments/images/` | **the product**: content-addressed images validated inside the bytes that ship |

Two lifecycles on top of them:

- **Layer 1 — the environment** (`freeze`). Solved *once*: build with the install
  primitives (conda / pip / R / JAR / binary / source / cargo / go / perl), then produce
  the digest-addressed, HPC-shippable image and register it. An identical later request
  returns it by hash. *Validated == shipped.*
- **Layer 2 — the workflow** (`seal_workflow`). *Consumes* a frozen env by digest,
  validates the run-side invariants, and writes a machine-verified `WorkflowSpec` plus an
  HTML run dashboard rendered from the passing run.

The flow the agent walks:

1. `start_pipeline(name, description)` → a `pipeline_id` threaded through every call.
2. Install primitives build the env (conda first; `resolve_tool` ranks the tiers when unsure).
3. `run_pipeline_step(...)` — run the tool on test inputs; outputs are auto-validated.
4. `freeze(env, tools, ...)` — **Layer 1**; returns a `freeze_request_key`.
5. `run_step_in_container(freeze_request_key, ...)` — re-run *inside* the frozen image.
6. `seal_workflow(pipeline_id, freeze_request_key)` — **Layer 2**; writes the sealed spec.

`list_installed_pipelines()` shows what is already solved here — ask before solving a
tool twice.

---

## HPC

Once `projects_access.yaml` declares a cluster, the same frozen environment runs there.
Two chains, deliberately separate:

- **Validate** — `run_step_on_cluster` stages the `.sif`, submits, polls, fetches outputs
  back and records a cluster-locus step you can seal. Runs in the agent's own scratch
  sandbox.
- **Produce** — `stage_apptainer_image` → `submit_workflow_job` → `cluster_job_status` →
  `download`, against the directories you declared. Submit-and-document: production jobs
  run for hours, so the agent does not sit and watch.

Transfers are sha256 round-tripped (or end-to-end checksummed, if you configure Globus).
Playbooks: [Phase A](docs/hpc_bridge_phase_a_playbook.md) (read + small transfers),
[Phase B](docs/hpc_bridge_phase_b_playbook.md) (submit → poll → fetch).

ssh uses `BatchMode`, so nothing ever prompts for a password: open `ssh <your-host>` in a
separate terminal and leave it open — every bridge call rides that ControlMaster socket.

---

## Tests

```bash
pytest                                          # the project suite
pytest -m "not live and not integration_docker" # the fast hermetic tier (what CI runs)
pytest -m live                                  # opt-in: hits real package registries
```

## Docs

- [CLAUDE.md](CLAUDE.md) — the full honesty contract, every primitive, and the protocol.
- [docs/](docs/) — architecture, the HPC bridge, and the outcomes/intent dashboards.

## License

BSD 3-Clause — see [LICENSE](LICENSE).
