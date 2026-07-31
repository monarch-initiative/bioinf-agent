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

from agent.models.core_data import (shipped_binaries as _shipped_binaries,
                                    step_is_validated, usage_commands, usage_label)


def _version_of(pkg: dict) -> str:
    """Best-known INSTALLED version of a package record. `version` is what the
    package manager recorded installing; a release binary records its version only
    in the release tag of `binary_url` — the asset actually downloaded, an install
    fact, so it stays.

    It must NOT scrape the `conda_spec` / `pip_spec` string (audit 2026-07-19, W3):
    that string is the REQUESTED constraint (`samtools=1.21`, or even `>=1.10`), not
    what resolved — when conda actually installs it, the resolved version lands in
    `version`. Returning the spec here printed the requested number as installed on
    exactly the tiers that don't capture a version. Absence returns '?' (unknown),
    which the title filters out and the key-packages line shows as unknown — never a
    borrowed request."""
    v = pkg.get("version")
    if v:
        return str(v)
    im = pkg.get("install_method") if isinstance(pkg.get("install_method"), dict) else {}
    url = im.get("binary_url") or im.get("source") or ""
    m = re.search(r"/releases/download/([^/]+)/", url)   # .../download/v2.13.0/asset
    if m:
        return m.group(1).lstrip("vV")
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


def _observed_versions(freeze_record: dict) -> dict[str, str]:
    """name_lower → OBSERVED installed version, read from the shipped image, for EVERY
    package the image reveals — the full SBOM closure PLUS each requested tool via the
    shared `_resolved_version` (which also catches a source-compiled tool that prints
    its version only in an in-image banner). THE definition the ENV report uses, so the
    guide cites the same numbers.

    Covers dependencies, not just requested tools (audit 2026-07-20): the guide's
    key-packages line shows dependency versions too, and those come from a REQUEST-
    derived install-step record (`install_conda_packages` writes the parsed spec pin,
    not conda's resolved output). When the image is available its SBOM is the truth, so
    an observed version here overrides the request-pin everywhere the caller shows a
    package version. A package the image doesn't reveal is OMITTED (absence is not a
    version) — the caller never substitutes the request for it."""
    from agent.skills.env_report_helpers import (
        _pkg_index, _resolved_version, _verif_index)
    resolved = freeze_record.get("resolved_packages") or []
    pidx = _pkg_index(resolved)
    vidx = _verif_index(freeze_record.get("verifications") or [])
    shipped = freeze_record.get("shipped_binaries") or []
    out: dict[str, str] = {}
    # the full observed SBOM closure — every package the shipped image actually holds
    for p in resolved:
        if isinstance(p, dict) and p.get("name") and p.get("version"):
            out[p["name"].lower()] = str(p["version"])
    # each requested tool through the shared resolver (banner/shipped-binary fallbacks
    # a bare SBOM row can't provide, e.g. a source-compiled primary tool)
    for tool in (freeze_record.get("requested_tools") or []):
        nm = str(tool).split("=")[0].strip()
        if not nm:
            continue
        v = _resolved_version(nm, pidx.get(nm.lower()), vidx.get(nm.lower()), shipped)
        if v:
            out[nm.lower()] = v
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
        validated = step_is_validated(s)
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
    validated = [s for s in steps if step_is_validated(s)]
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


# SLURM key → srun flag. Used only to render a runnable "grab a node" line from the
# placement THIS run actually recorded (cluster_slurm) — recorded facts, not invented.
_SLURM_FLAG = {
    "account": "--account", "partition": "--partition", "queue": "--partition",
    "queue_default": "--partition", "cpus": "--cpus-per-task",
    "cpus_per_task": "--cpus-per-task", "mem": "--mem", "time": "--time",
}


def _srun_line(sc: Optional[dict]) -> tuple[str, bool]:
    """Render an interactive `srun … --pty bash` from a recorded SLURM placement.
    Returns (line, from_record). When the record carries no placement we fall back
    to a clearly-labelled EXAMPLE the reader sizes to their own job — never passed
    off as the validated placement."""
    flags = [f"{_SLURM_FLAG[k]}={v}" for k, v in (sc or {}).items()
             if k in _SLURM_FLAG and v not in (None, "")]
    if flags:
        return "srun " + " ".join(flags) + " --pty bash", True
    return "srun --cpus-per-task=4 --mem=16g --time=2:00:00 --pty bash", False


def _output_files(spec: dict, cmds: list[dict]) -> list[str]:
    """The files this workflow produces — declared usage.outputs first, else the
    validated steps' detected outputs. De-duplicated, order preserved."""
    outs: list[str] = []
    for o in ((spec.get("usage") or {}).get("outputs") or []):
        if isinstance(o, dict):
            f = o.get("files")
            outs += f if isinstance(f, list) else ([f] if f else [])
    if not outs:
        for c in cmds:
            outs += c.get("outputs") or []
    return list(dict.fromkeys(str(o) for o in outs if o))


def _prereqs_block(spec: dict, freeze_record: Optional[dict]) -> list[str]:
    """'What you need before you start' — the container, reference DBs, inputs, and a
    work dir, as a table. Every row is DERIVED from the verified record; nothing is
    invented. Empty → the section is omitted."""
    fr = freeze_record or {}
    rows: list[tuple[str, str]] = []
    img = fr.get("image") or (spec.get("docker") or {}).get("image_tag")
    if img:
        rows.append(("The container", f"the whole tool, frozen — nothing to install · `{img}`"))
    for r in (spec.get("reference_databases") or []):
        if isinstance(r, dict) and r.get("name"):
            v = f" v{r['version']}" if r.get("version") and r["version"] != "unknown" else ""
            where = f"`{r['local_path']}`" if r.get("local_path") else "external reference data"
            rows.append((f"Reference — {r['name']}{v}", where))
    for i in ((spec.get("usage") or {}).get("inputs") or []):
        if isinstance(i, dict):
            rows.append((f"Input — {i.get('name', '?')}",
                         i.get("format") or i.get("description") or "your data"))
    if not rows:
        return []
    rows.append(("A work dir", "an empty scratch directory for this run's outputs"))
    L = ["## What you need before you start", "", "| # | Thing | What it is |",
         "|---|-------|------------|"]
    L += [f"| {n} | **{k}** | {v} |" for n, (k, v) in enumerate(rows, 1)]
    L.append("")
    return L


def render_user_guide(spec: dict, freeze_record: Optional[dict] = None,
                      valid_digests: Optional[set] = None,
                      narrative: Optional[dict] = None) -> str:
    """Render the Markdown user guide as a hand-holding, copy-pasteable WALKTHROUGH
    of how to run the tool on the compute resource it was validated against — the
    shape of the Talos guide, generated honestly.

    Two halves, kept visibly distinct:
      * the SKELETON is DERIVED from the verified record — the container, the
        compute-node acquisition (`srun` from the recorded SLURM placement), the
        `module load` lines, the ordered VALIDATED commands, inputs/outputs,
        provenance. `executed_commands()` remains the single honesty hook, so a
        command that never ran can't appear.
      * the NARRATIVE is agent-AUTHORED and optional — `narrative={'overview': str,
        'traps': [str, ...]}`. It is the biology/why-prose only a human can write;
        it is clearly labelled as authored (not machine-verified), and when absent
        the guide simply omits it rather than fabricate it.

    `freeze_record` supplies the HPC delivery + digests; `valid_digests` makes the
    shipped-image badge correct for multi-env workflows."""
    narrative = narrative or {}
    # A draft carries `pipeline_name`; a sealed WorkflowSpec carries `workflow_name`.
    # The guide renders from either, so accept both.
    name = spec.get("pipeline_name") or spec.get("workflow_name") or "pipeline"
    kpkgs = key_packages(spec)
    # When a frozen env is pinned, EVERY version this guide cites must be OBSERVED in the
    # shipped image, not the requested spec (audit 2026-07-19 W3 / 2026-07-20 hunt): the
    # draft's install-step `installed_packages[].version` is the parsed REQUEST pin
    # (install_conda_packages writes the spec, not conda's resolved output), so the title,
    # key-packages line, AND dependency rows would otherwise show the request. The image
    # SBOM is the truth — an observed version overrides the request-pin per package; a
    # package the image doesn't reveal is left as-is (never a substituted request), and
    # requested tools the image reveals are added so the title/key-packages can cite them.
    obs = _observed_versions(freeze_record) if freeze_record else {}
    if obs:
        kpkgs = {n: obs.get(n.lower(), v) for n, v in kpkgs.items()}
        for tool in (freeze_record.get("requested_tools") or []):
            nm = str(tool).split("=")[0].strip()
            if nm and nm.lower() in obs and not any(k.lower() == nm.lower() for k in kpkgs):
                kpkgs[nm] = obs[nm.lower()]
    version = next((v for n, v in kpkgs.items()
                    if n.lower() == name.lower() and v not in ("", "?")), "")
    title = f"{name}" + (f" {version}" if version else "")
    in_shipped = validated_in_shipped_image(spec, freeze_record, valid_digests)
    usage = spec.get("usage") or {}
    hpc = (freeze_record or {}).get("hpc_delivery") or {}
    steps = [s for s in (spec.get("pipeline_steps") or []) if isinstance(s, dict)]
    cluster_steps = [s for s in steps
                     if s.get("validation_locus") == "cluster"
                     or (s.get("resource_usage") or {}).get("locus") == "cluster"]
    cmds = executed_commands(spec)
    template_cmds = usage_commands(usage) if spec.get("usage_verified") else []

    L: list[str] = [f"# {title} — how to run it", ""]
    if spec.get("description"):
        L += [spec["description"], ""]
    outs = _output_files(spec, cmds)
    if outs:
        L += ["**By the end you have:** " + ", ".join(f"`{o}`" for o in outs) + ".", ""]
    if in_shipped:
        L += ["> Every command below was executed **inside the shipped image** (digest in "
              "Provenance) and its outputs validated — the bytes you run are the bytes we "
              "checked. Any _italic notes_ are authored explanation, not machine-verified.", ""]
    else:
        L += ["> Every command below was executed during the build and its outputs validated "
              "(provenance at the bottom). Any _italic notes_ are authored explanation, not "
              "machine-verified.", ""]

    # OVERVIEW — authored ("what the tool does, 30-second version"). Omitted if absent.
    if narrative.get("overview"):
        L += [f"## What {name} does", "", str(narrative["overview"]).strip(), ""]

    # PREREQUISITES — derived table.
    L += _prereqs_block(spec, freeze_record)

    # RUN IT — the walkthrough spine: get onto the compute + load runtime, get the
    # container, then the validated command sequence. Each numbered step present only
    # when the record supports it (no fabricated cluster steps for a local-only run).
    L += ["## Run it", ""]
    n = 1
    if cluster_steps:
        sc = next((s.get("cluster_slurm") for s in cluster_steps if s.get("cluster_slurm")), {}) or {}
        srun, from_record = _srun_line(sc)
        tail = "" if from_record else "  # example — size this to your job"
        L += [f"**{n}. Grab a compute node** — never run compute on the login node:",
              "", _fence(srun + tail), ""]
        n += 1
        mods: list[str] = []
        for s in cluster_steps:
            for m in (s.get("cluster_apptainer_module"), s.get("cluster_nextflow_module")):
                if m and m not in mods:
                    mods.append(m)
        if mods:
            L += [f"**{n}. Load the runtime module(s):**", "",
                  _fence("\n".join(f"module load {m}" for m in mods)), ""]
            n += 1
    if hpc.get("get_image"):
        note = f" — _{hpc['source_note']}_" if hpc.get("source_note") else ""
        L += [f"**{n}. Get the container**{note}:", "", _fence(hpc["get_image"]), ""]
        n += 1
        if hpc.get("run_example"):
            L += ["Run a command inside it with:", "", _fence(hpc["run_example"]), ""]
        if hpc.get("sbatch_template"):
            L += ["<details><summary>SLURM batch template (for a non-interactive run)</summary>",
                  "", _fence(hpc["sbatch_template"]), "", "</details>", ""]
    else:
        docker = spec.get("docker") or {}
        tag = docker.get("image_tag") or f"{name}:{version or 'latest'}"
        L += [f"**{n}. Get the container** (Apptainer):", "",
              _fence(f"apptainer pull {name}.sif docker://{tag}\n"
                     f"apptainer exec --bind /scratch/$USER/data:/data {name}.sif <command>"), ""]
        n += 1

    order = ("" if len(template_cmds) <= 1
             else " Run them IN ORDER, from one working directory — the sequence the self-test verified.")
    L += [f"**{n}. Run the workflow** — the validated command sequence." + order, ""]
    if template_cmds:
        L += ["Fill the `{PLACEHOLDER}` slots with your inputs. Inside the container the paths "
              "live under the `--bind` mount (e.g. `/data/reads.fastq.gz`).",
              "", _fence("\n".join(template_cmds)), ""]
    elif cmds:
        for c in cmds:
            L += [f"_{c['source']}_", "", _fence(c["command"]), ""]
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

    # TRAPS — authored hard-won gotchas ("traps we already hit"). The most valuable
    # authored section, and one no record can hold; rendered only when supplied, never
    # fabricated.
    traps = [t for t in (narrative.get("traps") or []) if str(t).strip()]
    if traps:
        L += ["## Traps to avoid", ""] + [f"{i}. {str(t).strip()}" for i, t in enumerate(traps, 1)] + [""]

    # Environment / driver details (the nf-core gap: record the runner env).
    L += ["## Environment details", ""]
    if spec.get("conda_env"):
        L.append(f"- conda env: `{spec['conda_env']}`")
    # `python_version` on the spec is the env-CREATION request (or a config default),
    # never re-observed. When the shipped image is pinned, its SBOM carries the real
    # python — show THAT (audit 2026-07-20 hunt: the guide printed the requested 3.11
    # while the image shipped 3.10.14). Without an image there is nothing to contradict
    # the created python, so the draft value stands.
    py = obs.get("python") or spec.get("python_version")
    if py:
        L.append(f"- python: {py}")
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
    validated = [s for s in steps if step_is_validated(s)]
    # Prefer the status the producer STATED on the spec (self-describing); derive
    # from the steps only if absent (pre-fix specs). ONE definition —
    # spec_writer.derive_pipeline_status — shared with the seal that wrote it, so
    # the stored value and any fallback can never disagree.
    from agent.skills.spec_writer import derive_pipeline_status
    run_status = spec.get("pipeline_status") or derive_pipeline_status(steps)
    rows = [
        ("content digest", fr.get("content_digest")),
        ("image", fr.get("image")),
        ("image digest", fr.get("image_digest")),
        ("build method", fr.get("build_method")),
        ("lock sha256", spec.get("lock_sha256")),
        ("run status", run_status),
        ("steps validated", f"{len(validated)}/{len(steps)}" if steps else None),
        ("validated in shipped image", "yes — validated == shipped" if in_shipped else None),
        # THE THREE-STATE READ (core_data.usage_status), not the bool. Printing
        # `usage_verified: False` into a how-to document tells a reader the command
        # was tested and does not work; seal REFUSES that case, so it has only ever
        # meant nobody ran it. This guide was the last artifact still saying it.
        ("usage self-tested", usage_label(spec)),
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

    # TL;DR — the absolute-minimum runnable sequence, derived: the compute preamble
    # (from the recorded placement/modules) + the validated commands, nothing else.
    seq = template_cmds or [c["command"] for c in cmds]
    if seq:
        pre: list[str] = []
        if cluster_steps:
            sc0 = next((s.get("cluster_slurm") for s in cluster_steps if s.get("cluster_slurm")), {}) or {}
            srun, from_record = _srun_line(sc0)
            pre.append(srun + ("" if from_record else "   # example — size to your job"))
            seen: set = set()
            for s in cluster_steps:
                for m in (s.get("cluster_apptainer_module"), s.get("cluster_nextflow_module")):
                    if m and m not in seen:
                        seen.add(m)
                        pre.append(f"module load {m}")
        L += ["## TL;DR", "", "```bash", *pre, *seq, "```", ""]

    return "\n".join(L)
