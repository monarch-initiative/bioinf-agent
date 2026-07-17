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

import re
from typing import Any, Optional

from agent.models.core_data import shipped_binaries as _shipped_binaries, usage_commands


def _version_of(pkg: dict) -> str:
    """Best-known version of a package record, with fallbacks for tiers that
    don't carry a plain `version` (a release binary records its version only in
    the release tag of binary_url; conda/pip carry it in the spec string)."""
    v = pkg.get("version")
    if v:
        return str(v)
    im = pkg.get("install_method") if isinstance(pkg.get("install_method"), dict) else {}
    url = im.get("binary_url") or im.get("source") or ""
    m = re.search(r"/releases/download/([^/]+)/", url)   # .../download/v2.13.0/asset
    if m:
        return m.group(1).lstrip("vV")
    for key in ("conda_spec", "pip_spec"):
        mm = re.search(r"[=@]{1,2}([0-9][\w.\-]*)", im.get(key) or "")
        if mm:
            return mm.group(1)
    return "?"


def key_packages(spec: dict) -> dict[str, str]:
    """name → version for the tools in this env, from a draft OR finalized spec.
    Drafts only have install_steps[].installed_packages; finalized specs have a
    derived packages[] (authoritative). Version uses _version_of's fallbacks so a
    release-binary tool shows its real version, not '?'."""
    out: dict[str, str] = {}
    for st in spec.get("install_steps", []) or []:
        for ip in (st.get("installed_packages") or []):
            if isinstance(ip, dict) and ip.get("name") and ip["name"] not in out:
                out[ip["name"]] = _version_of(ip)
    for p in spec.get("packages", []) or []:
        if isinstance(p, dict) and p.get("name"):
            out[p["name"]] = _version_of(p)
    return out


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
    # ONE reading of command_template (str or list[str]) — core_data.usage_commands. A
    # multi-command how-to lists each command in order; the guide must show every one,
    # because the self-test ran every one.
    if spec.get("usage_verified"):
        cmds = usage_commands(usage)
        for i, c in enumerate(cmds, 1):
            src = ("usage.command_template (self-tested)" if len(cmds) == 1
                   else f"usage.command_template step {i}/{len(cmds)} (self-tested)")
            out.append({"command": c, "outputs": [], "source": src})
    return out


def validated_in_shipped_image(spec: dict, freeze_record: Optional[dict] = None,
                               valid_digests: Optional[set] = None) -> bool:
    """True iff EVERY validated step ran inside a SHIPPED frozen env image, matched
    by digest — the bytes the user will run are the exact bytes we validated. The
    strongest honesty claim the two-layer model can make.

    Multi-env workflows chain several frozen envs: a step may run in its OWN env
    image (its own freeze). Pass `valid_digests` = the set of ALL frozen env image
    digests (from the EnvCache); each validated step must have run in container with
    a digest IN that set. Single-env (the common case) is the default: omit
    valid_digests and it falls back to the one freeze_record's digest."""
    if valid_digests is None:
        dig = (freeze_record or {}).get("image_digest")
        valid_digests = {dig} if dig else set()
    valid_digests = {d for d in valid_digests if d}
    if not valid_digests:
        return False
    steps = [s for s in (spec.get("pipeline_steps") or []) if isinstance(s, dict)]
    validated = [s for s in steps
                 if s.get("validation") or s.get("validation_status") == "passed"]
    if not validated:
        return False
    return all(_step_ran_in_shipped_image(s, valid_digests) for s in validated)


def _step_ran_in_shipped_image(s: dict, valid_digests: set) -> bool:
    """One validated step's shipped-image claim.

    Base: it ran in a container whose digest is one of the frozen envs'.

    Cluster caveat (C2): for a CLUSTER-locus step the recorded
    container_image_digest is the EnvCache's NOMINAL digest — copied from the
    freeze record, never observed on the cluster. Matching it against the
    EnvCache set is circular. So a cluster step must ALSO carry
    `cluster_image_verified` — proof run_step_on_cluster fingerprinted the
    actual .sif that ran (sha256 + apptainer inspect). A local
    run_step_in_container step captures its digest from `docker inspect` of the
    image that really ran, so it needs no extra proof."""
    if not (s.get("ran_in_container")
            and s.get("container_image_digest") in valid_digests):
        return False
    is_cluster = (s.get("validation_locus") == "cluster"
                  or (s.get("resource_usage") or {}).get("locus") == "cluster")
    if is_cluster:
        return bool(s.get("cluster_image_verified"))
    return True


def _fence(text: str) -> str:
    return f"```\n{text}\n```"


def render_user_guide(spec: dict, freeze_record: Optional[dict] = None,
                      valid_digests: Optional[set] = None) -> str:
    """Render the Markdown user guide. `freeze_record` (from freeze()) supplies
    the HPC delivery + the image/content digests; without it the guide falls
    back to the spec's docker info. `valid_digests` (the set of all frozen env
    digests) makes the shipped-image badge correct for multi-env workflows."""
    name = spec.get("pipeline_name", "pipeline")
    kpkgs = key_packages(spec)
    version = next((v for n, v in kpkgs.items()
                    if n.lower() == name.lower() and v not in ("", "?")), "")
    title = f"{name}" + (f" {version}" if version else "")
    in_shipped = validated_in_shipped_image(spec, freeze_record, valid_digests)
    L: list[str] = [f"# {title} — user guide", ""]
    if spec.get("description"):
        L += [spec["description"], ""]
    if in_shipped:
        L += ["> Generated from a run **inside the shipped image** (digest in Provenance) — "
              "the bytes you run are the bytes we validated. Every command below was executed "
              "there and its outputs checked. Not hand-written.", ""]
    else:
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

    # 2. Run it. The runnable form is the self-tested command_template (container-
    # agnostic — you fill its {PLACEHOLDER} slots), NOT the build-time host paths.
    L += ["## 2. Run it", ""]
    usage = spec.get("usage") or {}
    cmds = executed_commands(spec)
    template_cmds = usage_commands(usage) if spec.get("usage_verified") else []
    if template_cmds:
        order = ("" if len(template_cmds) == 1
                 else " Run them IN ORDER, from one working directory — that is the "
                      "sequence the self-test verified.")
        L += ["Fill the `{PLACEHOLDER}` slots with your inputs. Inside the container the "
              "paths live under the `--bind` mount (e.g. `/data/reads.fastq.gz`)." + order,
              "", _fence("\n".join(template_cmds)), ""]
    elif cmds:
        for c in cmds:
            L += [f"_{c['source']}_", _fence(c["command"]), ""]
    else:
        L += ["_No validated run command recorded yet — run the tool via run_pipeline_step "
              "and set a verified usage.command_template, then regenerate._", ""]

    if usage.get("inputs"):
        L += ["**Inputs**", ""] + [
            f"- `{i.get('name','?')}` — {i.get('format') or i.get('description','')}"
            for i in usage["inputs"] if isinstance(i, dict)] + [""]
    if usage.get("outputs"):
        L += ["**Outputs**", ""]
        for o in usage["outputs"]:
            if not isinstance(o, dict):
                continue
            files = o.get("files")
            patt = (", ".join(f"`{f}`" for f in files) if isinstance(files, list)
                    else (f"`{files}`" if files else ""))
            desc = f" — {o['description']}" if o.get("description") else ""
            L.append(f"- {o.get('name', 'output')}: {patt}{desc}")
        L.append("")

    # The actual executed commands (host/test paths) — proof, kept out of the way.
    proof = [c for c in cmds if str(c["source"]).startswith("pipeline_step")]
    if proof:
        L += ["<details><summary>Validated proof run (executed during the build, on test data)"
              "</summary>", ""]
        for c in proof:
            L += [_fence(c["command"])]
            L += (["Produced + validated:", ""] + [f"- `{o}`" for o in c["outputs"]] + [""]
                  if c["outputs"] else [""])
        L += ["</details>", ""]

    # 3. Environment / driver details (the nf-core gap: record the runner env).
    L += ["## 3. Environment details", ""]
    if spec.get("conda_env"):
        L.append(f"- conda env: `{spec['conda_env']}`")
    if spec.get("python_version"):
        L.append(f"- python: {spec['python_version']}")
    if kpkgs:
        listed = ", ".join(f"{n}={v}" for n, v in list(kpkgs.items())[:12])
        L += [f"- key packages: {listed}" + (" …" if len(kpkgs) > 12 else "")]
    # `platform` and `sha256` were read here for as long as this line has existed and
    # are written by ZERO producers — so this rendered, verbatim, into a user-facing
    # guide: "- shipped binary: `None` (None, sha256 …)". Four times, for the
    # authors'-image env. Absence of data must never render as data (Rule 2); a key no
    # producer writes is now an AttributeError on the model, not a None in a document.
    for sb in _shipped_binaries(freeze_record or {}):
        ver = f" {sb.version}" if sb.version else " (version unrecorded)"
        prov = f" — {sb.provenance}" if sb.provenance else ""
        L.append(f"- shipped binary: `{sb.tool}`{ver}{prov}")
    L.append("")

    # 3b. Reference databases — the biology-half reproducibility anchor. A DB
    # named "vep_cache_111_hg38" is not self-verifying; surface the content
    # sha256 (from the download sidecar, re-derived at seal) so a re-run pins
    # the bytes, not just the name+URL.
    rdbs = [r for r in (spec.get("reference_databases") or []) if isinstance(r, dict)]
    if rdbs:
        L += ["## Reference databases", ""]
        for r in rdbs:
            name = r.get("name", "database")
            ver = f" v{r['version']}" if r.get("version") and r["version"] != "unknown" else ""
            L.append(f"- **{name}**{ver}")
            if r.get("source_url"):
                L.append(f"  - source: {r['source_url']}")
            if r.get("sha256"):
                size = f" ({r['size_bytes']:,} bytes)" if r.get("size_bytes") else ""
                L.append(f"  - sha256: `{r['sha256']}`{size}")
            else:
                L.append(f"  - sha256: _not captured — this DB is pinned by name/URL only, "
                         f"not by content_")
            if r.get("local_path"):
                L.append(f"  - path: `{r['local_path']}`")
        L.append("")

    # 4. Provenance — the machine-verified anchors. Run status is computed from
    # the steps (the draft's pipeline_status is only derived at finalize).
    L += ["## Provenance (machine-verified)", ""]
    fr = freeze_record or {}
    steps = [s for s in (spec.get("pipeline_steps") or []) if isinstance(s, dict)]
    validated = [s for s in steps
                 if s.get("validation") or s.get("validation_status") == "passed"]
    run_status = ("fully_validated" if steps and len(validated) == len(steps)
                  else (spec.get("pipeline_status") or "in_progress"))
    rows = [
        ("content digest", fr.get("content_digest")),
        ("image", fr.get("image")),
        ("image digest", fr.get("image_digest")),
        ("build method", fr.get("build_method")),
        ("lock sha256", spec.get("lock_sha256")),
        ("run status", run_status),
        ("steps validated", f"{len(validated)}/{len(steps)}" if steps else None),
        ("validated in shipped image", "yes — validated == shipped" if in_shipped else None),
        ("usage_verified", spec.get("usage_verified")),
    ]
    L += [f"- {k}: `{v}`" for k, v in rows if v not in (None, "")]
    L.append("")

    # 4b. Cluster run context — when a step's evidence came from a real SLURM
    # job (validation_locus="cluster"), surface what a reader needs to
    # reproduce it: the node, the loaded modules, the SLURM placement, and the
    # sha256 of the .sif that actually ran (the C2 observed fingerprint).
    cluster_steps = [s for s in steps
                     if s.get("validation_locus") == "cluster"
                     or (s.get("resource_usage") or {}).get("locus") == "cluster"]
    if cluster_steps:
        L += ["## Cluster run context", ""]
        for s in cluster_steps:
            tool = s.get("tool", "step")
            L.append(f"- **{tool}** (SLURM job `{s.get('cluster_job_id', '?')}`"
                     f" on `{s.get('cluster_node', '?')}`)")
            if s.get("cluster_sif_sha256"):
                verified = " ✓ verified on cluster" if s.get("cluster_image_verified") else ""
                L.append(f"  - .sif sha256: `{s['cluster_sif_sha256']}`{verified}")
            mods = [m for m in (s.get("cluster_apptainer_module"),
                                s.get("cluster_nextflow_module")) if m]
            if mods:
                L.append(f"  - modules: {', '.join(f'`{m}`' for m in mods)}")
            sc = s.get("cluster_slurm") or {}
            if sc:
                placement = ", ".join(f"{k}={v}" for k, v in sc.items())
                L.append(f"  - slurm: {placement}")
        L.append("")

    return "\n".join(L)
