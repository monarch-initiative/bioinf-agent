# HPC Bridge Phase 2 — Actuators

**Status:** HISTORICAL DESIGN DOC. Phase 2 has since shipped; the
implemented surface differs from the primitive names sketched below.
In particular, the six zone-specific transfer primitives proposed here
(`upload_to_scratch`, `download_from_scratch`, `upload_to_common_data`,
…) were collapsed into TWO unified primitives — `transfer.upload` /
`transfer.download` — that auto-route authorization by which zone the
absolute remote path falls in (scratch / common_data / container_upload
/ project_path). Likewise `submit_cluster_job` shipped as the split
pair `submit_workflow_job` (production) + `run_step_on_cluster`
(validation/seal). **For the current contract, read the "HPC bridge —
Phase 2" section of CLAUDE.md, not this file.** This doc is retained
for the design rationale (the auth model, the cheat-guard posture, the
walls) which still holds.
**Date:** 2026-06-04
**Prereq context:** Read CLAUDE.md (the two-layer architecture), `agent/skills/projects_access.yaml.example` (the bridge schema), and `agent/skills/snapshot.py` (Phase 1, the read-only bridge).

---

## What this is

Phase 1 of the HPC bridge shipped `snapshot_project` — read-only, one-level
visibility into the user's cluster project tree. Phase 2 adds the **actuator
surface**: the agent can push files, submit jobs, monitor them, and fetch
results back. The whole thing stays under explicit per-project authorization
in `projects_access.yaml` and every actuator carries refuse-to-emit
semantics in the same shape as Phase 1's L14 cheat-guards.

This unlocks T8 ("HPC delivery") in the capability spectrum, with the
specific motivating case being **Exomiser** — a multi-GB reference DB +
heavy JVM tool that cannot honestly be validated on a laptop.

---

## Goals

1. **The agent can ship a verified workflow end-to-end to the cluster.**
   Today the agent produces a sealed WorkflowSpec locally; afterward the
   user copies the .sif and runs it themselves. After Phase 2: the agent
   uploads, submits, monitors, fetches outputs, and re-verifies them
   locally — without ever needing a raw shell on the cluster.

2. **Big-data pipelines (Exomiser-class) become tractable.** The agent can
   trigger a SLURM-batch reference-data download into a designated location
   declared in `projects_access.yaml`, then trigger a workflow run that
   consumes it.

3. **The trust contract extends across the laptop/cluster boundary.** Every
   anchor we use locally (command verbatim, sha256, container digest,
   resource usage) is captured cluster-side and re-verified locally on
   fetch. **The cluster is never trusted — it's an executor; the verifier
   stays local.**

## Non-goals

- **Raw ssh / arbitrary bash on the cluster.** If a behavior isn't covered
  by a primitive, we build the primitive. We do not open an escape hatch.
- **Interactive nodes / SLURM `srun`.** Out of scope. If the user needs
  interactive work, they do it themselves in their own terminal.
- **Nextflow this phase.** Single-step `sbatch` covers the motivating
  cases. Nextflow earns its keep when we have a real multi-step DAG
  (Phase 4+).
- **Globus transfer integration this phase.** Curl-resume in a SLURM job
  covers most "download a big DB" cases. Globus (the center's recommended
  large-file path) gets its own phase — see [Open question Q1](#open-question-q1-globus--uncs-large-file-transfer-service) below.
- **Cluster-side `seal_workflow` as a separate primitive.** We thought
  about it; turns out unnecessary. See [Trust model](#trust-model-cluster-is-an-executor-verifier-stays-local).

---

## Trust model: cluster is an executor; verifier stays local

The cluster runs the workflow; the laptop verifies. Every actuator records
an anchor that is **re-checkable locally** on fetch. If the cluster cheats
(or anything goes wrong) the local re-verify fails and we refuse to seal.

| Anchor | Captured where | Verified where |
|---|---|---|
| Command verbatim (the full sbatch script) | Local at submit time | Local — script is held in the draft |
| SLURM job ID | Cluster (sbatch return) | N/A |
| Container image digest | Cluster (`singularity inspect`) inside the job | Local on fetch — compared to the env's frozen digest |
| Output file sha256 | Local on `fetch_from_scratch` | Local — recorded in pipeline_step.output_sha256 |
| Job stdout/stderr | Cluster (job log files) | Local on fetch — held with the step record |
| Wall / peak RSS / cores | Cluster (`sacct` after job exits) | Local on fetch — stamped onto resource_usage with i7_authoritative=true (native locus) |

Because every anchor lives locally after fetch, **`seal_workflow` works
unchanged**. It validates the same I3/I4/I6/I7/I8 invariants against the
fetched artifacts. There is no "cluster_seal" primitive. The cluster never
produces a sealed spec; the local agent does, against fetched evidence.

**Why this works:** the validators (samtools view, bcftools view, json.loads,
sha256, GNU time output parsing) all run locally on the fetched bytes.
The cluster could lie about whether it ran the job — but it cannot
produce bytes that pass type-aware validation if it didn't actually run
the tool on real data.

---

## The 5 new primitives

Each is a discrete deliverable. Each has refuse-to-emit. Each records its
action in a structured way the audit trail can re-verify.

### 1. `upload_to_refdata(project, local_path, ref_name) -> dict`

Push a file to the project's `reference_data_targets[<ref_name>].path`.

```python
def upload_to_refdata(project: str, local_path: str, ref_name: str) -> dict:
    """
    Push a local file to the project's authorized reference-data target.

    Permission model: the named `ref_name` must exist in
    projects_access.yaml under the project's compute_env's
    reference_data_targets, AND that block's permissions must include
    'upload'.

    Returns: {success, remote_path, sha256, bytes, duration_s}
    Refuses on:
      - ref_name not declared in projects_access.yaml
      - permissions on the target don't include 'upload'
      - local_path doesn't exist or isn't a regular file
      - sha256 round-trip mismatch (transfer corruption)
      - traversal characters in ref_name (..)
    """
```

**Cheat-guards (L14-style tests):**

- `upload_to_refdata` refuses a `ref_name` not in the active project's targets
- refuses an `ref_name` declared in ANOTHER project (cross-project leak)
- refuses paths with `../` in `ref_name`
- refuses if target permissions = `[upload]` is missing
- refuses symlinks in `local_path` (don't follow user's local sym tricks)
- verifies sha256 round-trip; refuses on mismatch
- works under ControlMaster; bare-call without `ssh hpc-agent` running gives the hint
- the underlying scp invocation cannot be substituted with `ssh hpc-agent <arbitrary>` (template is closed)

**Transfer mechanism:** scp over the existing ssh ControlMaster socket
(zero-zombie pattern from Phase 1). For files >5GB consider a chunked
transfer with `rsync --partial --progress` for resume. Globus deferred
(see Q1).

### 2. `upload_to_scratch(project, local_path, remote_subpath) -> dict`

Push a local file into the project's `agent_scratch_target.path/<remote_subpath>`.

```python
def upload_to_scratch(project: str, local_path: str, remote_subpath: str) -> dict:
    """
    Push a local file into the agent's scratch sandbox on the cluster.

    `remote_subpath` is a relative path inside the scratch root; subdirs
    will be created. The scratch root is the active compute_env's
    `agent_scratch_target.path`.

    Returns: {success, remote_path, sha256, bytes, duration_s}
    Refuses on:
      - no agent_scratch_target on the project's compute_env
      - permissions doesn't include 'upload'
      - remote_subpath has traversal (..) or is absolute
      - local_path doesn't exist
      - sha256 mismatch
    """
```

**Cheat-guards:**

- refuses absolute `remote_subpath` (e.g. `/etc/passwd`)
- refuses traversal in `remote_subpath`
- refuses if scratch target undeclared or wrong permissions
- refuses if `remote_subpath` resolves outside the scratch root after
  normalization (defense in depth)
- sha256 round-trip required

### 3. `submit_cluster_job(project, ...) -> dict`

The heart of Phase 2. Submit a SLURM batch job whose command is
structurally constrained to one of three modes.

```python
def submit_cluster_job(
    project: str,
    command: str,
    job_name: str,
    job_type: str,                  # "run" | "data_acquisition" | "diagnostic"
    workflow_spec_path: str = "",    # required when job_type="run"
    cores: int = 1,
    mem_gb: int = 4,
    time_hours: int = 1,
    queue: str = "",                # defaults to slurm.queue_default
    workdir_subpath: str = "",      # within scratch root
    container_image: str = "",      # the .sif path on the cluster
    job_id_hint: str = "",
) -> dict:
    """
    Submit a SLURM batch job under the project's compute_env.

    `job_type` gates what `command` can contain:

      - "run":              command MUST be a substitution of
                            WorkflowSpec.usage.command_template found at
                            workflow_spec_path. Substitution preserves
                            placeholder order; only the bound values change.
                            container_image is asserted against the
                            WorkflowSpec's env digest via `singularity inspect`
                            in the job's prologue.

      - "data_acquisition": command MUST come from the constrained
                            DATA-ACQUISITION template set (curl/wget/rsync
                            with explicit URL + sha256 + extract). Free-form
                            shell rejected. Target path MUST be in a
                            reference_data_targets entry with permissions
                            including 'upload'.

      - "diagnostic":       command MUST come from the constrained
                            DIAGNOSTIC template set (sacct/squeue/sinfo,
                            singularity inspect, ls of authorized paths).
                            Defensive surface for the agent to check
                            cluster state without raw shell.

    Records (durably):
      - the full SBATCH script we generated (verbatim)
      - the cluster's returned job_id
      - submission timestamp
      - workflow_spec_path (if "run") + its sha256 at submit time
      - container_image's expected digest

    Returns: {success, job_id, submit_script, sbatch_log_path}.
    Refuses on:
      - any job_type/command combination outside the three templates
      - workflow_spec_path absent or unsealed when job_type="run"
      - container_image not in EnvCache OR its uploaded SIF missing
      - cores/mem/time exceeds the compute_env's slurm.max_*
      - queue not in slurm.allowed_queues
      - workdir_subpath outside scratch
    """
```

**Cheat-guards — many. This is the hot zone.**

- `job_type="run"` with command that doesn't structurally match the spec's
  template → refused (placeholder mismatch, extra command tokens, etc.)
- `job_type="run"` with mutated container_image (different digest) → refused
- `job_type="data_acquisition"` with `command="curl X && rm -rf /"` → refused
  (template is curl-only, no `&&`)
- `job_type="data_acquisition"` with destination outside refdata → refused
- `job_type="diagnostic"` with `command="squeue && cat /etc/shadow"` → refused
- attempt to set `--mail-user=evil@badguy.com` via job_name with newline injection → refused
- attempt to set `--export=ALL` (env leak) → forced to empty `--export=NONE`
- the SBATCH script can't include `srun -interactive` or `salloc`
- container_image SIF must exist at the cluster path we claim (verified by
  `ssh -- ls -la PATH` before submit)

The submit script the primitive generates is a CLOSED TEMPLATE:

```bash
#!/bin/bash
#SBATCH -J {job_name}
#SBATCH --ntasks=1
#SBATCH --cpus-per-task={cores}
#SBATCH --mem={mem_gb}G
#SBATCH --time={time_hours}:00:00
#SBATCH -o {workdir}/logs/{job_id_hint}.out
#SBATCH -e {workdir}/logs/{job_id_hint}.err
#SBATCH --export=NONE          # no env leak from submit host
#SBATCH -p {queue}

set -euo pipefail
cd {workdir}

# Captured for audit (the primitive writes these BEFORE submit):
echo "AGENT_AUDIT: job_type={job_type}"
echo "AGENT_AUDIT: container_image_expected={container_image}"
echo "AGENT_AUDIT: workflow_spec_sha256={workflow_spec_sha256_if_any}"
echo "AGENT_AUDIT: submitted_at={timestamp}"

# Validate the container's digest matches what we think
singularity inspect --json {container_image} > .container_inspect.json
echo "AGENT_AUDIT: container_digest=$(jq -r .data.attributes.fingerprints .container_inspect.json | head)"

# The real command
{command}

# Final marker
echo "AGENT_AUDIT: completed_at=$(date -Iseconds)"
```

The agent NEVER writes its own SBATCH headers. The primitive owns the
template; the agent supplies only the slots the template allows.

### 4. `cluster_job_status(project, job_id) -> dict`

Read-only state query.

```python
def cluster_job_status(project: str, job_id: str) -> dict:
    """
    Returns: {
        state: "PENDING" | "RUNNING" | "COMPLETED" | "FAILED" | "CANCELLED" | "TIMEOUT",
        exit_code: int | None,
        sacct: {...},                # parsed sacct output (alloc cpus, mem,
                                     # max RSS, wall, etc.)
        log_tail_stdout: str,        # last ~30 lines
        log_tail_stderr: str,
    }
    Refuses on:
      - job_id not owned by the configured cluster user
        (parsed from sacct's User field)
    """
```

**Cheat-guards:**

- queries someone else's job → returns error, never their data
- malformed job_id → refused without ssh call
- `sacct` uses `--format=...` constrained to a fixed list of fields

### 5. `fetch_from_scratch(project, remote_subpath, local_path) -> dict`

Pull a file from agent scratch back to local.

```python
def fetch_from_scratch(project: str, remote_subpath: str, local_path: str) -> dict:
    """
    Pull a file from the agent's cluster scratch back to a local path.

    Returns: {success, sha256, bytes, duration_s}
    Refuses on:
      - remote_subpath outside agent_scratch_target after normalization
      - remote_subpath is a symlink (defense — don't be tricked into
        fetching /etc/passwd if the cluster compromised the agent's scratch)
      - local_path's parent doesn't exist
    """
```

**Cheat-guards:**

- remote_subpath that resolves outside scratch (via symlinks) → refused
- remote_subpath that's a directory (use multiple fetches; primitive is file-scoped) → refused
- transfer corruption (sha256 not stable across the wire) → refused

---

## `projects_access.yaml` schema extensions

The schema grows by THREE optional blocks under each `compute_envs[]`
entry. Each is added only as the primitive that consumes it ships
(the [feedback-architecture-discipline](../) rule against pre-declared
empty config slots).

```yaml
compute_envs:
  - name: hpc_cluster
    type: ssh
    host: hpc-agent
    user: USER

    # EXISTS (Phase 1)
    container_upload_target:
      path: /scratch/USER/containers/
      permissions: [upload]
      description: "Where .sif tarballs land"

    # NEW (Phase 2): the agent's writable sandbox
    agent_scratch_target:
      path: /scratch/USER/agent_workspace/
      permissions: [upload, fetch, exec]
      description: "Agent sandbox for noodling, logs, test data, drafts"

    # NEW (Phase 2): named reference-data destinations
    reference_data_targets:
      - name: exomiser_data
        path: /work/users/U/USER/ref/exomiser/
        permissions: [upload, exec]    # exec = jobs can read
        description: "Exomiser reference (15GB), Monarch Initiative"
      - name: gnomad_v4
        path: /work/users/U/USER/ref/gnomad_v4/
        permissions: [upload, exec]
        description: "gnomAD v4 vcfs (50GB)"

    # SLURM scheduler policy (all keys optional; see projects_access.yaml.example)
    slurm:
      account: "tislab"
      partition: "general"
      # Globus deferred (Q1)
```

**Validation rule for the schema:** the loader refuses a `compute_env` whose
`agent_scratch_target.path` overlaps any `reference_data_targets[].path`
or `container_upload_target.path`. They must be disjoint subtrees so a
breach of one doesn't grant access to another.

---

## Audit trail — what gets recorded where

For every cluster job:

```
draft.pipeline_steps[N] += {
    "command": "<original command, verbatim>",
    "ran_in_container": True,
    "container_image": "<sif path>",
    "container_image_digest_expected": "<from EnvCache>",
    "container_image_digest_observed": "<from singularity inspect log>",
    "cluster_job": {
        "project": "...",
        "compute_env": "hpc_cluster",
        "job_id": "12345678",
        "submit_script": "<full SBATCH script verbatim>",
        "submit_script_sha256": "<hex>",
        "submitted_at": "<iso>",
        "queue": "general",
        "sacct": {
            "state": "COMPLETED",
            "exit_code": 0,
            "alloc_cpus": 4,
            "alloc_mem_gb": 8,
            "max_rss_kb": 6_134_000,
            "wall_seconds": 1834,
        },
        "log_paths": {
            "stdout_local": "<fetched>",
            "stderr_local": "<fetched>",
            "stdout_remote": "<cluster path>",
            "stderr_remote": "<cluster path>",
        },
    },
    "detected_outputs": [...],
    "output_sha256": { "/abs/path": "<hex>", ... },     # computed locally on fetch
    "resource_usage": {
        "wall_seconds": 1834,
        "peak_rss_kb": 6_134_000,
        "cores": 4,
        "i7_authoritative": True,                       # native locus
        "locus": "cluster:hpc_cluster:cpu",
    },
}
```

This is the same shape as a local `pipeline_step` plus the `cluster_job`
sub-record. The honesty contract's I3/I4/I6/I7/I8 walks the same fields
and refuses by the same rules.

---

## Open question Q1: Globus / the center's large file transfer service

This is the "complicated the HPC center service" — the answer is **Globus**, and
the integration deserves its own phase. Here's what would be involved:

**Why Globus matters at the HPC center:**
- The recommended path for >50GB datasets on hpc_cluster
- Real benefits: resumable transfers, parallel streams, network-aware
  retries, integrity verification baked in
- the HPC center has registered hpc_cluster as a Globus collection; many public datasets
  (Ensembl, EBI, UCSC, some NIH archives) live on Globus-connected endpoints
- Doesn't help for arbitrary HTTPS URLs (Monarch Initiative's Exomiser
  data downloads are HTTPS — no Globus endpoint published)

**Auth model — fits our pattern:**
- User runs `globus login` interactively, once. Session lasts ~24h.
- Agent uses `globus transfer ...` CLI, which inherits the session.
- Same shape as our ssh ControlMaster pattern: user authenticates
  interactively; agent never sees credentials.

**Proposed primitive (Phase 3):**

```python
def globus_transfer(
    project: str,
    source_endpoint: str,   # UUID or registered alias
    source_path: str,
    dest_target_name: str,  # name of an entry in reference_data_targets
    sync_wait: bool = True, # block on completion vs. return task_id
) -> dict:
```

**Constraints:**
- `source_endpoint` must be in projects_access.yaml's
  `globus.allowed_source_endpoints` whitelist (per-project, not global)
- `dest_target_name` resolves to a `reference_data_targets[].path` —
  same authorization as `upload_to_refdata`
- Returns Globus task_id; agent polls with `globus_task_status` (a small
  secondary primitive)
- Audit: the Globus task record (source endpoint, transferred bytes,
  checksum mode, completion timestamp) gets attached to the
  ReferenceDatabase entry in the draft

**Defer rationale:** for the Exomiser case (which is HTTPS-only on the
source side), curl-in-a-SLURM-job with `-C -` resume and a sha256
manifest check is sufficient. Globus earns its keep on the *next* case
(gnomAD via the Broad's Globus endpoint, or a TB-scale dataset). Build
the primitive when we hit a case it solves.

**One non-obvious thing about Globus:** the destination endpoint
(hpc_cluster) must have the file path it's writing to **already exist** as
a Globus-shared collection. That's a one-time the HPC center setup per directory
tree, not a per-transfer thing. We just have to remember the dest path
must be inside the registered collection root.

---

## Open question Q2: Nextflow

Defer until Phase 4. For Phase 2:

- The motivating cases (Exomiser data acquisition, single-sample
  Exomiser run, a samtools view smoke test) are single-job SLURM
  submits. Nextflow would be ceremony.
- When we hit a real T4 multi-env DAG (e.g., basecall → align →
  variant call → annotate, each in its own env), nextflow's value
  becomes obvious.
- The integration when we do build it: `submit_cluster_job` grows a
  fourth mode, `job_type="nextflow"`, where `command` is a
  `nextflow run main.nf -profile slurm -c custom.config` invocation.
  The `main.nf` must be authored via `stage_authored_artifact` (so
  its sha256 is anchored), the `custom.config` is generated by the
  primitive from `slurm.*` settings, and the nextflow `.nextflow/cache`
  directory gets fetched as part of the audit trail.

---

## Open question Q3: cluster-side `seal_workflow`?

Considered, rejected. Reasoning: every anchor we need to verify is
locally checkable after fetch. The cluster runs the workflow; the
local agent re-verifies on the fetched bytes (sha256, validators,
container digest, sacct wall time). `seal_workflow` works unchanged.
A cluster-side seal would just be a redundant copy of the same
contract that the cluster could fake. Don't ship it.

---

## Implementation plan

Each phase is a discrete deliverable; commit per phase. Tests gate
each step.

### Step 1: schema + loader
- Extend `agent/skills/compute_access.py` to validate the three new
  blocks (`agent_scratch_target`, `reference_data_targets`, `slurm`).
- Add disjoint-subtree check across the three "target" blocks.
- Add L14 tests pinning rejection of mis-configured schemas.
- Update `agent/skills/projects_access.yaml.example`.

### Step 2: `upload_to_scratch` + `fetch_from_scratch`
- Smallest scope; round-trip is the test (upload a file, fetch it
  back, sha256 match).
- 10-12 L14 cheat-guards each.
- These two are paired — easiest to build and test together.

### Step 3: `upload_to_refdata`
- Same shape as `upload_to_scratch` but to ref data targets.
- The "named target" lookup is the new piece.
- L14 cheat-guards including cross-project leak.

### Step 4: `submit_cluster_job` (job_type="diagnostic" only)
- Start with the smallest, safest job type — `sacct`, `squeue`,
  `singularity inspect`. No actual workflow execution.
- This proves the SBATCH template generation, the job_id capture, the
  audit-record shape.
- 15+ L14 cheat-guards (the template-injection tests).

### Step 5: `cluster_job_status`
- Read-only; no actuation. Probably the easiest of the actuator
  primitives.

### Step 6: `submit_cluster_job` (job_type="data_acquisition")
- Add the curl/wget/rsync template set.
- Cheat-guards: refuse `;`, `&&`, `||`, `$()`, backticks in command,
  refuse destinations outside refdata, refuse missing sha256 anchor.
- Manual test: download a small fixture (say a 1MB tar) into a real
  refdata location, verify the file appears with correct hash.

### Step 7: `submit_cluster_job` (job_type="run")
- The big one. Structural command matching against a
  `WorkflowSpec.usage.command_template`.
- Container digest verification in the job prologue.
- End-to-end test: T2 workflow (samtools view on a BAM), shipped to
  cluster, fetched, locally re-sealed.

### Step 8: Exomiser as the real-world stress
- Use `add_core_test_data` or `phenopacket_to_vcf` to generate a tiny
  test VCF.
- Build env: install_jar_tool(exomiser) + install_conda_packages(deps)
- T8 acquisition: submit a data_acquisition job for the Exomiser data
  bundle (~15GB)
- T2 run: submit a run job for `exomiser --analysis test.yml --vcf x.vcf`
- Fetch + locally `seal_workflow`
- Document the gaps and ergonomic friction. They'll inform Phase 3.

### Step 9 (deferred): Globus
- Only when we hit a case it solves.

### Step 10 (deferred): Nextflow
- Only when we hit a multi-step DAG.

---

## Test plan

For each primitive:
- **L14 cheat-guards** (refuse-to-emit tests against `tests/integration/honesty/L14_compute_env_safety/`)
- **E2E happy path** against the local envs/ directory simulator
  (Phase 1 pattern: pretend a local dir is "the cluster") — fast,
  no network
- **Manual smoke test** against the real hpc_cluster when the primitive
  is in flight (user-initiated)

Existing patterns to reuse:
- `tests/integration/honesty/L14_compute_env_safety/` for refusal tests
- `tests/integration/correctness/test_snapshot_local_envs_e2e.py` for
  local-dir-as-cluster e2e

---

## Estimated cost

| Step | LOC | L14 tests | E2E tests | Sessions |
|---|---|---|---|---|
| 1. Schema + loader | ~200 | 8-10 | 0 | 0.5 |
| 2. upload_to_scratch + fetch_from_scratch | ~350 | 20 | 4 | 1 |
| 3. upload_to_refdata | ~150 | 10 | 2 | 0.5 |
| 4. submit_cluster_job (diagnostic) | ~300 | 15 | 2 | 1 |
| 5. cluster_job_status | ~120 | 6 | 2 | 0.3 |
| 6. submit_cluster_job (data_acquisition) | ~200 | 15 | 2 | 0.7 |
| 7. submit_cluster_job (run) | ~400 | 20 | 4 | 1.5 |
| 8. Exomiser stress | (no new code) | (no new tests) | 1 | 1 |
| **Total** | **~1700** | **~95** | **17** | **~6.5** |

That's roughly 6-7 working sessions, each gated by tests, with a real
Exomiser shipment at the end.

---

## Connections

- CLAUDE.md — the canonical architecture
- `agent/skills/projects_access.yaml.example` — Phase 1 schema we extend
- `agent/skills/snapshot.py` — Phase 1, the read-only bridge
- `tests/integration/honesty/L14_compute_env_safety/` — Phase 1
  cheat-guard pattern this phase replicates
- Memory: `project-mcp-tools-split` (how to add a primitive), `feedback-mcp-tools-conventions` (the `_ms.X` rule), `feedback-architecture-discipline` (the 5 anti-patterns this design respects), `project-freeze-speed-framework` (where freeze() cost lives)
