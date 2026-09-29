"""
pipeline_commands — the by-hand item on the menu, and the run lines the page shows.

A pipeline directory offers two ways to run, and no more: ONE sample by hand
(`commands.sh`, this module) and many samples with Nextflow (`pipeline_render_nextflow`).
Both come from the same record; the page shows how to run each, at each locus, as three
steps a person can follow without thinking: change directory, enter the environment,
run. This module is the ONE spelling of those lines — `commands.sh` and the page read
it, so they cannot disagree.

`commands.sh` is the sealed how-to for one sample, nothing else: the values at the top
(the seal's own example row, to be replaced), then the commands in order, each
placeholder bound to a shell variable. It runs INSIDE the pipeline's image; entering the
image is a step the page shows (`enter_image`), not something the script does, because
a script that wraps itself in docker or apptainer is no longer the commands.
"""
from __future__ import annotations

import re
from typing import Optional

from agent.skills.pipeline_record import (PipelineRecord, PipelineStage, _PLACEHOLDER_RE)

#: The variable every output slot binds to — one directory per sample.
OUTPUT_VAR = "OUTPUT_DIR"
#: The row identifier's variable, the samplesheet's `sample` column.
ROW_VAR = "SAMPLE"
#: What a value in `commands.sh` may hold: it travels through a shell line unquoted.
_VALUE_RE = re.compile(r"^[A-Za-z0-9_./:@%+=,-]*$")
#: A `{NAME}` that is a record placeholder — not the `{NAME}` inside a `${NAME}` the
#: sealed command already carried.
_BIND_RE = re.compile(r"(?<!\$)" + _PLACEHOLDER_RE.pattern)
#: The line that opens each stage's commands in `commands.sh`; the honesty lint reads
#: the commands back by it.
STAGE_MARKER_RE = re.compile(r"^# (\d+)\. ([A-Z][A-Z0-9_]*)\b")

STRICT_MODE_LINE = ("set -euo pipefail   # bash strict mode: stop at the first failing command, "
                    "an unset variable, or a failure inside a pipe")


class CommandsRenderError(ValueError):
    """The by-hand form cannot carry this record; the message names the remedy."""


# ── what the page needs to say ──────────────────────────────────────────────


def example_values(record: PipelineRecord) -> dict[str, str]:
    """`{PLACEHOLDER: value}` for one sample: the shared defaults plus the first
    samplesheet row (per_row) or the per-sample defaults (linear)."""
    values: dict[str, str] = {}
    for p in record.params:
        if p.default is not None:
            values[p.name] = p.default
    if record.samplesheet is not None and record.samplesheet.rows:
        row = record.samplesheet.rows[0]
        for c in record.samplesheet.columns:
            if c.name in row:
                values[c.placeholder] = str(row[c.name])
    return values


def data_dirs(record: PipelineRecord) -> list[str]:
    """Every directory a path-valued example lives in, sorted and deduplicated — what a
    container has to see for the example to run unchanged."""
    dirs: set[str] = set()
    kinds = {p.name: p.value_kind for p in record.params}
    if record.samplesheet is not None:
        kinds.update({c.placeholder: c.value_kind for c in record.samplesheet.columns})
    for name, value in example_values(record).items():
        if kinds.get(name) in ("path", "prefix") and value.startswith("/"):
            dirs.add(value.rsplit("/", 1)[0] or "/")
    return sorted(d for d in dirs if d != "/")


def images(record: PipelineRecord) -> list[tuple[str, str]]:
    """The distinct (image, digest) pairs the stages run in, in stage order."""
    seen: list[tuple[str, str]] = []
    for st in record.stages:
        pair = (st.image or "", st.image_digest or "")
        if pair not in seen:
            seen.append(pair)
    return seen


def sif_for(record: PipelineRecord) -> Optional[str]:
    """The .sif path when the render named a cluster, else None."""
    for st in record.stages:
        if st.sif_path:
            return st.sif_path
    return None


def enter_image(record: PipelineRecord, locus: str) -> list[str]:
    """The line(s) that put a shell INSIDE the pipeline's image, with the pipeline
    directory and every example data directory visible at their own paths.

    locus `local`: one `docker run` per distinct image (one, almost always).
    locus `hpc`: `module load` when the record names modules, then `apptainer shell`
    on the .sif — the real path when the render named a cluster, a placeholder that
    says where it comes from otherwise."""
    if locus not in ("local", "hpc"):
        raise ValueError(f"locus must be 'local' or 'hpc', got {locus!r}")
    binds = data_dirs(record)
    if locus == "local":
        mounts = " ".join(f'-v "{d}":"{d}"' for d in binds)
        lines = []
        for image, _digest in images(record):
            lines.append(f'docker run --rm -it -v "$PWD":"$PWD" -w "$PWD" {mounts} {image} bash'.replace("  ", " "))
        return lines
    lines = []
    if record.modules:
        lines.append("module load " + " ".join(record.modules))
    sif = sif_for(record) or "<the .sif that stage_apptainer_image put in the cluster's container zone>"
    bind = f" --bind {','.join(binds)}" if binds else ""
    lines.append(f'apptainer shell --cleanenv{bind} --pwd "$PWD" {sif}')
    return lines


def nextflow_run(record: PipelineRecord, locus: str) -> list[str]:
    """How to run the samplesheet through Nextflow at a locus."""
    if locus == "local":
        return ["nextflow run main.nf -profile local -params-file params.yaml -resume"]
    if locus == "hpc":
        return ["sbatch launcher.sh"]
    raise ValueError(f"locus must be 'local' or 'hpc', got {locus!r}")


# ── commands.sh ─────────────────────────────────────────────────────────────


def _check_value(name: str, value: str) -> None:
    if not _VALUE_RE.match(value):
        raise CommandsRenderError(
            f"the example value of {name} ({value!r}) holds whitespace or a shell "
            f"metacharacter and cannot be written into commands.sh; re-seal the how-to with "
            f"a plain value or set it by hand in the script")


def bind(record: PipelineRecord, command: str) -> str:
    """The sealed command with every placeholder bound to its shell variable: an output
    slot to `${OUTPUT_DIR}`, a param or column to `${NAME}`, a per-sample identifier
    that is not a column to `${SAMPLE}`. Anything else is refused: the record decides
    what a placeholder is."""
    known = {p.name for p in record.params}
    if record.samplesheet is not None:
        known |= {c.placeholder for c in record.samplesheet.columns}
    per_sample_values = {p.name for p in record.params if p.kind == "per_sample" and p.value_kind == "value"}

    def sub(m: re.Match) -> str:
        name = m.group(1)
        if name in record.output_slots:
            return "${" + OUTPUT_VAR + "}"
        if name in known:
            return "${" + name + "}"
        if name in per_sample_values:
            return "${" + ROW_VAR + "}"
        raise CommandsRenderError(
            f"placeholder {{{name}}} in the sealed how-to is neither a param, a samplesheet "
            f"column nor an output slot of the record; re-derive the record")
    return _BIND_RE.sub(sub, command)


def _describe(record: PipelineRecord, name: str) -> str:
    for p in record.params:
        if p.name == name:
            bits = [{"path": "a path", "prefix": "a prefix (a family of files named after it)",
                     "value": "a value"}[p.value_kind]]
            if p.description:
                bits.append(p.description)
            if p.source.startswith("sealed_step:"):
                bits.append(f"produced by sealed step {p.source.split(':', 1)[1]} of the workflow")
            return "; ".join(bits)
    if record.samplesheet is not None:
        for c in record.samplesheet.columns:
            if c.placeholder == name:
                return c.description or f"samplesheet column `{c.name}`"
    return ""


def render_commands(record: PipelineRecord) -> str:
    """`commands.sh`: one sample by hand. Values first, commands after, one stage marker
    per stage so the honesty lint can read the commands back."""
    values = example_values(record)
    for name, value in values.items():
        _check_value(name, value)
    n = len(record.stages)
    per_sample = [p.name for p in record.params if p.kind == "per_sample"]
    if record.samplesheet is not None:
        per_sample = [c.placeholder for c in record.samplesheet.columns]
    shared = [p.name for p in record.params if p.kind == "shared"]

    L = ["#!/usr/bin/env bash",
         f"# {record.name} — ONE sample by hand — rendered from sealed workflow {record.sealed_workflow}",
         "#",
         "# Run this INSIDE the pipeline's image, from the pipeline directory (pipeline.html shows",
         "# how to enter it). Edit the values below, then:  bash commands.sh",
         STRICT_MODE_LINE,
         ""]
    L.append("# ── the sample: the sealed run's own example — replace it with yours ──")
    for name in per_sample:
        desc = _describe(record, name)
        L.append(f"{name}={values.get(name, '')}" + (f"   # {desc}" if desc else ""))
    L.append("")
    if shared:
        L.append("# ── shared parameters: the values the sealed run was validated with ──")
        for name in shared:
            desc = _describe(record, name)
            L.append(f"{name}={values.get(name, '')}" + (f"   # {desc}" if desc else ""))
        L.append("")
    # One directory per row, as the sealed run had: results/<sample>/ when there is a
    # samplesheet, results/ itself for a one-row pipeline — the layout the Nextflow form
    # publishes to and the page describes.
    L += ["# ── where this sample's files go ──",
          f'{OUTPUT_VAR}="$PWD/results/${ROW_VAR}"' if record.samplesheet is not None
          else f'{OUTPUT_VAR}="$PWD/results"',
          f'mkdir -p "${OUTPUT_VAR}"',
          ""]
    for st in record.stages:
        head = f"# {st.index + 1}. {st.name} — {st.tool}"
        if len(images(record)) > 1:
            head += f" (image {st.image or st.image_digest})"
        L.append(head)
        for c in st.commands:
            L.append(bind(record, c))
        L.append("")
    _ = n
    return "\n".join(L).rstrip("\n") + "\n"


def commands_in_script(text: str, stage: PipelineStage) -> Optional[list[str]]:
    """The command lines `commands.sh` runs for `stage`: the non-blank lines after its
    marker, up to the next blank line. None when the marker is absent."""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        m = STAGE_MARKER_RE.match(ln)
        if m and m.group(2) == stage.name and int(m.group(1)) == stage.index + 1:
            out: list[str] = []
            for nxt in lines[i + 1:]:
                if not nxt.strip():
                    break
                out.append(nxt)
            return out
    return None


__all__ = ["render_commands", "enter_image", "nextflow_run", "example_values", "data_dirs",
           "images", "sif_for", "bind", "commands_in_script", "CommandsRenderError",
           "STRICT_MODE_LINE", "OUTPUT_VAR", "ROW_VAR"]
