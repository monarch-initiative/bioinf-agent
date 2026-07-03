"""run_tools — the "execute against test data" surface.

run_pipeline_step (host conda env, fast iteration) and run_step_in_container
(inside the frozen image, validated==shipped) are the two run primitives;
verify_installation captures a custom version/help command per package;
run_in_env is the raw shell-in-env escape hatch; validate_output is the
type-aware file validator used by all of them.

Helpers _infer_validator_type and _stamp_i7_authority are private to this
surface — they sit here next to their only callers.
"""
from __future__ import annotations

from pathlib import Path

# IMPORT-BINDING: see workflow_tools.py — singletons go through `_ms.X`
# so test monkeypatching on mcp_server reaches us.
from agent import mcp_server as _ms
from agent.mcp_server import mcp  # FastMCP app, never monkeypatched
from agent.skills.outcomes import proven, refused, broke


def _infer_validator_type(basename: str, ext: str) -> str:
    """Pure-function extension → validator type. No memorization beyond
    obvious filetype names that the OutputValidator already handles."""
    ext = ext.lstrip(".").lower()
    # Strip .gz: validators handle compressed forms natively.
    if ext.endswith(".gz"):
        ext = ext[:-3]
    # Final extension is usually authoritative.
    last = ext.split(".")[-1] if ext else ""
    # Aliases — map common shorthands to canonical validator types.
    aliases = {"fq": "fastq", "fa": "fasta", "fna": "fasta", "faa": "fasta", "ndjson": "jsonl"}
    if last in aliases:
        return aliases[last]
    # Validator's internal dispatch already keys off these names; mirror it.
    for known in ("bam", "sam", "bai", "vcf", "bcf", "fasta", "fastq",
                  "bed", "bigwig", "gtf", "gff", "gfa", "tsv", "csv", "txt",
                  "html", "json", "jsonl"):
        if last == known:
            return known
    return "any"


def _stamp_i7_authority(resource_usage, platform: str):
    """Mark whether captured I7 resource numbers (wall, peak RSS, peak CPU) are
    authoritative. They are real only when the step ran on a NATIVE locus; under
    emulation (qemu/Rosetta) they are emulator artefacts. This does NOT change the
    step's pass/fail or the I7 invariant (the measurement still happened) — it stops
    the spec/guide from presenting emulated timings as ship-architecture truth."""
    if isinstance(resource_usage, dict):
        loc = _ms._locus.detect_locus(platform)
        resource_usage["i7_authoritative"] = loc["i7_authoritative"]
        resource_usage["locus"] = loc["locus"]
    return resource_usage


@mcp.tool()
def run_pipeline_step(
    env_name: str,
    command: str,
    pipeline_id: str,
    inputs: list = [],
    output_types: dict = {},
    watch_dir: str = "",
    tool: str = "",
    subcommand: str = "",
    purpose: str = "",
    step: int = 0,
    timeout_seconds: int = 1800,
) -> dict:
    """Run a pipeline step and auto-validate every produced output in one call.

    Replaces the three-call dance (run_in_env → inspect detected_outputs →
    validate_output(files=[...])) with one. Internally:
      1. Runs the command via run_in_env (with pipeline_id-merge into pipeline_steps)
      2. Validates every detected_output against an inferred or supplied type
      3. Validations are merged into the same pipeline_step automatically

    output_types: optional dict mapping {basename | extension | absolute path}
                  to expected_type (e.g. `{".vcf.gz": "vcf", ".bam": "bam",
                  "/tmp/report.html": "html"}`). Lookup order on each detected
                  output: absolute resolved path → as-detected path → basename
                  → ".ext" → "ext" → extension inference. Pre-N8 only the
                  basename/extension keys worked — passing a full path
                  silently fell through to inference (which yields
                  expected_type='any', an I3 violation at seal time). Unmatched
                  output_types keys are reported back in the response as
                  `output_types_unmatched` so a typo doesn't go silent.

    pipeline_id is required (this primitive's purpose is the merged flow).
    """
    if not pipeline_id:
        return refused("run_pipeline_step.pipeline_id_required",
                       error="pipeline_id is required for run_pipeline_step")

    result = _ms._env_mgr.run_in_env(
        env_name, command, timeout=timeout_seconds, inputs=inputs,
        watch_dir=watch_dir or None,
    )
    # L11 (universal lineage): hash each detected_output at production time
    # so seal can verify the same path holds the same bytes through to its
    # downstream consumer. Cheap (one sha256 per produced file), file-type-
    # agnostic, and the producer's own bytes — agent can't substitute.
    output_sha256 = _ms._env_mgr.hash_outputs(result.get("detected_outputs", []))
    step_data = {
        "tool":            tool or (command.split() or [""])[0],
        "subcommand":      subcommand or None,
        "purpose":         purpose or None,
        "command":         command,
        "returncode":      result.get("returncode"),
        "runtime_seconds": result.get("runtime_seconds"),
        "resource_usage":  result.get("resource_usage"),
        "inputs":          result.get("inputs", []),
        "detected_outputs": result.get("detected_outputs", []),
        "output_sha256":   output_sha256 or None,
    }
    step_data = {k: v for k, v in step_data.items() if v is not None}
    idx = _ms._pipeline_state.add_step(pipeline_id, step_data, replace_step=step)

    # Auto-validate every detected output if the run succeeded.
    # N8 (batch-3): track which output_types keys were consumed so we can
    # surface unmatched keys back to the caller — a typo or a path that
    # didn't actually get produced should not silently fall through to
    # _infer_validator_type (which returns "any" → I3 violation at seal).
    output_types_used: set[str] = set()
    validations: dict = {}
    if result.get("returncode") == 0 and idx is not None:
        for path in result.get("detected_outputs", []):
            basename = Path(path).name
            ext = "".join(Path(path).suffixes).lower()
            # Resolve expected type, in order of specificity:
            #   1. absolute resolved path  (N8: full-path keys now work)
            #   2. as-detected path        (path-as-it-came, in case agent
            #                               keyed off a non-canonical form)
            #   3. basename
            #   4. ".ext"  (with leading dot)
            #   5. "ext"   (without leading dot)
            #   6. extension inference     (fallback — yields "any" if no
            #                               recognized extension)
            try:
                resolved_path = str(Path(path).resolve())
            except Exception:
                resolved_path = path
            etype = None
            for key in (resolved_path, path, basename, ext, ext.lstrip(".")):
                if key and key in output_types:
                    etype = output_types[key]
                    output_types_used.add(key)
                    break
            if etype is None:
                etype = _infer_validator_type(basename, ext)
            v = _ms._validator.validate(path, etype, env_name=env_name)
            validations[basename] = v
            _ms._pipeline_state.add_validation(pipeline_id, idx, basename, v)

    unmatched = sorted(set(output_types) - output_types_used)
    out = {
        **result,
        "pipeline_merge":  {"status": "merged", "pipeline_id": pipeline_id, "step_index": idx},
        "validations":     validations,
        "validation_count": len(validations),
    }
    if unmatched:
        # Surface but don't fail the call — the validation outcome is still
        # produced; the caller just needs to know their output_types key
        # didn't bind to anything (typo, wrong extension, file not produced).
        out["output_types_unmatched"] = unmatched
    return out


@mcp.tool()
def run_step_in_container(
    freeze_request_key: str,
    command: str,
    pipeline_id: str,
    inputs: list = [],
    output_types: dict = {},
    data_dir: str = "",
    watch_dir: str = "",
    extra_mounts: list = [],
    tool: str = "",
    subcommand: str = "",
    purpose: str = "",
    step: int = 0,
    timeout_seconds: int = 1800,
    platform: str = "linux/amd64",
) -> dict:
    """Run a pipeline step INSIDE the frozen env image and auto-validate every
    produced output — the validation-locus pivot: **the artifact you ship is the
    artifact you validate.** Use this instead of run_pipeline_step once the env is
    frozen, so the recorded run is the one that actually executes on HPC.

    Resolves the image from the EnvCache by `freeze_request_key` (call freeze()
    first), pulling an adopted-by-digest image local if needed. The data dir is
    bind-mounted at its OWN host path inside the container, so the same absolute
    paths in `command`/`inputs` work unchanged and outputs land back on the host
    for validation. Resource usage (I7) is captured IN the container (GNU time →
    exact peak RSS, falling back to host docker-stats sampling). The step is
    stamped ran_in_container + container_image[_digest] so seal can assert
    validated==shipped.

    output_types: {basename|ext: validator_type}. inputs: paths (or {path,…}).
    extra_mounts: ["host:container", …] for data outside data_dir."""
    if not pipeline_id:
        return refused("run_container.pipeline_id_required",
                       error="pipeline_id is required for run_step_in_container")
    rec = _ms._env_cache.lookup(freeze_request_key)
    if not rec:
        return refused("run_container.no_frozen_env",
                       error=f"no frozen env for '{freeze_request_key}' — run freeze() first")
    image = rec.get("image")
    if not image:
        return refused("run_container.no_image_handle",
                       error=f"freeze record for '{freeze_request_key}' has no image handle")
    if _ms._locus.daemon_is_remote():
        # This step bind-mounts LOCAL test data; a remote daemon (DOCKER_HOST) can't
        # see local paths, so outputs would never land back here for validation.
        # (Layer-1 freeze() build+validation IS daemon-agnostic and runs natively on
        # a remote amd64 host — only these Layer-2 DATA steps need the daemon local.)
        return refused("run_container.remote_daemon",
                error="run_step_in_container bind-mounts local test data, but the active "
                "Docker daemon is REMOTE (DOCKER_HOST). It cannot see local paths. Use a local "
                "daemon for Layer-2 data steps, or stage the data on the remote host and pass "
                "data_dir as its path there. Note: Layer-1 freeze() validation runs natively on "
                "a remote amd64 host with no change.")
    # An adopted biocontainer is referenced by digest — pull it local so it can run.
    if _ms._docker._run(["docker", "image", "inspect", image])["returncode"] != 0:
        pull = _ms._docker._run(["docker", "pull", "--platform", platform, image], timeout=900)
        if pull["returncode"] != 0:
            return broke("run_container.image_pull_failed",
                         error=f"could not pull image {image}: {(pull['stderr'] or '')[-300:]}")

    ddir = (Path(data_dir) if data_dir else (_ms._env_mgr.project_root / "data")).resolve()
    mounts = [(str(ddir), str(ddir))]   # same-path mount → host abs paths work verbatim
    for m in extra_mounts:
        if isinstance(m, str) and ":" in m:
            h, c = m.split(":", 1)
            mounts.append((h, c))

    wdir = (Path(watch_dir).resolve() if watch_dir else ddir)

    def _snap() -> dict:
        snap = {}
        for p in wdir.rglob("*"):
            if p.is_file():
                try:
                    stt = p.stat()
                    snap[str(p)] = (stt.st_mtime_ns, stt.st_size)
                except OSError:
                    pass
        return snap

    before = _snap()
    res = _ms._docker.run_in_container(image, command, mounts=mounts, workdir=str(ddir),
                                       platform=platform, timeout=timeout_seconds)
    # I7 numbers measured under emulation are emulator artefacts, not real — stamp
    # whether they're authoritative (native locus) so the spec/guide never presents
    # emulated timings as if they were measured on the ship architecture.
    res["resource_usage"] = _stamp_i7_authority(res.get("resource_usage"), platform)
    after = _snap()
    detected = sorted(p for p, sig in after.items() if before.get(p) != sig)

    norm_inputs = [{"path": i, "references": []} if isinstance(i, str) else i for i in inputs]
    # L11 (universal lineage): hash detected_outputs at production time so
    # seal can verify same-path-same-bytes against downstream consumers.
    output_sha256 = _ms._env_mgr.hash_outputs(detected)
    step_data = {
        "tool":            tool or (command.split() or [""])[0],
        "subcommand":      subcommand or None,
        "purpose":         purpose or None,
        "command":         command,
        "returncode":      res.get("returncode"),
        "resource_usage":  res.get("resource_usage"),
        "inputs":          norm_inputs,
        "detected_outputs": detected,
        "output_sha256":   output_sha256 or None,
        "ran_in_container": True,
        "container_image":  image,
        "container_image_digest": rec.get("image_digest"),
    }
    step_data = {k: v for k, v in step_data.items() if v is not None}
    idx = _ms._pipeline_state.add_step(pipeline_id, step_data, replace_step=step)

    validations: dict = {}
    if res.get("returncode") == 0 and idx is not None:
        for path in detected:
            basename = Path(path).name
            ext = "".join(Path(path).suffixes).lower()
            etype = (output_types.get(basename) or output_types.get(ext)
                     or output_types.get(ext.lstrip(".")) or _infer_validator_type(basename, ext))
            v = _ms._validator.validate(path, etype)
            validations[basename] = v
            _ms._pipeline_state.add_validation(pipeline_id, idx, basename, v)

    return {
        **res,
        "detected_outputs":  detected,
        "validated_in_image": image,
        "pipeline_merge":    {"status": "merged", "pipeline_id": pipeline_id, "step_index": idx},
        "validations":       validations,
        "validation_count":  len(validations),
    }


@mcp.tool()
def verify_installation(
    env_name: str,
    package_name: str,
    check_command: str,
    pipeline_id: str = "",
) -> dict:
    """Run a custom version/help command inside the env to confirm a package
    installed correctly.

    Advisory: env_status no longer requires every package to have a verify
    record — `conda list --json` plus successful install_steps are the
    structural truth. Use this when you want to capture a custom check
    (e.g. `samtools --version` for a CLI tool) so it appears in the report
    next to that package.

    If pipeline_id is supplied, the result is cached in
    draft.verifications[package_name] and stitched onto the derived package
    record at finalize-time."""
    result = _ms._env_mgr.verify(env_name, package_name, check_command)
    if pipeline_id:
        ok = _ms._pipeline_state.cache_verification(pipeline_id, package_name, {
            "verify_command": check_command,
            "verify_output":  result.get("output", ""),
        })
        result["pipeline_merge"] = (
            {"status": "cached", "pipeline_id": pipeline_id, "name": package_name}
            if ok else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def run_in_env(
    env_name: str,
    command: str,
    working_dir: str = "",
    timeout_seconds: int = 1800,
    inputs: list = [],
    watch_dir: str = "",
    pipeline_id: str = "",
    step: int = 0,
    tool: str = "",
    subcommand: str = "",
    purpose: str = "",
) -> dict:
    """Run an arbitrary shell command inside a conda environment. Always use absolute paths.

    inputs: list of files this step consumes. Each entry is either:
            - a plain string path: "/abs/path/to/file"
            - a structured dict:   {path: "/abs/path/to/run.R",
                                    references: ["/abs/path/data1.tsv", ...]}
              Use the dict form when an input is a script / config / wrapper that
              opens other files at runtime — the references become an indented
              sublist under that input in the HTML report and are kept in the
              spec so the lineage is programmatic (not memory-bound).
    watch_dir: directory to snapshot before/after execution. New and modified files
               are returned as detected_outputs. Defaults to working_dir if omitted.

    If pipeline_id is supplied, a PipelineStep entry is appended to
    draft.pipeline_steps with the command, returncode, runtime, inputs, and
    detected outputs. Pass `step=N` to replace step N (for retries); default
    is append. The returned `pipeline_merge.step_index` is what you pass to
    validate_output(step=...) to attach validations to this step.

    Return keys: returncode, stdout, stderr, success, command, runtime_seconds,
                 inputs, detected_outputs, [pipeline_merge]."""
    result = _ms._env_mgr.run_in_env(
        env_name, command,
        working_dir=working_dir or None,
        timeout=timeout_seconds,
        inputs=inputs,
        watch_dir=watch_dir or working_dir or None,
    )
    if pipeline_id:
        step_data = {
            "tool":            tool or (command.split() or [""])[0],
            "subcommand":      subcommand or None,
            "purpose":         purpose or None,
            "command":         command,
            "returncode":      result.get("returncode"),
            "runtime_seconds": result.get("runtime_seconds"),
            "resource_usage":  result.get("resource_usage"),
            "inputs":          result.get("inputs", []),
            "outputs":         result.get("detected_outputs", []),
        }
        step_data = {k: v for k, v in step_data.items() if v is not None}
        idx = _ms._pipeline_state.add_step(pipeline_id, step_data, replace_step=step)
        result["pipeline_merge"] = (
            {"status": "merged", "pipeline_id": pipeline_id, "step_index": idx}
            if idx is not None else
            {"status": "unknown_pipeline_id", "pipeline_id": pipeline_id}
        )
    return result


@mcp.tool()
def validate_output(
    file_path: str = "",
    expected_type: str = "",
    files: list[dict] = [],
    env_name: str = "",
    pipeline_id: str = "",
    step: int = 0,
    allow_empty: bool = False,
) -> dict:
    """Validate one or many bioinformatics output files are non-empty and parseable.

    expected_type: sam | bam | fastq | fasta | vcf | bcf | bed | bigwig |
                   bim | fam | ld | frq | prune | tsv | csv | txt | log | any

    Two call shapes:
      Single:  validate_output(file_path=..., expected_type=...)
      Batch:   validate_output(files=[{path, expected_type, allow_empty?}, ...])

    In batch mode each file is validated independently — one failure never
    aborts the others. Returns per-file results in `validations` keyed by
    basename, plus aggregate `all_passed` / `passed_count` / `failed_count`.

    `allow_empty` (single-call) or `allow_empty: true` per-entry (batch):
    treat an empty file as passing instead of failing. Use for tools whose
    success signal is an empty `.err` / `.log` (OpenCRAVAT's stderr file is
    the canonical case).

    If pipeline_id+step are supplied, every result is merged into
    draft.pipeline_steps[step].validation[basename] — the step's
    validation_status (I3) is derived from the aggregate at seal time."""
    # Batch path
    if files:
        validations: dict = {}
        passed_count = failed_count = 0
        merge_status = "merged" if pipeline_id and step > 0 else None
        for i, entry in enumerate(files):
            if not isinstance(entry, dict):
                # A malformed batch entry (None / string / …) must not crash the
                # whole batch — record it as a failed validation and continue (C4).
                validations[f"<invalid entry #{i}>"] = refused(
                    "validate.malformed_entry", passed=False,
                    error=f"files[] entries must be dicts with a 'path'; "
                          f"entry {i} is {type(entry).__name__}")
                failed_count += 1
                continue
            path = entry.get("path", "")
            etype = entry.get("expected_type", "any")
            entry_allow_empty = bool(entry.get("allow_empty", False))
            vr = _ms._validator.validate(
                path, etype, env_name=env_name or None,
                allow_empty=entry_allow_empty,
            )
            filename = Path(path).name
            validations[filename] = vr
            if vr.get("passed") is True:
                passed_count += 1
            elif vr.get("passed") is False:
                failed_count += 1
            if merge_status == "merged":
                ok = _ms._pipeline_state.add_validation(pipeline_id, step, filename, vr)
                if not ok:
                    merge_status = "step_not_found"
        out: dict = {
            "validations":   validations,
            "all_passed":    failed_count == 0 and passed_count == len(files),
            "passed_count":  passed_count,
            "failed_count":  failed_count,
            "total":         len(files),
        }
        if pipeline_id:
            if step <= 0:
                out["pipeline_merge"] = {"status": "step_required", "pipeline_id": pipeline_id}
            else:
                out["pipeline_merge"] = {
                    "status": merge_status, "pipeline_id": pipeline_id, "step": step,
                    "merged_files": passed_count + failed_count,
                }
        return out

    # Single-file path (unchanged behavior)
    result = _ms._validator.validate(
        file_path, expected_type, env_name=env_name or None,
        allow_empty=allow_empty,
    )
    if pipeline_id:
        if step <= 0:
            result["pipeline_merge"] = {"status": "step_required", "pipeline_id": pipeline_id}
        else:
            filename = Path(file_path).name
            ok = _ms._pipeline_state.add_validation(pipeline_id, step, filename, result)
            result["pipeline_merge"] = (
                {"status": "merged", "pipeline_id": pipeline_id,
                 "step": step, "filename": filename}
                if ok else
                {"status": "step_not_found", "pipeline_id": pipeline_id, "step": step}
            )
    return result
