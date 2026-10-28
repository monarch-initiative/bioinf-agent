"""pipeline_proof — the render proves its own pipeline.

A sealed workflow proves that ONE run of the how-to worked, step by step. The pipeline is
that how-to over many samples as a Nextflow directory, and the directory the user plugs
in is the artifact that has to work. So a render ends with ONE run of it: the rendered
files, copied under the scratch zone, run with the runtime env's Nextflow over the sealed
samples (`samples.csv` as rendered, `params.yaml` as rendered, `-profile local`, docker,
the stages' own images), and the run's records (`runs/<stamp>/`: trace.txt, report.html,
timeline.html, nextflow.log) moved into the pipeline directory. The verdict is read off
Nextflow's exit status and its trace — every expected task completed with exit 0 — and
written into the record (`PipelineRecord.proof`), which the page renders.

What stops a proof before anything is written is said as a refusal: no local Nextflow
runtime, a stage image absent from the daemon, a sealed input path missing. A proof that
fails leaves no pipeline directory; its run stays under scratch for diagnosis.
"""
from __future__ import annotations

import csv
import os
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from agent.skills import workspace
from agent.skills.outcomes import refused
from agent.skills.pipeline_record import PipelineProof, PipelineRecord, ProofTask

#: How long a proof may run before it is killed and recorded as timed out.
PROOF_TIMEOUT_S_DEFAULT = 7200
#: The columns of `trace.txt` the proof reads (nextflow.config's TRACE_FIELDS writes them).
_TASK_FIELDS = ("name", "status", "exit", "realtime", "peak_rss", "container")


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def check_preconditions(record: PipelineRecord) -> Optional[dict]:
    """What would stop the proof, said before a file is written: the runtime, every
    stage's image in the local daemon, every sealed input on this machine. Returns the
    refusal (an outcome payload with its code and remedy) or None."""
    from agent.skills import run_production as rp
    rt = record.local_runtime
    if rt is None or not Path(rt.activate).is_file():
        return refused("pipeline.proof_no_runtime", success=False,
                       error="this machine has no local Nextflow runtime to prove the pipeline with",
                       remedy="./scripts/setup.sh builds the runtime env (nextflow with its own Java); then render again")
    if rt.nextflow is None or not Path(rt.nextflow).is_file():
        return refused("pipeline.proof_no_runtime", success=False,
                       error=f"the runtime env carries no nextflow binary beside {rt.activate}",
                       remedy="./scripts/setup.sh installs it; then render again")
    missing = [im["image"] for im in rp._stage_images(record)
               if not im["image"] or not rp._docker_image_present(im["image"])]
    if missing:
        return refused("pipeline.proof_image_missing", success=False,
                       error=f"{len(missing)} image(s) the stages run in are not in the local Docker daemon: {missing}",
                       missing_images=missing,
                       remedy="`docker load -i <workspace>/environments/<env>/<env>.tar`, or freeze the env again, "
                              "then render again: the proof runs the stages in these images")
    effective = {p.name.lower(): str(p.default) for p in record.params if p.kind == "shared" and p.default}
    try:
        shared, prefixes, files = rp._bound_paths(record, effective, record.samplesheet.rows)
        rp._check_paths_local(sorted((set(shared.values()) - set(prefixes)) | set(files)), prefixes)
    except rp._Refusal as r:
        res = dict(r.result)
        return refused("pipeline.proof_inputs_missing", success=False,
                       error=res.get("error", "a sealed input is not on this machine"),
                       missing_inputs=res.get("missing_inputs", []),
                       remedy="the proof runs over the sealed samples' own files; restore them (or re-seal on data "
                              "that is here) and render again")
    return None


def _expected_tasks(record: PipelineRecord) -> int:
    rows = len(record.samplesheet.rows)
    return sum(rows if s.scope == "per_sample" else 1 for s in record.stages)


def read_trace(path: Path) -> list[ProofTask]:
    """The tasks of a run, from Nextflow's tab-separated trace."""
    if not path.is_file():
        return []
    out: list[ProofTask] = []
    with path.open(newline="") as fh:
        for row in csv.DictReader(fh, delimiter="\t"):
            out.append(ProofTask(**{f: str(row.get(f) or "") for f in _TASK_FIELDS}))
    return out


def _verdict(rc: Optional[int], tasks: list[ProofTask], expected: int, timed_out: bool) -> tuple[str, str]:
    if timed_out:
        return "timed_out", f"Nextflow was still running at the proof's time limit; {len(tasks)} of {expected} task(s) had a trace line"
    bad = [t for t in tasks if t.status != "COMPLETED" or t.exit != "0"]
    if rc != 0:
        names = ", ".join(f"{t.name} ({t.status}, exit {t.exit})" for t in bad[:5])
        return "failed", (f"Nextflow exited {rc}" + (f"; failed task(s): {names}" if names else
                          "; no task failed, so the failure is in the workflow itself (read nextflow.log)"))
    if bad:
        names = ", ".join(f"{t.name} ({t.status}, exit {t.exit})" for t in bad[:5])
        return "failed", f"Nextflow exited 0 but {len(bad)} task(s) did not complete cleanly: {names}"
    if len(tasks) != expected:
        return "failed", f"the run finished with {len(tasks)} task(s); {expected} expected ({_tasks_word(expected)})"
    return "passed", f"every one of the {expected} task(s) completed with exit 0"


def _tasks_word(expected: int) -> str:
    return "one per per-sample stage and sample, one per cohort stage"


def run_proof(record: PipelineRecord, pipeline_dir: Path, *, files: list[str],
              timeout_s: int = PROOF_TIMEOUT_S_DEFAULT) -> PipelineProof:
    """Run the rendered directory once over the sealed samples and say what happened.

    `files` are the rendered files (relative paths) to carry into the proof directory —
    the manifest's set, so the proof runs exactly what was written. The run's `runs/<stamp>/`
    is moved into `pipeline_dir` whatever the verdict; the rest of the proof (work/,
    results/) stays under scratch."""
    stamp = _stamp()
    proof_dir = workspace.scratch_dir("pipeline_proofs", record.name, stamp)
    for rel in files:
        src = pipeline_dir / rel
        if src.is_file():
            dst = proof_dir / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
    rt = record.local_runtime
    activate = rt.activate if rt else ""
    nf = (f'nextflow -log {shlex.quote(f"runs/{stamp}/nextflow.log")} run main.nf -profile local '
          f'-params-file params.yaml --run_stamp {shlex.quote(stamp)}')
    launch = f"source {shlex.quote(activate)} && {nf}"
    env = dict(os.environ, NXF_HOME=str(proof_dir / ".nextflow_home"), NXF_ANSI_LOG="false")
    started = time.monotonic()
    timed_out = False
    rc: Optional[int] = None
    out = err = ""
    try:
        p = subprocess.run(["bash", "-c", launch], cwd=proof_dir, env=env, capture_output=True, text=True,
                           timeout=timeout_s)
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired as e:
        timed_out = True
        out, err = (e.stdout or b"").decode(errors="replace") if isinstance(e.stdout, bytes) else (e.stdout or ""), \
                   (e.stderr or b"").decode(errors="replace") if isinstance(e.stderr, bytes) else (e.stderr or "")
    duration = time.monotonic() - started
    run_rel = f"runs/{stamp}"
    src_run = proof_dir / run_rel
    if src_run.is_dir():
        dst_run = pipeline_dir / run_rel
        dst_run.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_run), str(dst_run))
    else:
        (pipeline_dir / run_rel).mkdir(parents=True, exist_ok=True)
    (pipeline_dir / run_rel / "proof.stdout").write_text(out[-20000:])
    (pipeline_dir / run_rel / "proof.stderr").write_text(err[-20000:])
    tasks = read_trace(pipeline_dir / run_rel / "trace.txt")
    expected = _expected_tasks(record)
    verdict, reason = _verdict(rc, tasks, expected, timed_out)
    return PipelineProof(
        verdict=verdict, reason=reason, stamp=stamp, run_dir=str(proof_dir), run_record=run_rel,
        launch=launch, samples=[r.get("sample", "") for r in record.samplesheet.rows],
        tasks_expected=expected, tasks=tasks, nextflow_exit=rc, duration_s=round(duration, 1))


__all__ = ["PROOF_TIMEOUT_S_DEFAULT", "check_preconditions", "read_trace", "run_proof"]
