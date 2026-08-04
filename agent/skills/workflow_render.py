"""
workflow_render — render a one-process Nextflow workflow (main.nf +
nextflow.config + launcher.sh) for a single-tool demo on HPC.

This is the agent's per-project renderer per
[[project-nextflow-module-principles]] and
[[project-per-project-pipelines]]:

  1. The module is generic + parameterized: inputs/outputs flow through
     `params{}` at the top of main.nf so a human reader can see EVERY
     path the pipeline touches in one place. No bespoke wiring.
  2. The process's `script:` block is the LITERAL shell command — no
     DSL magic. Acceptance test: a human reads main.nf, copies the
     command, substitutes the params, and re-runs the step from a
     terminal. If they can't, the renderer is wrong.
  3. The tool comes from a frozen apptainer .sif (staged to the env's
     container_upload_target). The process runs `apptainer exec <sif> <cmd>`. The
     .sif path is a top-level param so swapping versions is a one-line
     change to main.nf.

The renderer is pure: strings in, strings out. Filesystem writes,
ssh, and sbatch live in `submit_workflow_job` (Step 2 commit #2).

Input shape — `WorkflowSpec` namedtuple-ish dict
-------------------------------------------------

  tool_name       short label, e.g. "samtools". Used in process name +
                  sbatch --job-name. Safe-token (alnum + `_-`).
  command         literal shell command with `${param}` placeholders.
                  e.g. `samtools view -b -h -F 4 ${input_bam} > ${output_bam}`.
                  Placeholders MUST match keys in `inputs` ∪ `outputs`, and
                  every `$` in the command must open one of them. Single
                  quotes, backslashes and a triple-double-quote are REFUSED —
                  main.nf would rewrite them in transit and run something else
                  on the cluster; see `_check_command_renders_faithfully` for
                  the measurement and the workaround.
  inputs          {placeholder_name: remote_abs_path}. The remote path
                  is what the running process will see — the renderer
                  does NOT compute container paths; main.nf uses the
                  remote path directly (apptainer's default bind makes
                  the host's $PWD visible inside the container).
  outputs         {placeholder_name: remote_filename}. Outputs are
                  always written to the working dir (Nextflow's
                  per-process workDir); the params carry just the
                  filename, not an absolute path.
  apptainer_sif   absolute remote path to the .sif image.
  apptainer_module  Lmod module string, e.g. "apptainer/1.4.1".
  nextflow_module   Lmod module string, e.g. "nextflow/25.04.7".
  slurm           {queue, time, mem, cpus, account?}. account is
                  optional; everything else required.
  workflow_name   safe-token, ≤64 chars. Used for sbatch --job-name
                  AND as the directory name on the remote side.
"""
from __future__ import annotations

import re
import shlex
from typing import Mapping, Optional


# Bare-bones safe-token check — alphanumeric, underscores, dashes,
# dots. No path separators, no shell metacharacters. Used for any
# value that ends up in a shell line via simple string interpolation.
_SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.\-]+$")
# A nextflow param key — same as a Python identifier (snake_case).
_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# A Lmod module string — alnum + `_+./-` (e.g. "apptainer/1.4.1",
# "GNU-parallel/20231022", "nextflow/25.04.7").
_MODULE_TOKEN_RE = re.compile(r"^[A-Za-z0-9_+./-]+$")
# Absolute POSIX path — leading slash, no `..` segments.
_ABS_POSIX_RE = re.compile(r"^/[^\0]*$")


def _check_safe_token(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if not _SAFE_TOKEN_RE.match(value):
        raise ValueError(
            f"{name}={value!r} must be alnum + `_.-` (no path separators "
            f"or shell metacharacters)")


def _check_abs_path(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if not _ABS_POSIX_RE.match(value):
        raise ValueError(f"{name}={value!r} must be an absolute POSIX path")
    if ".." in value.split("/"):
        raise ValueError(f"{name}={value!r} must not contain `..` segments")


def _check_module(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    if not _MODULE_TOKEN_RE.match(value):
        raise ValueError(
            f"{name}={value!r} must be alnum + `_+./-` (Lmod module token)")


def _check_param_name(label: str, value: str) -> None:
    if not _PARAM_NAME_RE.match(value):
        raise ValueError(
            f"{label} key {value!r} must be a Python identifier "
            f"(snake_case)")


def _scan_placeholders(command: str) -> set[str]:
    """Find every `${name}` placeholder in `command`. Returns the set
    of names — the renderer cross-checks against `inputs ∪ outputs`."""
    return set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", command))


def _check_command_renders_faithfully(command: str, declared: set[str]) -> None:
    """Refuse a command whose rendered form would not BE the command that
    was authored.

    main.nf carries the command through two layers that both rewrite text: a
    Groovy TRIPLE-DOUBLE-QUOTED string (the `script:` block, which does escape
    processing and `$` interpolation) and then a `bash -c '…'` argument. Each
    character below survives that trip only by changing meaning, and it changes
    silently — the workflow renders, uploads and sbatches looking perfectly
    well-formed, and the compute node runs something else. Measured, all four,
    before this check existed:

      `'`    closes the `bash -c '…'` argument early. `awk '{print $1}'` renders
             as `bash -c '… | awk '{print $1}' > …'` — the middle escapes the
             quoting and is re-parsed by the outer shell.
      `\\`    Groovy processes escapes inside `\"\"\"…\"\"\"`, so `\\n` reaches the
             cluster as a REAL NEWLINE that splits the command in half and `\\t`
             as a tab. The worst of the four: it can produce a plausible wrong
             answer rather than an error.
      `$x`   Groovy interpolates it. `$PWD` raises MissingPropertyException at
             run time; `$1` (every awk/sed field reference) is a PARSE error in
             main.nf. Neither is visible until the job has already been queued.
      `\"\"\"`  terminates the script block outright.

    This is a refusal, not an escaping TODO. Escaping would mean layering Groovy
    escapes under shell quoting inside main.nf, and the resulting line is no
    longer something a human can read, copy and re-run from a terminal — which
    is the ONLY reason this renderer exists rather than a Nextflow module library
    ([[project-nextflow-module-principles]]). A command that genuinely needs
    quoting or shell variables belongs in a script baked into the image and
    invoked here by name; that also makes it part of the frozen, validated
    artifact instead of a string typed at submit time.

    Deliberate locus asymmetry: `run_production._render_local_command` ACCEPTS
    all four, because a local run shell-quotes into `run.sh` with no Groovy layer
    in between and the command really does arrive intact. Do not "fix" the
    inconsistency by loosening this side — the two loci differ in what they can
    faithfully carry, and the honest move is for each to refuse what it would
    otherwise corrupt.
    """
    if '"""' in command:
        raise ValueError(
            'command contains `\"\"\"`, which terminates main.nf\'s script block. '
            'Move it into a script baked into the image.')

    if "'" in command:
        raise ValueError(
            "command contains a single quote. main.nf runs the command as "
            "`bash -c '<command>'`, so the quote closes that argument early and "
            "the remainder is re-parsed by the outer shell — the cluster would "
            "run a DIFFERENT command than the one written here. Use double "
            "quotes, or move the quoted fragment (awk/sed programs especially) "
            "into a script baked into the image and call it by name.")

    if "\\" in command:
        raise ValueError(
            "command contains a backslash. The Groovy script block in main.nf "
            "processes escape sequences, so `\\n` arrives at the cluster as a "
            "real newline (splitting the command in two) and `\\t` as a tab. "
            "Move anything needing escapes into a script baked into the image.")

    # Every `$` must open a placeholder this command DECLARED. Anything else is
    # a Groovy interpolation we did not author — checked over the raw command,
    # since the `${k}` → `${params.k}` rewrite below happens only for declared
    # keys and would otherwise hide the stray ones.
    for m in re.finditer(r"\$", command):
        head = re.match(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", command[m.start():])
        if head and head.group(1) in declared:
            continue
        snippet = command[m.start():m.start() + 12]
        raise ValueError(
            f"command has a `$` at offset {m.start()} ({snippet!r}) that does "
            f"not open a declared placeholder (declared: {sorted(declared)}). "
            f"main.nf embeds the command in a Groovy interpolated string, so a "
            f"bare `$NAME` is resolved by Nextflow (MissingPropertyException) "
            f"and `$1` fails to parse the file at all. Declare it as an input, "
            f"or move the shell variable into a script baked into the image.")


# The per-job resource request — a CONTROLLED vocabulary: the same key names and
# the same value formats every job, so a header is reproducible and reviewable.
#   time (req)     HH:MM:SS | D-HH:MM:SS | D-   → --time
#   mem  (req)     6G | 45g | 12000M            → --mem
#   cpus (def 1)   int 1..256                   → --cpus-per-task
#   ntasks (def 1) int 1..256                   → --ntasks
#   gpus (def 0)   int 0..16                    → --gres=gpu:N (+ partition/qos)
#   partition (opt) safe token                  → --partition (OMITTED ⇒ scheduler
#                                                  default, e.g. many HPC CPU jobs)
#   qos (opt)       safe token                  → --qos (GPU access on many HPCs)
#   account (opt)   safe token                  → --account (OMITTED when unset)
# partition/qos/account are normally MERGED IN from the env's slurm policy by the
# caller (bridge_tools) — the agent's per-job dict carries time/mem/cpus/ntasks/gpus.
_SLURM_REQUIRED = ("time", "mem")
_SLURM_OPTIONAL = ("cpus", "ntasks", "gpus", "partition", "qos", "account")
_SLURM_ALL = _SLURM_REQUIRED + _SLURM_OPTIONAL

_TIME_RE = re.compile(r"^(\d+-)?\d{1,2}:\d{2}:\d{2}$|^\d+-$")
_MEM_RE = re.compile(r"^\d+[KMGTkmgt]$")
# A partition value: one or more comma-separated safe tokens (some sites permit
# targeting several GPU partitions, e.g. "a100-gpu,l40-gpu").
_PARTITION_RE = re.compile(r"^[A-Za-z0-9_.\-]+(,[A-Za-z0-9_.\-]+)*$")


def _check_slurm(slurm: Mapping) -> dict:
    """Closed-key check + value typing for the per-job resource request. Typos
    raise instead of silently being ignored. Defaults are applied for the
    convenience keys (cpus/ntasks=1, gpus=0) so the header is consistent even
    from a minimal `{time, mem}` request."""
    if not isinstance(slurm, Mapping):
        raise ValueError(f"slurm must be a mapping, got {type(slurm).__name__}")
    unknown = set(slurm.keys()) - set(_SLURM_ALL)
    if unknown:
        raise ValueError(
            f"slurm has unknown keys {sorted(unknown)} (allowed: {list(_SLURM_ALL)})")
    missing = set(_SLURM_REQUIRED) - set(slurm.keys())
    if missing:
        raise ValueError(
            f"slurm missing required keys {sorted(missing)} (required: {list(_SLURM_REQUIRED)})")
    out: dict = {}
    # time: HH:MM:SS | D-HH:MM:SS | D-
    t = slurm["time"]
    if not isinstance(t, str) or not _TIME_RE.match(t):
        raise ValueError(f"slurm.time={t!r} must look like HH:MM:SS, D-HH:MM:SS, or D-")
    out["time"] = t
    # mem: like "6G" / "45g" / "12000M"
    m = slurm["mem"]
    if not isinstance(m, str) or not _MEM_RE.match(m):
        raise ValueError(f"slurm.mem={m!r} must look like 6G / 45g / 12000M")
    out["mem"] = m
    # cpus / ntasks: bounded positive ints with defaults
    for key, default, hi in (("cpus", 1, 256), ("ntasks", 1, 256)):
        v = slurm.get(key, default)
        if not isinstance(v, int) or isinstance(v, bool) or v < 1 or v > hi:
            raise ValueError(f"slurm.{key}={v!r} must be an int in [1, {hi}]")
        out[key] = v
    # gpus: bounded non-negative int, default 0 (0 ⇒ a CPU job)
    g = slurm.get("gpus", 0)
    if not isinstance(g, int) or isinstance(g, bool) or g < 0 or g > 16:
        raise ValueError(f"slurm.gpus={g!r} must be an int in [0, 16]")
    out["gpus"] = g
    # partition: optional; may be comma-separated (e.g. "a100-gpu,l40-gpu" —
    # some sites let a GPU job target several partitions). Omitted ⇒ no line.
    if slurm.get("partition"):
        p = slurm["partition"]
        if not isinstance(p, str) or not _PARTITION_RE.match(p):
            raise ValueError(
                f"slurm.partition={p!r} must be one or more comma-separated safe "
                f"tokens (alnum + `_.-`)")
        out["partition"] = p
    # qos / account: optional single safe tokens (omitted lines when unset)
    for key in ("qos", "account"):
        if slurm.get(key):
            _check_safe_token(f"slurm.{key}", slurm[key])
            out[key] = slurm[key]
    # GPU coherence: a GPU job MUST carry a partition + qos (the HPC's GPU
    # convention, merged in from the env slurm.gpu block by the caller). Without
    # them the job would land on a CPU partition and never see a GPU.
    if out["gpus"] > 0 and not (out.get("partition") and out.get("qos")):
        raise ValueError(
            f"slurm.gpus={out['gpus']} requires both a partition and a qos (the HPC's "
            f"GPU convention from the env slurm.gpu block) — resolved partition="
            f"{out.get('partition')!r}, qos={out.get('qos')!r}")
    return out


_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")


def _check_email(email: str) -> str:
    """A plausible email, safe to drop into `#SBATCH --mail-user=`. Empty ⇒ no
    mail lines. Rejects anything with shell metacharacters or whitespace."""
    if not email:
        return ""
    if not isinstance(email, str) or not _EMAIL_RE.match(email):
        raise ValueError(f"email={email!r} is not a valid email address")
    return email


def _render_sbatch_header(workflow_name: str, slurm_v: dict, email: str) -> str:
    """The #SBATCH block — one controlled convention, using SLURM's own filename
    directives (%x=job-name, %j=job-id) so logs self-name with the real job ID and
    never collide. Optional lines (partition/qos/gres/account/mail) are emitted
    ONLY when their value is present, so a typical CPU job renders no --partition
    and no --account (matches slurm_header_template.txt)."""
    def line(cond: object, text: str) -> str:
        return f"{text}\n" if cond else ""
    sv = slurm_v
    return (
        f"#SBATCH --job-name={workflow_name}\n"
        f"#SBATCH --time={sv['time']}\n"
        f"#SBATCH --mem={sv['mem']}\n"
        f"#SBATCH --nodes=1\n"
        f"#SBATCH --ntasks={sv['ntasks']}\n"
        f"#SBATCH --cpus-per-task={sv['cpus']}\n"
        + line(sv.get("partition"), f"#SBATCH --partition={sv.get('partition')}")
        + line(sv["gpus"] > 0, f"#SBATCH --qos={sv.get('qos')}")
        + line(sv["gpus"] > 0, f"#SBATCH --gres=gpu:{sv['gpus']}")
        + line(sv.get("account"), f"#SBATCH --account={sv.get('account')}")
        + f"#SBATCH --output=%x-%j.out\n"
        + f"#SBATCH --error=%x-%j.err\n"
        + line(email, "#SBATCH --mail-type=END")
        + line(email, f"#SBATCH --mail-user={email}")
    )


def render_workflow(*,
                    tool_name: str,
                    command: str,
                    inputs: Mapping[str, str],
                    outputs: Mapping[str, str],
                    apptainer_sif: str,
                    apptainer_module: str,
                    nextflow_module: str,
                    slurm: Mapping,
                    workflow_name: str,
                    email: str = "",
                    nextflow_main_filename: str = "main.nf",
                    nextflow_config_filename: str = "nextflow.config",
                    ) -> dict:
    """Render the three workflow files as strings.

    Returns:
      {
        "main.nf":         <str>,
        "nextflow.config": <str>,
        "launcher.sh":     <str>,
        "process_name":    <str>,   # derived from tool_name; sanity check
        "param_keys":      [<str>],  # the full param set the launcher passes
      }
    Raises ValueError on any validation failure — the renderer is the
    last line of defense before the strings hit the cluster, so it's
    strict about every input.
    """
    # ─── Validate every input ────────────────────────────────────────────
    _check_safe_token("tool_name", tool_name)
    _check_safe_token("workflow_name", workflow_name)
    if len(workflow_name) > 64:
        raise ValueError(f"workflow_name length {len(workflow_name)} > 64")
    _check_abs_path("apptainer_sif", apptainer_sif)
    _check_module("apptainer_module", apptainer_module)
    _check_module("nextflow_module", nextflow_module)
    if not isinstance(command, str) or not command.strip():
        raise ValueError("command must be a non-empty string")
    if "\n" in command:
        raise ValueError("command must be a single line (no newlines)")

    if not isinstance(inputs, Mapping) or not isinstance(outputs, Mapping):
        raise ValueError("inputs and outputs must both be mappings")
    for k in inputs:
        _check_param_name("inputs", k)
    for k in outputs:
        _check_param_name("outputs", k)
    for k, v in inputs.items():
        _check_abs_path(f"inputs[{k!r}]", v)
    for k, v in outputs.items():
        # Outputs are filenames, not abs paths. Same safe-token rule
        # plus we allow `.` for extensions.
        if not isinstance(v, str) or not v:
            raise ValueError(f"outputs[{k!r}] must be a non-empty string")
        if "/" in v or ".." in v:
            raise ValueError(
                f"outputs[{k!r}]={v!r} must be a bare filename "
                f"(no path separators or `..`)")
        if not _SAFE_TOKEN_RE.match(v):
            raise ValueError(
                f"outputs[{k!r}]={v!r} must be alnum + `_.-`")

    declared = set(inputs.keys()) | set(outputs.keys())
    referenced = _scan_placeholders(command)
    undeclared = referenced - declared
    unreferenced = declared - referenced
    if undeclared:
        raise ValueError(
            f"command references undeclared placeholders {sorted(undeclared)} "
            f"(declared: {sorted(declared)})")
    if unreferenced:
        raise ValueError(
            f"declared placeholders {sorted(unreferenced)} are not "
            f"referenced in the command")
    # Reject overlap: a placeholder can't be in both inputs and outputs.
    overlap = set(inputs.keys()) & set(outputs.keys())
    if overlap:
        raise ValueError(
            f"placeholders {sorted(overlap)} appear in BOTH inputs and outputs")
    # LAST, so the more basic "you didn't declare it" errors above fire first
    # on a command that trips both.
    _check_command_renders_faithfully(command, declared)

    slurm_v = _check_slurm(slurm)
    _check_safe_token("nextflow_main_filename", nextflow_main_filename)
    _check_safe_token("nextflow_config_filename", nextflow_config_filename)

    # ─── Render main.nf ──────────────────────────────────────────────────
    process_name = f"run_{tool_name}"

    # The shell command Nextflow will execute. Apptainer is invoked
    # explicitly so the .sif path can change per-run without rebuilding
    # the workflow; the bind mounts of $PWD + the input dirs come for
    # free from apptainer's defaults.
    bind_dirs = sorted({
        # The parent dir of each input file — apptainer auto-binds $PWD,
        # but inputs live elsewhere on the host so we bind their parents
        # explicitly to make them readable inside the container.
        "/".join(p.split("/")[:-1]) or "/"
        for p in inputs.values()
    })
    bind_flags = " ".join(
        f"--bind {shlex.quote(d)}" for d in bind_dirs)

    # Inside the script block, substitute ${param} → ${params.param}
    # so Nextflow's process scope resolves them.
    script_body = command
    for k in declared:
        script_body = script_body.replace(
            "${" + k + "}", "${params." + k + "}")

    # params declarations — dot notation per Nextflow DSL2. Groovy
    # parses `params { ... }` as a method call (Nextflow rejects it
    # with "Unknown method invocation `params`"). One-line-per-param
    # avoids that and is universally supported.
    param_lines = []
    for k, v in inputs.items():
        param_lines.append(f"params.{k} = {_nf_quote(v)}")
    for k, v in outputs.items():
        param_lines.append(f"params.{k} = {_nf_quote(v)}")
    param_lines.append(f"params.apptainer_sif = {_nf_quote(apptainer_sif)}")
    param_block = "\n".join(param_lines)

    # Output channel: emit every output file so Nextflow tracks them.
    output_lines = "\n".join(
        f"    path \"${{params.{k}}}\""
        for k in outputs.keys())

    main_nf = (
        f"// Generated by workflow_render — DO NOT hand-edit.\n"
        f"// Tool: {tool_name}. Workflow: {workflow_name}.\n"
        f"\n"
        f"nextflow.enable.dsl = 2\n"
        f"\n"
        f"{param_block}\n"
        f"\n"
        f"process {process_name} {{\n"
        f"    publishDir \".\", mode: 'copy'\n"
        f"\n"
        f"    output:\n"
        f"{output_lines}\n"
        f"\n"
        f"    script:\n"
        f"    \"\"\"\n"
        # --cleanenv: run with the IMAGE's baked environment, NOT the host's.
        # Apptainer passes host env vars into the container by default, and they
        # OVERRIDE the image's ENV — so a cluster-set JAVA_HOME (the cluster ships
        # /nas/.../java/17) clobbers our baked JAVA_HOME and the JVM/Spark gateway
        # dies "java: No such file or directory". --cleanenv makes validated ==
        # shipped hold at the env level: the container runs the environment we
        # sealed, immune to whatever the login node happens to export.
        f"    apptainer exec --cleanenv {bind_flags} ${{params.apptainer_sif}} "
        f"bash -c '{script_body}'\n"
        f"    \"\"\"\n"
        f"}}\n"
        f"\n"
        f"workflow {{\n"
        f"    {process_name}()\n"
        f"}}\n"
    )

    # ─── Render nextflow.config ──────────────────────────────────────────
    # Minimal: enable apptainer, point at the .sif, set the work dir to
    # a per-run subdir so re-runs don't collide. The user can override
    # any of this via -c on the nextflow CLI; we ship the floor.
    nextflow_config = (
        f"// Generated by workflow_render — DO NOT hand-edit.\n"
        f"apptainer {{\n"
        f"    enabled = true\n"
        f"    autoMounts = true\n"
        f"}}\n"
        f"\n"
        f"process {{\n"
        f"    cpus = {slurm_v['cpus']}\n"
        # Nextflow MemoryUnit wants an uppercase unit + 'B' (e.g. 6GB, 20GB). The
        # SBATCH --mem accepts either case (writes lowercase '20g'); normalize
        # here so a lowercase request doesn't render an odd '20gB'.
        f"    memory = '{slurm_v['mem'].upper()}B'\n"
        f"    time = '{slurm_v['time']}'\n"
        f"}}\n"
    )

    # ─── Render launcher.sh (the sbatch wrapper) ─────────────────────────
    # The launcher is what `sbatch launcher.sh` consumes on the head
    # node. SBATCH directives must be the first non-comment lines. The header
    # follows ONE controlled convention (see _render_sbatch_header) and uses
    # SLURM's %x/%j filename directives so logs self-name with the job ID.
    email_v = _check_email(email)
    launcher_sh = (
        f"#!/usr/bin/env bash\n"
        + _render_sbatch_header(workflow_name, slurm_v, email_v)
        + f"\n"
        f"set -euo pipefail\n"
        f"\n"
        f"# Generated by workflow_render — DO NOT hand-edit.\n"
        f"module purge\n"
        f"module load {apptainer_module}\n"
        f"module load {nextflow_module}\n"
        f"\n"
        f"# cd into the workflow dir. SLURM stages the script into\n"
        f"# /var/spool/slurmd/job<id>/, so $0 / readlink -f point at\n"
        f"# THAT dir (not writable). $SLURM_SUBMIT_DIR is set by SLURM\n"
        f"# to the dir where `sbatch launcher.sh` was invoked — that's\n"
        f"# the workflow_dir we uploaded everything into. Fall back to\n"
        f"# $0's dir only for non-SLURM invocations (sanity).\n"
        f"cd \"${{SLURM_SUBMIT_DIR:-$(dirname \"$(readlink -f \"$0\")\")}}\"\n"
        f"\n"
        f"# Redirect Nextflow's per-user cache + per-run state INTO the\n"
        f"# workflow dir. On HPC, $HOME often isn't writable from compute\n"
        f"# nodes (quota, NFS, allocation boundaries), so nextflow's\n"
        f"# bootstrap of ~/.nextflow can fail silently and the run dies\n"
        f"# with \".nextflow/history.lock: No such file or directory\".\n"
        f"# Pinning NXF_HOME / NXF_WORK to $PWD keeps everything inside\n"
        f"# the project workspace where `exec` perms already apply.\n"
        f"export NXF_HOME=\"$PWD/.nextflow_home\"\n"
        f"export NXF_WORK=\"$PWD/work\"\n"
        f"mkdir -p \"$NXF_HOME\" \"$NXF_WORK\" .nextflow\n"
        f"\n"
        f"nextflow run {shlex.quote(nextflow_main_filename)} "
        f"-c {shlex.quote(nextflow_config_filename)} "
        f"-with-report {shlex.quote(workflow_name + '_report.html')} "
        f"-with-trace {shlex.quote(workflow_name + '_trace.txt')}\n"
    )

    return {
        "main.nf":         main_nf,
        "nextflow.config": nextflow_config,
        "launcher.sh":     launcher_sh,
        "process_name":    process_name,
        "param_keys":      sorted(declared) + ["apptainer_sif"],
    }


def _nf_quote(s: str) -> str:
    """Quote a string for safe interpolation inside a Nextflow `params`
    block. Nextflow's Groovy parser accepts single-quoted strings as
    literal — no escape processing — so any string that doesn't contain
    a single quote is safe. The validators upstream already refuse
    every dangerous character, so this is the last-mile assert."""
    if "'" in s or "\n" in s:
        raise ValueError(
            f"value {s!r} contains characters that cannot be safely "
            f"quoted into a Nextflow params block (single quote or newline)")
    return f"'{s}'"
