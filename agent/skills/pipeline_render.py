"""
pipeline_render — the pipeline DIRECTORY: every requested form of ONE record, written once.

`render_pipeline_files(record, forms=, env=)` is pure: it asks each form renderer for its
files, refuses when two forms disagree about a shared file, runs the honesty lint over
the result, adds the explain page, the record itself and a manifest, and returns
`{relative path: text}`. `render_pipeline_dir` writes that under a directory the caller
names — `workspace.pipelines_dir()/<name>` for the MCP primitive — and refuses to
replace a directory whose files were edited since they were rendered.

The honesty lint — `check_rendered_commands`
-------------------------------------------
The page's footer claims the render is mechanical: every command a form carries is a
sealed how-to command with nothing but its placeholders rebound. The lint is what makes
that a checked claim rather than a sentence. For each form it extracts the commands the
rendered files will actually execute (the plain stage scripts, the Nextflow process
script blocks) and requires each to match its stage's template MODULO BINDING: the
template is cut at every binding unit — a `{PLACEHOLDER}`, or an artifact named inside
an output slot, `{OUT}/<name>` — and the literal text between the units must appear
verbatim, in order, with no command added or dropped. HOW a unit is spelled in a form
(`${GTF}`, `${params.stranded}`, `${meta.sample}.counts.tsv`, a bare `.`) is the form's
business and its own tests' job; that the sealed text around the units survived is
this module's. A rendered command that fails is refused, never written.

The directory
-------------
    pipeline.yaml       the record (the ONE input of every file beside it)
    pipeline.html       the explain page
    MANIFEST.sha256     `sha256sum -c`-checkable; every file but itself. A later render
                        into the same directory compares the files on disk against it
                        and refuses to overwrite an edit unless told to
    <form files>        see pipeline_render_plain / pipeline_render_nextflow
    runs/               created by the runners, never by this module, never listed
"""
from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Callable, Mapping, Optional

from agent.skills.pipeline_page_html import render_pipeline_page
from agent.skills.pipeline_record import (RECORD_FILENAME, FormT, PipelineDerivationError,
                                          PipelineRecord, PipelineStage, _TOKEN_CHARS,
                                          _PLACEHOLDER_RE, _collapse, record_yaml,
                                          render_samplesheet)
from agent.skills.pipeline_render_nextflow import render_nextflow
from agent.skills.pipeline_render_plain import _stage_base, render_plain

PAGE_FILENAME = "pipeline.html"
MANIFEST_FILENAME = "MANIFEST.sha256"
SAMPLESHEET_FILENAME = "samples.csv"

FORM_RENDERERS: dict[str, Callable[..., dict[str, str]]] = {
    "plain": render_plain,
    "nextflow": render_nextflow,
}

#: A manifest line, as `sha256sum` writes and reads it.
_MANIFEST_LINE_RE = re.compile(r"^([0-9a-f]{64})  (.+)$")
#: What a bound unit may render as: a shell variable, a Groovy expression, a bare `.`, a
#: file name — never whitespace and never a shell operator, so a command with something
#: appended after its last binding does not pass as "the binding spelled longer".
_BOUND_UNIT = r"([^\s|;&<>]+?)"
#: Stands in for the page's own hash while the page is rendered: the page lists every
#: file's byte size, the manifest lists the page's hash, and a fixed-width stand-in is
#: what lets both be true of the bytes finally written.
_PENDING_HASH = "0" * 64


class PipelineRenderError(PipelineDerivationError):
    """A refusal at render time: the forms disagree, a rendered command drifted from
    its sealed template, or the target directory holds edits. Same shape as a
    derivation refusal (`code`, `error`, `remedy`) so one wrapper handles both."""


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


def _plain_commands(files: Mapping[str, str], stage: PipelineStage) -> Optional[list[str]]:
    """The commands `stages/NN_<stage>.sh` executes: the script ends with them, one per
    line, after its header and the preconditions on artifacts from earlier stages."""
    text = files.get(f"stages/{_stage_base(stage)}.sh")
    if text is None:
        return None
    lines = [ln for ln in text.splitlines() if ln.strip()]
    n = len(stage.commands)
    return lines[-n:] if n else []


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
    "plain": _plain_commands,
    "nextflow": _nextflow_commands,
}


def check_rendered_commands(record: PipelineRecord, files: Mapping[str, str],
                            forms: tuple[str, ...]) -> None:
    """Refuse unless every command each form will execute is its stage's sealed template
    modulo binding, with none added and none dropped. `forms` names the forms whose
    files are present; a form's files missing entirely is a refusal too — a lint that
    finds nothing to check must not pass."""
    slots = list(record.output_slots)
    for form in forms:
        extract = _EXTRACTORS[form]
        for stage in record.stages:
            rendered = extract(files, stage)
            if rendered is None:
                raise PipelineRenderError(
                    "pipeline.render_drift",
                    f"form {form!r} rendered no file carrying stage {stage.name}'s commands",
                    "this is a renderer defect, not a record problem; report it")
            if len(rendered) != len(stage.commands):
                raise PipelineRenderError(
                    "pipeline.render_drift",
                    f"form {form!r}, stage {stage.name}: the rendered file executes "
                    f"{len(rendered)} command(s) but the sealed how-to has "
                    f"{len(stage.commands)} for this stage: {rendered!r}",
                    "this is a renderer defect, not a record problem; report it")
            for got, template in zip(rendered, stage.commands):
                if not command_matches_template(got, template, slots):
                    raise PipelineRenderError(
                        "pipeline.render_drift",
                        f"form {form!r}, stage {stage.name}: the rendered command is not "
                        f"the sealed template with only its placeholders rebound.\n"
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


def _manifest(files: Mapping[str, str], *, pending: tuple[str, ...] = ()) -> str:
    """`sha256sum` format over every file but the manifest itself, sorted by path.
    Files named in `pending` get the stand-in hash (same width as a real one)."""
    lines = []
    for path in sorted(files):
        if path == MANIFEST_FILENAME:
            continue
        digest = _PENDING_HASH if path in pending else _sha256_text(files[path])
        lines.append(f"{digest}  {path}")
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


def _resolve_forms(record: PipelineRecord, forms: Optional[tuple[str, ...]]) -> tuple[str, ...]:
    chosen = tuple(forms) if forms else tuple(record.forms)
    if not chosen:
        raise PipelineRenderError(
            "pipeline.no_forms", "no form to render: the record names none and the caller chose none",
            f"pass forms= naming one or more of {sorted(FORM_RENDERERS)}")
    unknown = [f for f in chosen if f not in FORM_RENDERERS]
    if unknown:
        raise PipelineRenderError(
            "pipeline.unknown_form", f"unknown form(s) {unknown}",
            f"forms must be among {sorted(FORM_RENDERERS)}")
    return tuple(dict.fromkeys(chosen))


def render_pipeline_files(record: PipelineRecord, *, forms: Optional[tuple[str, ...]] = None,
                          env: Optional[Mapping] = None) -> dict[str, str]:
    """Every file of the pipeline directory, `{relative path: text}`, linted. Pure.

    `forms` defaults to the record's own; `env` is a compute-env block from
    projects_access.yaml handed to each form renderer. A refusal from a form renderer
    (a `ValueError` naming its remedy) is re-raised as `PipelineRenderError` so the
    caller sees one exception family."""
    chosen = _resolve_forms(record, forms)
    files: dict[str, str] = {}
    for form in chosen:
        try:
            rendered = FORM_RENDERERS[form](record, env=dict(env) if env is not None else None)
        except PipelineDerivationError:
            raise
        except ValueError as e:
            raise PipelineRenderError(
                "pipeline.form_refused", f"the {form} form cannot carry this record: {e}",
                "change the record (stages=, resources=, per_sample=) or drop this form") from e
        for path, text in rendered.items():
            if path in files and files[path] != text:
                raise PipelineRenderError(
                    "pipeline.render_drift",
                    f"forms disagree about {path}: {form!r} renders it differently from an "
                    f"earlier form",
                    "this is a renderer defect, not a record problem; report it")
            files[path] = text
    for reserved in (RECORD_FILENAME, PAGE_FILENAME, MANIFEST_FILENAME):
        if reserved in files:
            raise PipelineRenderError(
                "pipeline.render_drift", f"a form rendered the reserved file {reserved}",
                "this is a renderer defect, not a record problem; report it")
    check_rendered_commands(record, files, chosen)

    files[RECORD_FILENAME] = record_yaml(record)
    # The page lists every file with its size, the manifest included; the manifest
    # lists the page's hash. Render the page over a manifest whose line for the page
    # is a fixed-width stand-in, then write the real manifest — same byte count.
    provisional = dict(files)
    provisional[PAGE_FILENAME] = ""
    provisional[MANIFEST_FILENAME] = _manifest(provisional, pending=(PAGE_FILENAME,))
    del provisional[PAGE_FILENAME]
    files[PAGE_FILENAME] = render_pipeline_page(record, provisional)
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
                        forms: Optional[tuple[str, ...]] = None,
                        env: Optional[Mapping] = None,
                        overwrite: bool = False) -> dict:
    """Render every form into `out_dir` and return what was written.

    An existing, non-empty `out_dir` is replaced only when it is a previous render of
    this system (it carries a manifest) whose files are unedited; otherwise the call
    refuses — a hand-edited samples.csv or params.env is the user's work — unless
    `overwrite=True`. Files of the previous render that this render does not produce
    are removed; anything the manifest never listed (`runs/`, the user's own files)
    is left alone. Every `*.sh` is made executable."""
    out_dir = Path(out_dir)
    files = render_pipeline_files(record, forms=forms, env=env)

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
        "forms": list(_resolve_forms(record, forms)),
        "page": str(out_dir / PAGE_FILENAME),
        "record": str(out_dir / RECORD_FILENAME),
        "manifest": str(out_dir / MANIFEST_FILENAME),
        "replaced_previous_render": bool(previous),
        "removed": removed,
    }


__all__ = ["PipelineRenderError", "FORM_RENDERERS", "PAGE_FILENAME", "MANIFEST_FILENAME",
           "command_matches_template", "check_rendered_commands", "parse_manifest",
           "render_pipeline_files", "render_pipeline_dir"]
