# HPC Bridge — Phase A test-drive playbook

**Goal:** drive the 7 live bridge tools against your real hpc_cluster
account and surface any wiring bugs BEFORE we build the workflow
submission layer (Phase B).

**Driver:** YOU, from your MCP session. The agent (me) never runs ssh
directly against the cluster — head-node hygiene per
[[feedback-no-cheeky-head-node-testing]]. I sit on the sidelines, you
paste back the result, I fix anything that surfaces.

**Status reference:**
- 7 bridge tools live: `snapshot_project`, `cluster_module_avail`,
  `upload_to_scratch`, `download_from_scratch`,
  `upload_to_common_data`, `download_from_common_data`,
  `upload_to_project_path`, `download_from_project_path`
- `projects_access.yaml` at the repo root, with `hpc_cluster_test` project
  pointing at the `hpc_cluster` env

---

## Pre-flight

Open a separate terminal and:

```bash
ssh hpc-agent
```

Leave it open. That establishes the ControlMaster socket the MCP tools
piggyback on. The agent's BatchMode connections fail fast (no password
prompt) if there's no live session — by design.

Then back in your MCP session, run the tests below in order. Each line
is a single MCP tool call. After each, eyeball the **EXPECT** section
to confirm shape. If any line surprises you, paste me the full result
dict and I'll dig in.

---

## Test 1 — snapshot still works (regression check)

```python
snapshot_project("hpc_cluster_test")
```

**EXPECT:** dict with `entries: [...]` containing your PLANT_PROJECT
files. `entry_count > 0`. `compute_envs: ["hpc_cluster"]`.

**Now there's a new wrinkle:** Step 2.5 extended snapshot to walk
env-target paths under the project namespace. You should ALSO see
entries from `/work/users/u/s/user1/CLAUDE_SCRATCH/hpc_cluster_test/` and
`/work/users/u/s/user1/CLAUDE_GENOMES/hpc_cluster_test/` — if those dirs
exist on disk. If they don't exist yet (no uploads done), snapshot
should silently skip them, not error.

**If it errors:** paste the result and any `errors[]` slice — most
likely a stale snapshot-side bug from the Step 2.5 refactor.

---

## Test 2 — module discovery (full list)

```python
cluster_module_avail("hpc_cluster_test", "hpc_cluster")
```

**EXPECT:** dict with `modules: [list of strings]`, `module_count > 0`,
`pattern: None`. The list should NOT contain any English-word
contamination (`Default`, `Module`, `loaded`, etc.) — the parser stops
at Lmod's `Where:` footer. If you see those leaking in, the parser's
got a real-world quirk to fix.

**Spot-check:** the list should contain entries like
`apptainer/<version>` and `nextflow/<version>`. If those aren't there,
hpc_cluster might not have them as Lmod modules and we'll need a different
discovery mechanism.

---

## Test 3 — module discovery (filtered to nextflow)

```python
cluster_module_avail("hpc_cluster_test", "hpc_cluster", pattern="nextflow")
```

**EXPECT:** `modules: ["nextflow/22.04.5", "nextflow/25.04.7", ...]`,
`pattern: "nextflow"`. Should be a strict subset of Test 2's output.

**This is what Phase B will use** to pick the right `module load
nextflow/X.Y.Z` line for the launcher. So we need it to actually
surface real versions.

---

## Test 4 — module discovery (filtered to apptainer)

```python
cluster_module_avail("hpc_cluster_test", "hpc_cluster", pattern="apptainer")
```

**EXPECT:** at least one `apptainer/<version>` entry. Your earlier
slurm example showed `module load apptainer/1.4.1` — confirm that
version (or newer) shows up.

---

## Test 5 — upload a tiny file to scratch

First create a tiny test file locally:

```bash
echo "phase A test - $(date)" > /tmp/phase_a_test.txt
```

Then in MCP:

```python
upload_to_scratch(
    project_name="hpc_cluster_test",
    compute_env_name="hpc_cluster",
    local_path="/tmp/phase_a_test.txt",
    remote_subpath="phase_a/test_001.txt"
)
```

**EXPECT:**
- `success: True`
- `remote_path` ends with `/CLAUDE_SCRATCH/hpc_cluster_test/phase_a/test_001.txt`
  (note the **auto-prefix by project**: the path includes `hpc_cluster_test/`)
- `sha256: <hex>`, `bytes: <small>`, `duration_s: <float>`,
  `transferred_at: <iso>`

**If you see no auto-prefix:** something's off in scratch's resolver —
flag it.

---

## Test 6 — download the same file back

```python
download_from_scratch(
    project_name="hpc_cluster_test",
    compute_env_name="hpc_cluster",
    remote_subpath="phase_a/test_001.txt",
    local_path="/tmp/phase_a_fetched.txt"
)
```

**EXPECT:**
- `success: True`
- `sha256` matches what Test 5 returned
- `local_path: "/tmp/phase_a_fetched.txt"`

Then locally:

```bash
diff /tmp/phase_a_test.txt /tmp/phase_a_fetched.txt && echo "OK byte-perfect"
```

Should print `OK byte-perfect`. If there's a diff, the round-trip
sha256 check missed something — paste me both files.

---

## Test 7 — confirm overwrite refusal works on the real cluster

Run Test 5 AGAIN with the exact same `remote_subpath`:

```python
upload_to_scratch(
    project_name="hpc_cluster_test",
    compute_env_name="hpc_cluster",
    local_path="/tmp/phase_a_test.txt",
    remote_subpath="phase_a/test_001.txt"  # SAME as Test 5
)
```

**EXPECT:**
- `error: "remote path already exists: ... refuses overwrites. Pick a fresh remote_subpath..."`

This is the cluster-side `test -e $path && echo EXISTS || echo OK`
check firing. If it silently succeeds (treats it as an upload), the
overwrite-refusal stanza isn't reaching the ssh path. Critical to
catch.

---

## Test 8 — upload to common_data (versioned subpath)

```python
upload_to_common_data(
    project_name="hpc_cluster_test",
    compute_env_name="hpc_cluster",
    local_path="/tmp/phase_a_test.txt",
    remote_subpath="phase_a/v1/test_common.txt"
)
```

**EXPECT:**
- `success: True`
- `remote_path` ends with `/CLAUDE_GENOMES/phase_a/v1/test_common.txt`
  (**NO project prefix** — common_data is shared by design)

If `hpc_cluster_test/` shows up in the path, common_data's resolver is
incorrectly auto-prefixing — flag it.

---

## Test 9 — download from common_data

```python
download_from_common_data(
    project_name="hpc_cluster_test",
    compute_env_name="hpc_cluster",
    remote_subpath="phase_a/v1/test_common.txt",
    local_path="/tmp/phase_a_common_fetched.txt"
)
```

**EXPECT:** `success: True`, sha256 matches.

---

## Test 10 (OPTIONAL — requires yaml edit) — project workspace

This one needs you to add `upload` + `download` to your
`projects_access.yaml`'s PLANT_PROJECT entry, OR pick a subdir like
`PLANT_PROJECT/agent_outputs/` that you're OK with the agent writing to.

If you want to skip it for Phase A, that's fine — it's not on the
critical path for Phase B. But it's worth proving the third auth chain
works against real hpc_cluster at some point.

If you add:

```yaml
- name: hpc_cluster_test
  ...
  compute_env_access:
    - compute_env: hpc_cluster
      directories:
        - path: /work/users/u/s/user1/PLANT_PROJECT/agent_outputs
          permissions: [file_name_only, upload, download]
          description: "agent-writable subdir of PLANT_PROJECT"
```

(Make sure the dir exists on the cluster — `mkdir -p .../agent_outputs/`
from your ssh session.) Then:

```python
upload_to_project_path(
    project_name="hpc_cluster_test",
    compute_env_name="hpc_cluster",
    abs_path="/work/users/u/s/user1/PLANT_PROJECT/agent_outputs/test.txt",
    local_path="/tmp/phase_a_test.txt"
)
```

**EXPECT:** `success: True`, `remote_path` is the literal abs_path (no
auto-prefix).

---

## After Phase A — what we do next

Paste back EITHER:

(a) "All green" — then I roll Phase B (cluster_job_status + minimum
    submit_workflow_job for `samtools view on a small BAM`). Expect
    ~600 LOC + a doc updates.

(b) The specific test that broke + the result dict. I diagnose, fix,
    you re-run just that test, then we proceed.

The Phase B playbook will be a sibling doc in `docs/`.

## Quick reference — the 7 tools at a glance

| Tool | Auth chain | What it does |
|---|---|---|
| `snapshot_project(project)` | Phase-1 directories[] + env-target sweep | List directory contents |
| `cluster_module_avail(project, env, pattern=)` | Project on env | List `module avail` |
| `upload_to_scratch(project, env, local, subpath)` | env-implicit + project prefix | Push to sandbox |
| `download_from_scratch(project, env, subpath, local)` | env-implicit + project prefix | Pull from sandbox |
| `upload_to_common_data(project, env, local, subpath)` | env-implicit shared | Push to ref-data zone |
| `download_from_common_data(project, env, subpath, local)` | env-implicit shared | Pull from ref-data zone |
| `upload_to_project_path(project, env, abs_path, local)` | Phase-1 directories[] explicit | Push to project workspace |
| `download_from_project_path(project, env, abs_path, local)` | Phase-1 directories[] explicit | Pull from project workspace |

All uploads refuse to overwrite. All transfers verify sha256
round-trip. All ssh runs through your `ssh hpc-agent` ControlMaster
session — close that terminal cleanly to end everything.
