# HPC Bridge — Phase B test-drive playbook

**Goal:** drive the full submit→poll→fetch cycle end-to-end against
real hpc_cluster — samtools view on a small BAM, rendered as a Nextflow
one-process workflow, executed under apptainer, fetched back with
sha256 round-trip.

**Driver:** the agent (me), when the user opens `ssh hpc-agent` and
explicitly asks. Head-node hygiene per
[[feedback-no-cheeky-head-node-testing]] — exception case applies.

The playbook MIRRORS Phase A's structure: a tight MINIMUM PATH at the
top, the full storyboard underneath for thoroughness.

---

## What lives where

- **Project:** `phase_b_samtools_demo` in `projects_access.yaml`.
  Workspace: `/work/users/u/s/user1/CLAUDE_TEST_PROJECTS/phase_b_samtools_demo`
  with `[file_name_only, upload, download, exec]`.
- **Demo BAM:** `data/core_test_data_hg38/` — pick the smallest aligned
  BAM (`select_test_data(file_format="bam")` will route).
- **samtools env:** built via `freeze("samtools_view_demo", ["samtools"])`.
  The .sif lands locally; we upload it to common_data on hpc_cluster.

---

## 🎯 MINIMUM PATH (6 calls; ~30 sec cluster time end-to-end)

The submit→poll→fetch chain. Everything else under "Full playbook"
adds belt-and-suspenders.

| # | Call | Validates |
|---|---|---|
| 1 | `upload_to_project_path(... /CLAUDE_TEST_PROJECTS/phase_b_samtools_demo/inputs/test.bam, <local_bam>)` | scp + sha256 round-trip on a real BAM |
| 2 | `upload_to_common_data(... /CLAUDE_GENOMES/samtools/samtools_<v>.sif, <local_sif>)` | .sif lands in shared zone (no auto-prefix) |
| 3 | `submit_workflow_job(...)` | render + 3-file upload + sbatch returns job_id |
| 4 | `cluster_job_status(...)` (loop until COMPLETED) | sacct returns state changes; exit_code 0:0 |
| 5 | `download_from_project_path(... /workflow_dir/filtered.bam, <local_out>)` | output fetched + sha256 OK |
| 6 | local `samtools view filtered.bam | wc -l` | sanity: BAM actually parses (no truncation) |

**Green-light shape per call:**

| # | Expected |
|---|---|
| 1 | `success: True`, `remote_path` matches the requested abs_path, sha256 populated |
| 2 | `success: True`, `remote_path` in `/CLAUDE_GENOMES/samtools/`, sha256 populated |
| 3 | `success: True`, `job_id` is digit-only, `files_uploaded` has 3 entries (main.nf, nextflow.config, launcher.sh) |
| 4 | `jobs[0].state` transitions PENDING → RUNNING → COMPLETED; `exit_code: "0:0"` at the end |
| 5 | `success: True`, sha256 round-trip, local file size > 0 |
| 6 | nonzero record count — proves the filtered BAM is valid (not empty) |

The full 6-step ride exercises ALL three auth families (project_path
for BAM in + workflow files + BAM out, common_data for the .sif),
both transfer directions (upload + download), and the
render→upload→sbatch chain that is `submit_workflow_job`'s reason for
existing.

---

## Pre-flight

1. Open `ssh hpc-agent` in a side terminal. Leave it open.
2. Ensure `projects_access.yaml` has the `phase_b_samtools_demo`
   project pointing at `/work/users/u/s/user1/CLAUDE_TEST_PROJECTS/
   phase_b_samtools_demo/` with `[file_name_only, upload, download, exec]`.
3. Ensure the workspace + `inputs/` subdir exist on hpc_cluster
   (`mkdir -p` via your ssh session, OR the first
   upload_to_project_path call will create them via its internal
   `mkdir -p`).

The agent's MCP tools piggyback on the ControlMaster the side ssh
opens. BatchMode connections from the agent will fail fast if the
session isn't live — by design.

---

## Test 1 — upload the demo BAM

Pick a small BAM from `data/core_test_data_hg38/` (the manifest's
`select_test_data(file_format="bam")` returns one; the chr22-aligned
one is ideal — a few MB).

```python
upload_to_project_path(
    project_name="phase_b_samtools_demo",
    compute_env_name="hpc_cluster",
    abs_path="/work/users/u/s/user1/CLAUDE_TEST_PROJECTS/"
             "phase_b_samtools_demo/inputs/test.bam",
    local_path="<local path of the demo BAM>",
)
```

**EXPECT:** `success: True`, sha256 round-trip OK. If the parent
`inputs/` dir doesn't exist on hpc_cluster, upload_to_project_path's
internal `mkdir -p` handles it.

---

## Test 2 — freeze samtools + upload the .sif

```python
# Layer 1 — frozen env. Builds the apptainer .sif locally.
freeze(env_name="samtools_view_demo", tools=["samtools"], pipeline_id=...)
# Result has .sif under docker_images/ + an attestation
```

Then push the .sif to common_data:

```python
upload_to_common_data(
    project_name="phase_b_samtools_demo",
    compute_env_name="hpc_cluster",
    local_path="docker_images/samtools_view_demo/samtools_<v>.sif",
    remote_subpath="samtools/samtools_<v>.sif",
)
```

**EXPECT:** `success: True`, `remote_path: "/work/users/u/s/user1/
CLAUDE_GENOMES/samtools/samtools_<v>.sif"`. Note NO project prefix
— common_data is shared by design.

(Larger .sifs eventually need a SLURM data_acquisition job; for the
demo's small samtools sif, a single scp is fine.)

---

## Test 3 — submit_workflow_job

```python
submit_workflow_job(
    project_name="phase_b_samtools_demo",
    compute_env_name="hpc_cluster",
    workflow_dir="/work/users/u/s/user1/CLAUDE_TEST_PROJECTS/"
                 "phase_b_samtools_demo/run_001",
    workflow_name="samtools_view_run_001",
    tool_name="samtools",
    command="samtools view -b -h -F 4 ${input_bam} > ${output_bam}",
    inputs={"input_bam": "/work/users/u/s/user1/CLAUDE_TEST_PROJECTS/"
                         "phase_b_samtools_demo/inputs/test.bam"},
    outputs={"output_bam": "filtered.bam"},
    apptainer_sif="/work/users/u/s/user1/CLAUDE_GENOMES/samtools/"
                  "samtools_<v>.sif",  # from Test 2's remote_path
    apptainer_module="apptainer/1.4.1",
    nextflow_module="nextflow/25.04.7",   # from Phase A Test 2's
                                          # cluster_module_avail
    slurm={"queue": "general", "time": "00:30:00",
           "mem": "4G", "cpus": 2},
)
```

**EXPECT:**
- `success: True`
- `job_id` is a 7-9 digit string
- `files_uploaded` has 3 entries, each in `<workflow_dir>/`
- `workflow_dir: "/work/.../phase_b_samtools_demo/run_001"`
- `submitted_at` ISO UTC

**If sbatch returns a parseable-but-rejected reason** (e.g. partition
doesn't exist on hpc_cluster), the error has `sbatch_stdout` /
`sbatch_stderr` for diagnosis — those are usually one-line fixes to
the `slurm` dict.

**Common gotcha:** if the apptainer/nextflow module versions don't
exist on hpc_cluster (Lmod's drift), the job will fail at `module load`
inside the launcher. cluster_module_avail (Phase A Test 2) returns
the live list — the demo's defaults should match.

---

## Test 4 — poll cluster_job_status until COMPLETED

```python
# Tight loop; sleep(15) between calls; cap at ~20 iterations.
while True:
    status = cluster_job_status(
        project_name="phase_b_samtools_demo",
        compute_env_name="hpc_cluster",
        job_id=<the job_id from Test 3>,
    )
    if status["jobs"] and status["jobs"][0]["state"] in (
            "COMPLETED", "FAILED", "CANCELLED", "TIMEOUT"):
        break
    time.sleep(15)
```

**EXPECT after the loop exits:**
- `status["jobs"][0]["state"] == "COMPLETED"`
- `status["jobs"][0]["exit_code"] == "0:0"`
- `nodelist` is a real node id (e.g. `c-129-3`)
- `start` / `end` are ISO timestamps

**If it sits in PENDING for 30+ seconds:** check the queue is open
+ the resource request is satisfiable. The Lmod state on hpc_cluster
is `general/g0` / similar — `slurm.queue: general` is correct.

---

## Test 5 — download filtered.bam back

```python
download_from_project_path(
    project_name="phase_b_samtools_demo",
    compute_env_name="hpc_cluster",
    abs_path="/work/users/u/s/user1/CLAUDE_TEST_PROJECTS/"
             "phase_b_samtools_demo/run_001/filtered.bam",
    local_path="/tmp/phase_b_filtered.bam",
)
```

**EXPECT:** `success: True`, sha256 round-trip OK. Local file size
> 0 (likely smaller than the input — `samtools view -F 4` filters
unmapped reads).

**If "remote path doesn't exist":** the publishDir in main.nf is
`.` (the workflow_dir), so the filtered.bam SHOULD land at
`<workflow_dir>/filtered.bam`. If it landed in a Nextflow `work/`
subdir instead, the renderer's publishDir directive is wrong —
flag it.

---

## Test 6 — sanity: filtered.bam parses

Local:

```bash
samtools view /tmp/phase_b_filtered.bam | wc -l
```

**EXPECT:** a positive integer. Anything else means the .sif's
samtools wrote a garbage BAM (much less likely than the renderer or
the apptainer-mount being wrong).

---

## Cleanup (optional)

```bash
ssh hpc-agent "rm -rf /work/users/u/s/user1/CLAUDE_TEST_PROJECTS/phase_b_samtools_demo/run_001"
```

The `inputs/test.bam` and the .sif in common_data can stay — the
demo will reuse them on future runs.

---

## After Phase B — what we do next

Paste back EITHER:

(a) "All green" — then we declare Phase 2 minimum DONE. Phase C
    earns its primitives from REAL friction (common_data manifest,
    multi-step pipelines, data_acquisition jobs, Exomiser-class stress).

(b) The specific step that broke + the result dict. I diagnose, fix,
    you re-run just that step, then we proceed.

## Quick reference — Phase 2's 8 bridge tools

| Tool | What it does |
|---|---|
| `snapshot_project(project)` | List directory contents (Phase 1) |
| `cluster_module_avail(project, env, pattern=)` | List `module avail` (Lmod parse) |
| `upload_to_scratch(...)` / `download_from_scratch(...)` | Sandbox transfers, auto-prefixed by project |
| `upload_to_common_data(...)` / `download_from_common_data(...)` | Shared-data transfers, no project prefix |
| `upload_to_project_path(...)` / `download_from_project_path(...)` | Project-workspace transfers, literal abs_path |
| `cluster_job_status(project, env, job_id)` | sacct → state, elapsed, exit_code, nodelist |
| `submit_workflow_job(...)` | Render → upload → sbatch → job_id |
