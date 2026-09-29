"""
pipeline_render — the pipeline DIRECTORY: one record, two ways to run it, written once.

`render_pipeline_files(record, env=)` is pure: it renders the by-hand item
(`commands.sh`), the Nextflow item (`main.nf`, `nextflow.config`, `params.yaml`,
`launcher.sh`, `samples.csv`), runs the honesty lint over both, adds the explain page,
the record itself and a manifest, and returns `{relative path: text}`.
`render_pipeline_dir` writes that under a directory the caller names —
`workspace.pipelines_dir()/<name>` for the MCP primitive — and refuses to replace a
directory whose files were edited since they were rendered.

The honesty lint — `check_rendered_commands`
-------------------------------------------
The page's footer claims the render is mechanical: every command a form carries is a
sealed how-to command with nothing but its placeholders rebound. The lint is what makes
that a checked claim rather than a sentence. It extracts the commands each item will
actually execute (the stage blocks of `commands.sh`, the Nextflow process script
blocks) and requires each to match its stage's template MODULO BINDING: the template is
cut at every binding unit — a `{PLACEHOLDER}`, or an artifact named inside an output
slot, `{OUT}/<name>` — and the literal text between the units must appear verbatim, in
order, with no command added or dropped. HOW a unit is spelled (`${GTF}`,
`${params.stranded}`, `${meta.sample}.counts.tsv`, a bare `.`) is the item's business
and its own tests' job; that the sealed text around the units survived is this module's.
A rendered command that fails is refused, never written.

The directory, LOCKED
---------------------
    pipeline.html       the explain page: what this is and how to run it, both ways
    pipeline.yaml       the record (the ONE input of every file beside it)
    commands.sh         one sample by hand: the sealed commands, values at the top
    samples.csv         one row per sample (per_row only)
    params.yaml         the shared parameters
    main.nf             one inline process per stage
    nextflow.config     local (docker) and slurm (apptainer) profiles, sizing, run records
    launcher.sh         the cluster job: `sbatch launcher.sh`
    MANIFEST.sha256     `sha256sum -c`-checkable; every file but itself
    results/ runs/ work/    made by a run, never by this module, never listed
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable, Mapping, Optional

from agent.skills.pipeline_commands import (CommandsRenderError, commands_in_script,
                                            render_commands)
from agent.skills.pipeline_page_html import render_pipeline_page
from agent.skills.pipeline_record import (RECORD_FILENAME, PipelineDerivationError,
                                          PipelineRecord, PipelineStage, _TOKEN_CHARS,
                                          _PLACEHOLDER_RE, _collapse, record_yaml,
                                          render_samplesheet)
from agent.skills.pipeline_render_nextflow import render_nextflow

PAGE_FILENAME = "pipeline.html"
MANIFEST_FILENAME = "MANIFEST.sha256"
COMMANDS_FILENAME = "commands.sh"
SAMPLESHEET_FILENAME = "samples.csv"

#: A manifest line, as `sha256sum` writes and reads it.
_MANIFEST_LINE_RE = re.compile(r"^([0-9a-f]{64})  (.+)$")
#: What a bound unit may render as: a shell variable, a Groovy expression, a bare `.`, a
#: file name — never whitespace and never a shell operator, so a command with something
#: appended after its last binding does not pass as "the binding spelled longer".
_BOUND_UNIT = r"([^\s|;&<>]+?)"


class PipelineRenderError(PipelineDerivationError):
    """A refusal at render time: a rendered command drifted from its sealed template,
    an item cannot carry the record, or the target directory holds edits. Same shape
    as a derivation refusal (`code`, `error`, `remedy`) so one wrapper handles both."""


# ── the honesty lint ────────────────────────────────────────────────────────


def _binding_pattern(template: str, out_slots: list[str]) -> re.Pattern:
    """`template` with every binding unit replaced by a wildcard and every literal piece
    escaped. A unit is an artifact named inside an output slot (`{OUT}/<name>`, the
    name itself possibly holding placeholders) or a bare `{PLACEHOLDER}`."""
    alternatives = []
    if out_slots:
        slots = "|".join(re.escape(s) for s in sorted(out_slots))
        alternatives.append(r"\{(?:" + slots + r")\}/" + _TOKEN_CHARS)
    alternatives.append(_PLACEHOLDER_RE.pattern)
    unit_re = re.compile("|".join(alternatives))
    parts: list[str] = []
    pos = 0
    for m in unit_re.finditer(template):
        parts.append(re.escape(_collapse(template[pos:m.start()])))
        parts.append(_BOUND_UNIT)
        pos = m.end()
    parts.append(re.escape(_collapse(template[pos:])))
    return re.compile("".join(parts))


def command_matches_template(rendered: str, template: str, out_slots: list[str]) -> bool:
    """Does `rendered` equal `template` modulo binding? Whitespace runs compare equal;
    everything else outside the binding units must be verbatim."""
    return _binding_pattern(template.strip(), out_slots).fullmatch(
        _collapse(rendered.strip())) is not None


def _commands_sh(files: Mapping[str, str], stage: PipelineStage) -> Optional[list[str]]:
    text = files.get(COMMANDS_FILENAME)
    if text is None:
        return None
    return commands_in_script(text, stage) or []


def _nextflow_commands(files: Mapping[str, str], stage: PipelineStage) -> Optional[list[str]]:
    """The lines of `process <stage> { … script: \"\"\" … \"\"\" }` in main.nf."""
    text = files.get("main.nf")
    if text is None:
        return None
    lines = text.splitlines()
    head = re.compile(r"^\s*process\s+" + re.escape(stage.name) + r"\s*\{")
    i = next((k for k, ln in enumerate(lines) if head.match(ln)), None)
    if i is None:
        return []
    depth, k = 0, i
    while k < len(lines):
        depth += lines[k].count("{") - lines[k].count("}")
        if lines[k].strip() == '"""':
            body: list[str] = []
            k += 1
            while k < len(lines) and lines[k].strip() != '"""':
                body.append(lines[k])
                k += 1
            return body
        if depth <= 0 and k > i:
            break
        k += 1
    return []


_EXTRACTORS: dict[str, Callable[[Mapping[str, str], PipelineStage], Optional[list[str]]]] = {
    COMMANDS_FILENAME: _commands_sh,
    "main.nf": _nextflow_commands,
}


def check_rendered_commands(record: PipelineRecord, files: Mapping[str, str]) -> None:
    """Refuse unless every command each item will execute is its stage's sealed template
    modulo binding, with none added and none dropped. An item's file missing entirely is
    a refusal too — a lint that finds nothing to check must not pass."""
    slots = list(record.output_slots)
    for item, extract in _EXTRACTORS.items():
        for stage in record.stages:
            rendered = extract(files, stage)
            if rendered is None:
                raise PipelineRenderError(
                    "pipeline.render_drift",
                    f"{item} was not rendered, so stage {stage.name}'s commands cannot be checked",
                    "this is a renderer defect, not a record problem; report it")
            if len(rendered) != len(stage.commands):
                raise PipelineRenderError(
                    "pipeline.render_drift",
                    f"{item}, stage {stage.name}: the rendered file executes {len(rendered)} "
                    f"command(s) but the sealed how-to has {len(stage.commands)} for this stage: "
                    f"{rendered!r}",
                    "this is a renderer defect, not a record problem; report it")
            for got, template in zip(rendered, stage.commands):
                if not command_matches_template(got, template, slots):
                    raise PipelineRenderError(
                        "pipeline.render_drift",
                        f"{item}, stage {stage.name}: the rendered command is not the sealed "
                        f"template with only its placeholders rebound.\n"
                        f"  sealed:   {_collapse(template.strip())}\n"
                        f"  rendered: {_collapse(got.strip())}",
                        "this is a renderer defect, not a record problem; report it")
    if record.samplesheet is not None:
        if files.get(SAMPLESHEET_FILENAME) != render_samplesheet(record):
            raise PipelineRenderError(
                "pipeline.render_drift",
                f"{SAMPLESHEET_FILENAME} is not the record's own samplesheet rendering",
                "this is a renderer defect, not a record problem; report it")


# ── the file set ────────────────────────────────────────────────────────────


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _manifest(files: Mapping[str, str]) -> str:
    """`sha256sum` format over every file but the manifest itself, sorted by path."""
    lines = [f"{_sha256_text(files[path])}  {path}"
             for path in sorted(files) if path != MANIFEST_FILENAME]
    return "\n".join(lines) + "\n"


def parse_manifest(text: str) -> dict[str, str]:
    """`{path: sha256}` from a manifest's text; a malformed line is a ValueError."""
    out: dict[str, str] = {}
    for ln in text.splitlines():
        if not ln.strip():
            continue
        m = _MANIFEST_LINE_RE.match(ln)
        if not m:
            raise ValueError(f"malformed manifest line: {ln!r}")
        out[m.group(2)] = m.group(1)
    return out


def render_pipeline_files(record: PipelineRecord, *, env: Optional[Mapping] = None) -> dict[str, str]:
    """Every file of the pipeline directory, `{relative path: text}`, linted. Pure.

    `env` is a compute-env block from projects_access.yaml, handed to the Nextflow
    renderer for the launcher's SLURM policy. A refusal from an item's renderer (a
    `ValueError` naming its remedy) is re-raised as `PipelineRenderError` so the caller
    sees one exception family."""
    files: dict[str, str] = {}
    try:
        files[COMMANDS_FILENAME] = render_commands(record)
    except CommandsRenderError as e:
        raise PipelineRenderError(
            "pipeline.form_refused", f"commands.sh cannot carry this record: {e}",
            "change the record or the sealed how-to as the message says") from e
    try:
        rendered = render_nextflow(record, env=dict(env) if env is not None else None)
    except PipelineDerivationError:
        raise
    except ValueError as e:
        raise PipelineRenderError(
            "pipeline.form_refused", f"the Nextflow form cannot carry this record: {e}",
            "change the record (stages=, resources=, per_sample=) as the message says") from e
    for path, text in rendered.items():
        if path in files:
            raise PipelineRenderError(
                "pipeline.render_drift", f"two items rendered {path}",
                "this is a renderer defect, not a record problem; report it")
        files[path] = text
    for reserved in (RECORD_FILENAME, PAGE_FILENAME, MANIFEST_FILENAME):
        if reserved in files:
            raise PipelineRenderError(
                "pipeline.render_drift", f"an item rendered the reserved file {reserved}",
                "this is a renderer defect, not a record problem; report it")
    check_rendered_commands(record, files)
    files[RECORD_FILENAME] = record_yaml(record)
    files[PAGE_FILENAME] = render_pipeline_page(record)
    files[MANIFEST_FILENAME] = _manifest(files)
    return files


# ── the directory ───────────────────────────────────────────────────────────


def _edited_since_render(out_dir: Path, manifest: Mapping[str, str]) -> list[str]:
    """Files the manifest lists whose bytes on disk no longer hash to it (or are gone)."""
    edited = []
    for rel, digest in sorted(manifest.items()):
        p = out_dir / rel
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != digest:
            edited.append(rel)
    return edited


def render_pipeline_dir(record: PipelineRecord, out_dir: Path, *,
                        env: Optional[Mapping] = None,
                        overwrite: bool = False) -> dict:
    """Render the directory into `out_dir` and return what was written.

    An existing, non-empty `out_dir` is replaced only when it is a previous render of
    this system (it carries a manifest) whose files are unedited; otherwise the call
    refuses — a hand-edited samples.csv or params.yaml is the user's work — unless
    `overwrite=True`. Files of the previous render that this render does not produce
    are removed; anything the manifest never listed (`results/`, `runs/`, `work/`, the
    user's own files) is left alone. Every `*.sh` is made executable."""
    out_dir = Path(out_dir)
    files = render_pipeline_files(record, env=env)

    previous: dict[str, str] = {}
    existing = [p for p in out_dir.iterdir()] if out_dir.is_dir() else []
    if existing:
        manifest_path = out_dir / MANIFEST_FILENAME
        if not manifest_path.is_file():
            if not overwrite:
                raise PipelineRenderError(
                    "pipeline.dir_not_a_render",
                    f"{out_dir} exists and holds files this system did not render "
                    f"(no {MANIFEST_FILENAME})",
                    "render under another name, or pass overwrite=True to write into it anyway")
        else:
            try:
                previous = parse_manifest(manifest_path.read_text())
            except ValueError as e:
                if not overwrite:
                    raise PipelineRenderError(
                        "pipeline.dir_not_a_render",
                        f"{manifest_path} is not a manifest this system wrote: {e}",
                        "render under another name, or pass overwrite=True") from e
                previous = {}
            edited = _edited_since_render(out_dir, previous)
            if edited and not overwrite:
                raise PipelineRenderError(
                    "pipeline.dir_edited",
                    f"{out_dir} was rendered before and {len(edited)} file(s) were edited "
                    f"since: {edited}",
                    "copy your edits aside and re-run, or pass overwrite=True to replace them")

    removed = []
    for rel in sorted(previous):
        if rel not in files and (out_dir / rel).is_file():
            (out_dir / rel).unlink()
            removed.append(rel)
    out_dir.mkdir(parents=True, exist_ok=True)
    for rel, text in files.items():
        p = out_dir / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
        if rel.endswith(".sh"):
            p.chmod(p.stat().st_mode | 0o111)
    return {
        "dir": str(out_dir),
        "files": sorted(files),
        "page": str(out_dir / PAGE_FILENAME),
        "record": str(out_dir / RECORD_FILENAME),
        "manifest": str(out_dir / MANIFEST_FILENAME),
        "replaced_previous_render": bool(previous),
        "removed": removed,
    }


__all__ = ["PipelineRenderError", "PAGE_FILENAME", "MANIFEST_FILENAME", "COMMANDS_FILENAME",
           "command_matches_template", "check_rendered_commands", "parse_manifest",
           "render_pipeline_files", "render_pipeline_dir"]
