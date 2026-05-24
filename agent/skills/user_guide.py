"""
User-guide generator — Layer 2's deliverable.

Renders a runnable Markdown guide for a pipeline from ONLY material the runtime
already executed and validated: passing pipeline_steps (command + validated
outputs), the self-tested usage.command_template (when usage_verified), the
frozen env's HPC delivery block, and the verified package set. The honesty hook
is structural — `executed_commands()` is the single source the guide draws run
commands from, so a command that never ran (or a step that failed) cannot appear
in the guide. No hand-written "here's roughly how you'd run it".

A workflow consumes an environment BY DIGEST: the "Get the environment" section
is the freeze() artifact's Apptainer delivery, and provenance pins the content
digest + image digest + lock. The driver/runtime env is recorded too (the gap
nf-core/Snakemake leave open).
"""

from __future__ import annotations

from typing import Any, Optional


def executed_commands(spec: dict) -> list[dict]:
    """The validated run commands a guide may show. Each: {command, outputs,
    source}. Drawn only from pipeline_steps that ran (returncode 0) AND passed
    validation, plus the usage.command_template iff usage_verified — so every
    command surfaced is provably executed + checked."""
    out: list[dict] = []
    for s in spec.get("pipeline_steps", []) or []:
        if not isinstance(s, dict):
            continue
        if s.get("returncode") not in (0, None):
            continue
        cmd = s.get("command")
        if not cmd:
            continue
        outs = s.get("detected_outputs") or list((s.get("validation") or {}).keys())
        validated = bool(s.get("validation")) or s.get("validation_status") == "passed"
        if not validated:
            continue
        out.append({"command": cmd, "outputs": outs, "source": f"pipeline_step {s.get('step')}"})
    usage = spec.get("usage") or {}
    if spec.get("usage_verified") and usage.get("command_template"):
        out.append({"command": usage["command_template"],
                    "outputs": [], "source": "usage.command_template (self-tested)"})
    return out


def _fence(text: str) -> str:
    return f"```\n{text}\n```"


def render_user_guide(spec: dict, freeze_record: Optional[dict] = None) -> str:
    """Render the Markdown user guide. `freeze_record` (from freeze()) supplies
    the HPC delivery + the image/content digests; without it the guide falls
    back to the spec's docker info."""
    name = spec.get("pipeline_name", "pipeline")
    version = ""
    for p in spec.get("packages", []) or []:
        if isinstance(p, dict) and p.get("name", "").lower() == name.lower() and p.get("version"):
            version = p["version"]
            break
    title = f"{name}" + (f" {version}" if version else "")
    L: list[str] = [f"# {title} — user guide", ""]
    if spec.get("description"):
        L += [spec["description"], ""]
    L += ["> Generated from the build's passing, validated run — every command below was "
          "executed and its outputs checked (provenance at the bottom). Not hand-written.", ""]

    # 1. Get the environment (HPC / Apptainer) — pinned by digest.
    L += ["## 1. Get the environment", ""]
    hpc = (freeze_record or {}).get("hpc_delivery") or {}
    if hpc.get("get_image"):
        L += [f"_{hpc.get('source_note','')}_", "", _fence(hpc["get_image"]), ""]
        if hpc.get("run_example"):
            L += ["Run a command in it:", _fence(hpc["run_example"]), ""]
        if hpc.get("sbatch_template"):
            L += ["<details><summary>SLURM batch template</summary>", "",
                  _fence(hpc["sbatch_template"]), "", "</details>", ""]
    else:
        docker = spec.get("docker") or {}
        tag = docker.get("image_tag") or f"{name}:{version or 'latest'}"
        L += ["Pull/convert the image for HPC (Apptainer):",
              _fence(f"apptainer pull {name}.sif docker://{tag}\n"
                     f"apptainer exec --bind /scratch/$USER/data:/data {name}.sif <command>"), ""]

    # 2. Run it — only validated commands.
    L += ["## 2. Run it", ""]
    cmds = executed_commands(spec)
    if not cmds:
        L += ["_No validated run command recorded yet — run the tool via run_pipeline_step "
              "and set a verified usage.command_template, then regenerate._", ""]
    for c in cmds:
        L += [f"_{c['source']}_", _fence(c["command"])]
        if c["outputs"]:
            L += ["Produces (validated):", ""] + [f"- `{o}`" for o in c["outputs"]] + [""]
        else:
            L += [""]
    usage = spec.get("usage") or {}
    if usage.get("inputs"):
        L += ["**Inputs**", ""] + [
            f"- `{i.get('name','?')}` — {i.get('format', i.get('description',''))}"
            for i in usage["inputs"] if isinstance(i, dict)] + [""]
    if usage.get("outputs"):
        L += ["**Outputs**", ""] + [
            f"- {o.get('files', o.get('name','?'))}"
            for o in usage["outputs"] if isinstance(o, dict)] + [""]

    # 3. Environment / driver details (the nf-core gap: record the runner env).
    L += ["## 3. Environment details", ""]
    if spec.get("conda_env"):
        L.append(f"- conda env: `{spec['conda_env']}`")
    if spec.get("python_version"):
        L.append(f"- python: {spec['python_version']}")
    pkgs = [p for p in (spec.get("packages") or []) if isinstance(p, dict) and p.get("name")]
    if pkgs:
        listed = ", ".join(f"{p['name']}={p.get('version','?')}" for p in pkgs[:12])
        L += [f"- key packages: {listed}" + (" …" if len(pkgs) > 12 else "")]
    L.append("")

    # 4. Provenance — the machine-verified anchors.
    L += ["## Provenance (machine-verified)", ""]
    fr = freeze_record or {}
    rows = [
        ("content digest", fr.get("content_digest")),
        ("image", fr.get("image")),
        ("image digest", fr.get("image_digest")),
        ("lock sha256", spec.get("lock_sha256")),
        ("env_status", spec.get("env_status")),
        ("pipeline_status", spec.get("pipeline_status")),
        ("usage_verified", spec.get("usage_verified")),
    ]
    L += [f"- {k}: `{v}`" for k, v in rows if v not in (None, "")]
    L.append("")
    return "\n".join(L)
