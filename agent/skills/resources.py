"""
Resource listing utilities — manifests and pipeline specs.

Read the on-disk genome / test-data manifests, and the two layers of built
artifacts (frozen envs from the EnvCache + sealed WorkflowSpecs), into
structured dicts for the MCP tools list_available_resources and
list_installed_pipelines.
"""

from pathlib import Path

from agent.models import core_data
from agent.models.core_data import (USAGE_NOT_ATTEMPTED, USAGE_VERIFIED,
                                    record_is_gated as _record_is_gated,
                                    step_is_validated, usage_status)
from agent.skills.env_honesty import ASSURANCE as _ASSURANCE
from agent.skills.spec_writer import (TEST_DATA_NOT_ATTEMPTED, TEST_DATA_UNANCHORED,
                                      TEST_DATA_VERIFIED)

import yaml



def list_resources(inputs: dict, config: dict) -> dict:
    """
    Read core_test_data manifests and return structured resource info.

    inputs["resource_type"]: "genomes" | "test_data" | "both"
    config["paths"]["data_dir"]: root data directory (relative or absolute)
    """
    resource_type = inputs["resource_type"]
    result: dict = {}
    data_dir = Path(config["paths"]["data_dir"])

    genomes = []
    test_data = []

    for core_dir in sorted(data_dir.glob("core_test_data_*")):
        manifest_path = core_dir / "manifest.yaml"
        if not manifest_path.exists():
            continue
        with open(manifest_path) as f:
            m = yaml.safe_load(f) or {}

        build = m.get("genome_build", core_dir.name.replace("core_test_data_", ""))
        chrom = m.get("chromosome_subset", "")

        if resource_type in ("genomes", "both"):
            ginfo = m.get("genome") or {}
            # A `genome:` block with no `fasta:` declares nothing — and `core_dir / ""`
            # is the core dir itself, which exists, so an entry built from it would
            # report `available: True` for a genome that is not there.
            if ginfo.get("fasta"):
                fasta = core_dir / ginfo["fasta"]
                fai = core_dir / ginfo["fai"] if ginfo.get("fai") else None
                genomes.append({
                    "id": f"{build}_{chrom}" if chrom else build,
                    "build": build,
                    "chromosome_subset": chrom,
                    "fasta": str(fasta),
                    # The FASTA's own index, carried because `select_test_data` declares
                    # BOTH into `test_data` so I8 can trace and re-anchor them. Absent
                    # from the manifest ⇒ None, never a path we made up.
                    "fai": str(fai) if fai else None,
                    "available": fasta.exists(),
                    "indexes": list(ginfo.get("indexes", {}).keys()),
                    "core_dir": str(core_dir),
                })

        if resource_type in ("test_data", "both"):
            # Sequencing data. The directory tree (short_read/{end_or_platform}/{assay_type}/)
            # is just for organization — the authoritative read_type/end_type/assay_type/
            # platform values live on each per-sample record.  Long-read entries use the
            # platform name (pacbio/ont) in the directory slot where short-read uses
            # end_type, so reading from the per-sample record is the only correct path.
            for _read_type_key, level2 in m.get("sequencing_data", {}).items():
                if not isinstance(level2, dict):
                    continue
                for _level2_key, level3 in level2.items():
                    if not isinstance(level3, dict):
                        continue
                    for _level3_key, samples in level3.items():
                        if not isinstance(samples, list):
                            continue
                        for smp in samples:
                            for subset, sinfo in smp.get("subsets", {}).items():
                                if not isinstance(sinfo, dict):
                                    continue
                                r1 = core_dir / sinfo["r1"] if sinfo.get("r1") else None
                                test_data.append({
                                    "id": f"{build}_{smp.get('assay_type','')}_{smp.get('accession', '')}_{subset}",
                                    "genome_build": build,
                                    "read_type": smp.get("read_type", ""),
                                    "end_type":  smp.get("end_type", ""),
                                    "assay_type": smp.get("assay_type", ""),
                                    "platform":  smp.get("platform", ""),
                                    "sample": smp.get("sample", ""),
                                    "accession": smp.get("accession", ""),
                                    "subset": subset,
                                    "num_reads": sinfo.get("num_reads"),
                                    "available": sinfo.get("available", False) and (r1.exists() if r1 else False),
                                    "r1": str(core_dir / sinfo["r1"]) if sinfo.get("r1") else None,
                                    "r2": str(core_dir / sinfo["r2"]) if sinfo.get("r2") else None,
                                    # Pod5 / nanopore raw-signal metadata (defaults to "fastq" /
                                    # None on conventional entries). Surfaced so select_test_data
                                    # + downstream basecaller pipelines can route by file_format
                                    # / chemistry / suggested_model.
                                    "file_format":     smp.get("file_format", "fastq"),
                                    "chemistry":       smp.get("chemistry"),
                                    "flowcell":        smp.get("flowcell"),
                                    "kit":             smp.get("kit"),
                                    "suggested_model": smp.get("suggested_model"),
                                    "core_dir": str(core_dir),
                                })

            # Phenopackets
            for pk in m.get("phenopackets", []):
                if not isinstance(pk, dict):
                    continue
                test_data.append({
                    "id":              f"{build}_phenopacket_{pk.get('phenopacket_id', '')}",
                    "genome_build":    build,
                    "type":            "phenopacket",
                    "phenopacket_id":  pk.get("phenopacket_id", ""),
                    "subject_id":      pk.get("subject_id", ""),
                    "sex":             pk.get("sex"),
                    "genes":           pk.get("genes", []),
                    "diseases":        pk.get("diseases", []),
                    "hpo_terms":       pk.get("hpo_terms", []),
                    "variants":        pk.get("variants", []),
                    "genome_assembly": pk.get("genome_assembly", ""),
                    "available":       pk.get("available", False),
                    "file":            str(core_dir / pk["file"]) if pk.get("file") else None,
                    "source_url":      pk.get("source_url", ""),
                    "core_dir":        str(core_dir),
                })

            # Pipeline outputs
            for pipeline_name, pout in m.get("pipeline_outputs", {}).items():
                if not isinstance(pout, dict):
                    continue
                for sample_key, sout in pout.get("samples", {}).items():
                    if not isinstance(sout, dict):
                        continue
                    files = [
                        {
                            "path": str(core_dir / f["path"]),
                            "type": f.get("type"),
                            "exists": (core_dir / f["path"]).exists(),
                        }
                        for f in sout.get("files", [])
                    ]
                    test_data.append({
                        "id": f"{build}_pipeline_output_{pipeline_name}_{sample_key}",
                        "genome_build": build,
                        "type": "pipeline_output",
                        "pipeline": pipeline_name,
                        "sample": sample_key,
                        "upstream_pipelines": pout.get("upstream_pipelines", []),
                        "available": pout.get("available", False),
                        "files": files,
                        "provenance": str(core_dir / sout["provenance"]) if sout.get("provenance") else None,
                        "core_dir": str(core_dir),
                    })

    if resource_type in ("genomes", "both"):
        result["genomes"] = genomes
    if resource_type in ("test_data", "both"):
        result["test_data"] = test_data

    return result


# ---------------------------------------------------------------------------
# The core genome reference
# ---------------------------------------------------------------------------
# `test_data` has carried `reference_fasta` (and `fai`) in TEST_DATA_PATH_KEYS since
# I8 learned to trace external sources, and NOTHING wrote them: `select_test_data` is
# the sole producer of that block (it is not patchable, so anchors cannot be authored)
# and it emitted only r1/r2/core_data_dir. Meanwhile the one patchable alternative
# disclaims the job — `ReferenceDatabase` is for data "beyond the genome FASTA".
#
# So the most common shape in the field — align reads to a reference — had no correct
# way to declare its reference at all, and every such workflow bought a refused seal
# (`I8.composition_coherence`, "input '…/genome/chr22.fa' has no producing source")
# followed by a hand-authored declaration into a field whose own model says it is the
# wrong field. The reference in question is the one this system bootstraps onto the
# disk itself (`scripts/setup_core_test_data.sh`). Closed by giving the slot its
# producer, not by adding a fifth external kind.

#: What `genome_reference_for` found. THREE states, never two: "the manifest declares a
#: genome and the bytes are not there" and "this core dir ships no genome at all" are
#: different facts with different remedies (run the bootstrap script vs. bring your own
#: reference), and neither is a recording. A caller that only ever sees paths-or-nothing
#: cannot tell them apart, and would read an empty result as "there is no genome here".
GENOME_RECORDED = "recorded"
GENOME_DECLARED_ABSENT = "declared_but_not_on_disk"
GENOME_NONE_DECLARED = "none_declared"


def genome_reference_for(core_dir: str, genomes: list) -> dict:
    """THE reader for "which paths declare the core genome of `core_dir`, and are they
    on disk?" — the producer half of `test_data.reference_fasta` / `.fai`.

    Returns `{state, paths: {test_data_key: path}, declared: {test_data_key: path},
    detail}`. `declared` is what the manifest says exists; `paths` is the subset that
    really is on disk, and is what the caller writes into `test_data` so
    `select_test_data`'s anchor loop pins it. A declared-but-missing file gets NO entry
    in `paths`: recording it would refuse the seal of every workflow that never touched
    the genome, and fabricating an anchor for it is the failure this codebase names
    laundering. The absence is STATED in `state`/`detail` instead.

    SCOPE, stated because a producer silent about what it leaves out reads as complete:
    the FASTA and its `.fai` only, never the aligner index families the manifest also
    lists under `indexes:`. Those are DERIVED artifacts whose prefix IS the FASTA path
    already recorded, and anchoring them would turn a workflow that legitimately
    re-runs `bwa index` on the core genome into an `I8.test_data_mutated` refusal —
    a false accusation against a step that did exactly the right thing."""
    entry = None
    for g in genomes or []:
        if isinstance(g, dict) and g.get("core_dir") and str(g["core_dir"]) == str(core_dir):
            entry = g
            break
    if entry is None:
        return {"state": GENOME_NONE_DECLARED, "paths": {}, "declared": {},
                "detail": (f"no `genome:` block with a fasta in {core_dir}/manifest.yaml — "
                           f"this dataset ships reads and no reference. Declare the "
                           f"reference you align against with download_reference_database "
                           f"(or stage_authored_artifact if you built it here), or nothing "
                           f"will trace it at seal.")}

    declared = {k: v for k, v in (("reference_fasta", entry.get("fasta")),
                                  ("fai", entry.get("fai"))) if v}
    # Resolved through the SAME leaf every other reader of a `test_data` path uses —
    # the manifest's paths are relative to the project root in the shipped config, and
    # a CWD-relative existence check is how a reader ends up seeing none of them.
    paths = {k: v for k, v in declared.items()
             if core_data.resolve_data_path(v).exists()}
    if "reference_fasta" not in paths:
        return {"state": GENOME_DECLARED_ABSENT, "paths": {}, "declared": declared,
                "detail": (f"{core_dir}/manifest.yaml declares a genome FASTA at "
                           f"{declared.get('reference_fasta')} and it is not on disk, so "
                           f"nothing was recorded — an input pinned to bytes that are gone "
                           f"proves nothing. Fetch it with "
                           f"`scripts/setup_core_test_data.sh` and re-run select_test_data.")}
    missing = sorted(set(declared) - set(paths))
    return {"state": GENOME_RECORDED, "paths": paths, "declared": declared,
            "detail": ("recorded " + ", ".join(sorted(paths))
                       + (f"; declared but not on disk: {', '.join(missing)}" if missing else ""))}


def _semantic_versions(record: dict) -> list[dict]:
    """The REQUESTED tools with human-readable semantic versions — the string a user
    cites in a paper. Never image/build hashes.

    Each entry: `{tool, requested, installed, version, diverges}`.
      - `installed` is OBSERVED (via `_resolved_version` — the shared definition the
        ENV report also uses), or None = unrecorded.
      - `requested` is what the user asked for (or None if unpinned).
      - `version` == `installed`, kept for back-compat with readers of the old shape.
      - `diverges` flags requested ≠ installed (the shared divergence check, W5).

    This function originally forked `_resolved_version` and read only its first rung
    (the SBOM), so `list_installed_pipelines` reported `bcftools: null` for the
    authors'-image env while the ENV report claimed `1.23.1` — one fact, two readings,
    disagreeing. The SBOM cannot see a tool installed outside the package manager (a
    source-compiled binary carries no metadata anywhere), so a SBOM-only read is
    structurally blind on exactly the long-tail tiers this project prefers. Rule 4:
    one definition, read at every use.

    `installed` must NEVER fall back to the requested/pinned version (audit 2026-07-19,
    W6): the old `resolved or pinned or None` printed the REQUESTED version under the
    key a reader treats as installed — the same silent lie the ENV report carried. When
    nothing observed the real thing, `installed` is None; absence is a fact about our
    record, not a version to fabricate. The requested value lives in its OWN key.
    """
    from agent.skills.env_report_helpers import (
        _pkg_index, _resolved_version, _verif_index, requested_versions,
        versions_diverge)

    pidx = _pkg_index(record.get("resolved_packages") or [])
    vidx = _verif_index(record.get("verifications") or [])
    shipped = record.get("shipped_binaries") or []
    req = requested_versions(record)
    out = []
    for spec in (record.get("requested_tools") or []):
        name = str(spec).split("=")[0].strip()
        if not name:
            continue
        requested = (req.get(name) or req.get(str(spec).strip())
                     or (str(spec).split("=", 1)[1].strip() if "=" in str(spec) else ""))
        installed = _resolved_version(name, pidx.get(name.lower()),
                                      vidx.get(name.lower()), shipped) or None
        out.append({
            "tool": name,
            "requested": requested or None,
            "installed": installed,
            "version": installed,          # back-compat alias for `installed`
            "diverges": versions_diverge(requested, installed or ""),
        })
    return out


def _compact_tools(tools: list[dict]) -> list:
    """The tools of one env, as short as honesty allows.

    `{tool, requested, installed, version, diverges}` per tool is 117 tokens for a
    5-tool env, and `version` is a pure back-compat alias of `installed`. In the common
    case (nothing diverged, or nothing observed) the whole row collapses to one string.

    The DIVERGENCE never collapses. When requested and installed disagree, the full
    record is emitted — that flag is the whole point of the row, and shrinking a payload
    by dropping the one field that carries a warning would be the exact substitution
    (a request presented as an observation) the report-honesty work exists to prevent.
    """
    out: list = []
    for t in tools:
        if t.get("diverges"):
            out.append(t)                                  # a warning is never compacted
        elif t.get("installed"):
            out.append(f"{t['tool']} {t['installed']}")
        else:
            # absence is a FACT about our record, stated — never a fabricated version
            out.append(f"{t['tool']} (version unrecorded)")
    return out


def _test_data_status(spec: dict) -> str:
    """Three-state read of a sealed spec's input pin, for BOTH inventory forms.

    An absent `test_data_integrity` means the artifact predates anchoring, which is
    `unanchored` — not `verified`, and not "nothing to say" either when the spec does
    declare input paths. Rounding either way would be the inventory asserting a verdict
    its producer never made. Written once because the compact and detail rows had it
    twice within minutes, and they already disagreed: the compact one keyed off the
    stored field alone, so both legacy specs on disk showed no warning at all.

    NOT re-derived from disk. `verify_test_data` stats and hashes every declared input,
    and an inventory listing must stay a cheap read — the same reason the how-to status
    beside it is disclosed rather than re-earned."""
    if not core_data.test_data_paths(spec.get("test_data")):
        return TEST_DATA_NOT_ATTEMPTED
    return ((spec.get("test_data_integrity") or {}).get("status")
            or TEST_DATA_UNANCHORED)


def list_pipelines(config: dict, env_cache=None, detail: bool = False) -> dict:
    """Inventory of what has ALREADY been built, in both layers.

    COMPACT BY DEFAULT (`detail=False`). This is the tool the protocol tells an agent to
    call FIRST, and it was ~811 tokens per env+workflow pair — 5,622 today, and a linear
    40,000 at 50 envs, for the question "do I already have this?". It was also called 9
    times in one real session: 31,142 tokens of the same inventory.

    The compact row carries what answers that question — identity, tools, and whether
    the record would still be SERVED today. `detail=True` adds the digests, timestamps,
    platform, per-clause contract coverage and paths.

    What compaction must never do is hide a problem, so three things ignore the flag and
    appear whenever they are true: `contract_violations` (a record that would be
    refused), a diverging tool version, and a `usage_verification_reason` for a how-to
    that is not verified. Omitting what is ABSENT or DEFAULT is compression; omitting a
    warning is a different thing wearing the same clothes.

    Layer 1 — `envs`: the frozen, content-addressed envs in the EnvCache. These
    are the reusable "solved components": ask for one of these tools again and
    freeze serves it by digest instead of re-solving.
    Layer 2 — `workflows`: the sealed WorkflowSpecs (`*.workflow.yaml`), each
    pinning its env BY DIGEST.

    HONESTY: every env is re-anchored against the full contract before it is
    listed (`EnvCache.contract_violations`, the same check freeze/run/stage/seal
    ask at serve time), so `contract_ok: False` means "on disk but would NOT be
    served today". Listing a record as though it were usable when the serving
    paths would refuse it is exactly the false green tier 5 closed.

    Rewritten in tier 7. It previously parsed every `*.yaml` in env_reports/ as a
    `PipelineSpec` — a model whose producer (finalize_pipeline/save_pipeline_spec)
    was RETIRED in the re-spine. So it matched only WorkflowSpec/recipe yamls,
    every parse raised, a try/except turned each into an `{file, error}` dict, and
    the tool returned `count: 7` having understood exactly none of them. Broken
    for real users, and green in the suite: the disease in a user-facing tool.
    """
    pipelines_dir = Path(config["paths"]["pipelines_dir"])

    # --- Layer 1: frozen envs ------------------------------------------------
    envs: list[dict] = []
    if env_cache is None:
        from agent.skills.freeze import EnvCache
        env_cache = EnvCache(pipelines_dir / "_env_cache.json")
    for key, rec in sorted((env_cache.all() or {}).items()):
        if not isinstance(rec, dict):
            continue
        contract = env_cache.contract_report(rec)
        violations = contract.violations
        tools = _semantic_versions(rec)
        if not detail:
            row = {
                "request_key":   key,
                "name":          rec.get("name"),
                "tools":         _compact_tools(tools),
                "build_method":  rec.get("build_method") or rec.get("mode"),
                "contract_ok":   not violations,
            }
            # Shown ONLY when true — and never suppressed. `contract_ok: false` without
            # the failing clause would send the reader back to a second call to find out
            # what is wrong with an artifact we just told them not to trust.
            if violations:
                row["contract_violations"] = violations
            # A GREEN THAT PROVED LESS IS NOT THE SAME GREEN, and this is the row where
            # that matters most.
            #
            # `contract_ok` is a two-state answer to a three-state question. A clause can
            # PASS, FAIL, or have had nothing to look at — and only the first two reach a
            # boolean. The detail=True branch below has carried this since it was written,
            # with a comment saying an inventory that hid it "would be making exactly the
            # claim this field exists to qualify". The compact branch is the DEFAULT, so
            # the disclosure lived on the path almost nobody takes: the same
            # one-fact-two-readings drift as `usage_verified`, where six call sites had
            # both clauses and the seventh — the one facing the user — kept one.
            #
            # Measured on `repeatmasker_ancient` (G3 phase 5): freeze itself returned
            # `outcome: degraded`, `VALIDATED_IN_IMAGE … 0 of them RUN the tool (the rest
            # are presence/version/help probes) [presence]`, and `discriminates:
            # unobserved`. Its inventory row was `contract_ok: true`, byte-identical in
            # shape to `samtools_cluster_rung3` — whose tool was really run at a cluster
            # locus and whose evidence really discriminated. An agent asking "what is
            # already solved here" could not tell them apart.
            #
            # ASSURANCE clauses only. `WELL_FORMED.shipped_binaries` is unobserved on
            # essentially every adopt record (there are no shipped_binaries to shape-check),
            # so surfacing disclosure shortfalls here would put a constant on most rows —
            # and a field that is always present carries no signal and trains the reader to
            # skip it. The full list stays on detail=True, which is the full-disclosure view.
            unproven = [c.clause for c in contract.unobserved
                        if c.establishes == _ASSURANCE]
            if unproven:
                row["assurance_unproven"] = unproven
            if _record_is_gated(rec):
                row["license_gated"] = True
            envs.append(row)
            continue
        envs.append({
            "request_key":      key,
            "name":             rec.get("name"),
            "tools":            tools,
            "image":            rec.get("image"),
            "image_digest":     rec.get("image_digest"),
            "content_digest":   rec.get("content_digest"),
            # `build_method` is the real field ("adopt-image" / "authors-dockerfile" /
            # container-native); `mode` is the coarse two-valued sibling ("adopt" /
            # "build") that predates it. Reading `mode` here reported the authors'-
            # Dockerfile env as a generic "build" — erasing, in the inventory, the one
            # fact that distinguishes the authors'-own-machinery path this project
            # prefers. Fall back to `mode` only for records frozen before build_method.
            "build_method":     rec.get("build_method") or rec.get("mode"),
            "platform":         rec.get("platform"),
            "validation_locus": rec.get("validation_locus"),
            "created_at":       rec.get("created_at"),
            # via record_is_gated, not a bare .get: records on disk carry the legacy
            # `gated` key, and a one-key read reported a gated artifact as ungated in
            # the inventory — the same drift that once disabled I13 itself.
            "license_gated":    _record_is_gated(rec),
            # The green is EARNED here, not remembered from freeze time.
            "contract_ok":          not violations,
            "contract_violations":  violations,
            # ...and the green says how much it rests on. `contract_ok: true` with an
            # UNOBSERVED clause is a real state, and an inventory that hid it would be
            # making exactly the claim this field exists to qualify.
            "contract_coverage":    contract.summary(),
            "contract_unobserved":  [c.clause for c in contract.unobserved],
        })

    # --- Layer 2: sealed workflows -------------------------------------------
    workflows: list[dict] = []
    for spec_file in sorted(pipelines_dir.glob("*.workflow.yaml")):
        try:
            d = yaml.safe_load(spec_file.read_text()) or {}
            steps = d.get("pipeline_steps") or []
            if not detail:
                # NO `or USAGE_NOT_ATTEMPTED` FALLBACK. That default did the rounding the
                # leaf now refuses: a spec with no `usage_verification` block was reported
                # as "nobody attempted the self-test", which is a finding, from a field that
                # is simply absent. `usage_status` returns `unrecorded` for it and `""` only
                # for a spec that is not a mapping at all.
                status = usage_status(d)
                row = {
                    "workflow_name":   d.get("workflow_name"),
                    "env_request_key": d.get("env_request_key"),
                    "steps_validated": sum(1 for s in steps if step_is_validated(s)),
                    "steps_total":     len(steps),
                    "validated_in_shipped_image":
                        bool(d.get("validated_in_shipped_image")),
                    "usage_verification_status": status,
                }
                # The reason matters exactly when the how-to is NOT proven. Dropping it
                # there would leave `not_attempted` with no way to tell "nobody authored
                # a usage block" from "it spans two images and structurally cannot be
                # self-tested" — two very different facts about the same artifact.
                if status != USAGE_VERIFIED:
                    reason = (d.get("usage_verification") or {}).get("reason") or ""
                    if reason:
                        row["usage_verification_reason"] = reason
                # Same rule for the inputs: carried only when it is a WARNING. A sealed
                # workflow whose test data was never content-anchored is reproducible
                # only up to "the paths were there", and the inventory is where someone
                # decides whether to trust it.
                td_status = _test_data_status(d)
                if td_status not in (TEST_DATA_VERIFIED, TEST_DATA_NOT_ATTEMPTED):
                    row["test_data_status"] = td_status
                workflows.append(row)
                continue
            workflows.append({
                "workflow_name":  d.get("workflow_name"),
                "description":    d.get("description"),
                "created_at":     d.get("created_at"),
                "env_request_key":    d.get("env_request_key"),
                "env_image":          d.get("env_image"),
                "env_content_digest": d.get("env_content_digest"),
                "envs":               d.get("envs") or [],
                "validated_in_shipped_image": bool(d.get("validated_in_shipped_image")),
                "usage_verified":             bool(d.get("usage_verified")),
                # THE THREE-STATE READ, because the bool above cannot answer the
                # question anyone actually has. `usage_verified: false` conflates
                # "tested and broken" with "never tested" — and since seal REFUSES the
                # former, false on disk always meant the latter, a verdict nobody
                # reached. Consult this, not the bool.
                #
                # A missing key means the artifact predates the field, so the honest read
                # is `unrecorded` — never a fabricated False, and (since 2026-08-06) never
                # `not_attempted` either. Both would be this row inventing the verdict the
                # producer declined to make; the second just wore likelier clothes, and it
                # made a structurally-unprovable multi-ENV how-to indistinguishable from one
                # nobody bothered to author. (Unlike the envs[] half above, which
                # RE-EARNS contract_ok at read time, this is disclosure of a stored
                # claim: Layer 2 cannot re-anchor here, because
                # check_workflow_invariants dials out over ssh on a locus:cluster I5
                # and an inventory listing must not open cluster connections.)
                # Read through core_data.usage_status, not re-derived here. Spelling it
                # out locally made this row a THIRD reading of one field, and it drifted
                # immediately: for a spec sealed before `usage_verification` existed the
                # local version fell to "not_attempted" while `usage_verified: true` was
                # printed directly above it, so one row contradicted itself about one
                # workflow. samtools_cluster_rung3 is that artifact on disk.
                "usage_verification_status": usage_status(d),
                "usage_verification_reason": (
                    (d.get("usage_verification") or {}).get("reason") or ""),
                "steps_total":     len(steps),
                # core_data.step_is_validated — BOTH the per-file `validation` records and
                # the `mark_step_validated` override. This line used to keep only the
                # override, making it the one drifted copy of a predicate written out
                # seven times, and it reported `steps_validated: 0` for every workflow
                # ever sealed — including a five-step run with a full set of passing
                # records. Plausible, advertised in the tool description, untested.
                "steps_validated": sum(1 for s in steps if step_is_validated(s)),
                "test_data_status": _test_data_status(d),
                "path": str(spec_file),
            })
        except Exception as e:      # a malformed artifact is reported, never swallowed
            workflows.append({"file": spec_file.name, "error": str(e)})

    return {
        "envs": envs,
        "workflows": workflows,
        "counts": {
            "envs": len(envs),
            "envs_contract_ok": sum(1 for e in envs if e.get("contract_ok")),
            "workflows": len(workflows),
        },
    }
