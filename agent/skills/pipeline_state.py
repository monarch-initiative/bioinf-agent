"""
PipelineState — server-side accumulator for in-progress pipeline installs.

Each merging MCP tool can optionally pass `pipeline_id`; the accumulator
appends or patches the right slot in a disk-backed draft so the LLM never
has to hand-assemble the final spec.

  Draft path  → data/pipeline_drafts/{pipeline_id}.draft.yaml   (during install)
  Final path  → env_reports/{pipeline_name}_{version}.yaml      (after finalize)

The split keeps env_reports/ as the SHIPPABLE deliverables dir — a directory
listing tells the operator exactly what env images exist on disk + their
companion ENV.html/attestation/recipe. Drafts are workspace state and live
under data/pipeline_drafts/ (sibling of data/jobs/), so a half-finished
install isn't visually confused with a frozen artifact. Configurable via
`paths.drafts_dir` (back-compat: falls back to `paths.pipelines_dir` if
drafts_dir is unset, which preserves any caller still on the old layout).

The accumulator is opt-in: every merging tool also works without a
pipeline_id (no merge, original return value unchanged). This preserves
ad-hoc and debug flows.

Disk writes are atomic (tempfile + os.replace) so a crash mid-write
cannot leave a half-written draft.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Optional

import yaml

from agent.skills import store_lock as _store_lock
from agent.skills.outcomes import refused


def validation_key(path: str) -> str:
    """THE key a step's validation record is filed under: the output's absolute, resolved
    path. One definition, used by the store (`PipelineState.add_validation`), by the
    readers (`spec_writer` I3), and by the response builders — so the record, the invariant
    and what the agent is shown all name the same thing.

    Why not the basename: a basename is not an identity. Two outputs of one step routinely
    share one (`.../sampleA/counts.tsv`, `.../sampleB/counts.tsv`), and keying on it meant
    the later validation overwrote the earlier — deleting a `passed: False` that I3 exists
    to refuse on. Resolution also collapses `./out/x.bam` and `/abs/out/x.bam` to one key,
    so re-validating the same file updates its record instead of creating a second."""
    try:
        return str(Path(path).resolve())
    except Exception:                      # unresolvable (broken symlink, odd mount)
        return str(path)


def validation_covers(validation: dict, output_path: str) -> bool:
    """Does `validation` hold a record for this output? The READ side of `validation_key`.

    Accepts the canonical path key, the raw string as filed, and — for records written
    before path keys existed — the bare basename. The legacy fallback is deliberately last
    and deliberately narrow: on a path-keyed record no output's basename matches any key,
    so new records get the strict behaviour while old drafts keep working rather than
    failing an invariant for a naming change they had no say in."""
    if not isinstance(validation, dict) or not validation:
        return False
    return (output_path in validation
            or validation_key(output_path) in validation
            or Path(output_path).name in validation)


class PipelineState:
    def __init__(self, config: dict):
        self.config = config
        project_root = Path(__file__).parent.parent.parent.resolve()
        self.pipelines_dir = project_root / config["paths"]["pipelines_dir"]
        self.pipelines_dir.mkdir(parents=True, exist_ok=True)
        # Drafts live in their own dir (NOT env_reports/) — see module docstring.
        # Back-compat: if drafts_dir isn't set, fall back to pipelines_dir so an
        # older config keeps working without forcing a migration.
        drafts_rel = config["paths"].get("drafts_dir") or config["paths"]["pipelines_dir"]
        self.drafts_dir = project_root / drafts_rel
        self.drafts_dir.mkdir(parents=True, exist_ok=True)
        self._drafts: dict[str, dict] = {}
        self._load_existing_drafts()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self, pipeline_name: str, description: str) -> dict:
        """Initialize a draft, or resume the existing one (silent resume)."""
        if pipeline_name in self._drafts:
            return {
                "pipeline_id": pipeline_name,
                "draft_path": str(self._draft_path(pipeline_name)),
                "resumed": True,
            }
        # No env_status/pipeline_status stamp: they were written 'in_progress' here
        # and NEVER transitioned, so they answered nothing while LOOKING like a
        # state machine. `current_state()` derives the real lifecycle position from
        # the artifacts on every read instead (Phase-3 Piece B).
        self._drafts[pipeline_name] = {
            "pipeline_name":   pipeline_name,
            "description":     description,
            "packages":        [],
            "install_steps":   [],
            "pipeline_steps":  [],
        }
        self._persist(pipeline_name)
        return {
            "pipeline_id": pipeline_name,
            "draft_path":  str(self._draft_path(pipeline_name)),
            "resumed":     False,
        }

    def discard(self, pipeline_id: str) -> dict:
        """Delete a draft and clear in-memory state."""
        existed = pipeline_id in self._drafts
        self._drafts.pop(pipeline_id, None)
        path = self._draft_path(pipeline_id)
        if path.exists():
            path.unlink()
        return {"pipeline_id": pipeline_id, "existed": existed}

    def get_draft(self, pipeline_id: str) -> Optional[dict]:
        return self._drafts.get(pipeline_id)

    def pop_for_finalize(self, pipeline_id: str) -> Optional[dict]:
        """Remove the in-memory draft and return it. Caller deletes the file."""
        return self._drafts.pop(pipeline_id, None)

    def mutate_on_disk(self, pipeline_id: str, apply) -> bool:
        """Apply `apply(draft)` to the draft AS IT IS ON DISK, under the store lock.

        THE CROSS-PROCESS WRITE PATH. `self._drafts` is an in-memory cache populated at
        construction and never re-read, which is correct for one process and wrong the
        moment there are two — and there are two whenever `freeze(background=True)` is
        used, a shipped feature that spawns a DETACHED SUBPROCESS. The subprocess builds
        its own PipelineState, writes `frozen_as`, and exits; the parent still holds the
        pre-freeze copy and erases the pointer on its next `_persist`. The freeze was
        real and its record on the draft simply disappeared.

        So a writer that may be racing re-reads first: take the lock, load the file,
        apply, write, and refresh the in-memory copy from what was actually written.

        THIS DOES NOT MAKE THE WHOLE STORE CONCURRENCY-SAFE, and the difference is worth
        stating plainly rather than implying by omission. The ordinary mutators
        (`add_step`, `patch`, `add_validation`, …) still edit `self._drafts` and persist
        it wholesale, so two processes mutating the SAME draft through them still lose
        one side's edits. That is acceptable today because only this pointer is written
        cross-process; it is the thing to convert first when batch install lands."""
        with _store_lock.locked(self._draft_path(pipeline_id)):
            on_disk = self._read_draft_file(pipeline_id)
            draft = on_disk if on_disk is not None else self._drafts.get(pipeline_id)
            if draft is None:
                return False
            apply(draft)
            self._drafts[pipeline_id] = draft
            self._write_draft_file(pipeline_id, draft)
        return True

    def set_frozen_pointer(self, pipeline_id: str, request_key: str) -> None:
        """Record which frozen env (by request_key) this pipeline produced, so
        `current_state` can RE-EARN ENV_FROZEN via the honesty contract. A best-
        effort ORIENTATION pointer — never authoritative (current_state re-checks
        it) and never allowed to fail the freeze that calls it.

        Written through `mutate_on_disk` because the caller may be a background-freeze
        SUBPROCESS (see that method)."""
        def _set(draft):
            draft["frozen_as"] = request_key
        self.mutate_on_disk(pipeline_id, _set)

    def append_sealed_pointer(self, pipeline_id: str, workflow_name: str) -> None:
        """Record which workflow(s) this pipeline has sealed. A LIST — one draft
        may seal many workflows (seal does not pop the draft). Orientation only;
        `current_state` re-earns SEALED by matching the spec to this pipeline."""
        def _append(draft):
            sealed = draft.setdefault("sealed_as", [])
            if workflow_name not in sealed:
                sealed.append(workflow_name)
        self.mutate_on_disk(pipeline_id, _append)

    def delete_draft_file(self, pipeline_id: str) -> None:
        path = self._draft_path(pipeline_id)
        if path.exists():
            path.unlink()

    # ------------------------------------------------------------------
    # Merge operations (each returns True/index on success, None/False if
    # the pipeline_id is unknown — callers translate to error responses)
    # ------------------------------------------------------------------

    def add_package(self, pipeline_id: str, package_record: dict) -> Optional[int]:
        """Append a PackageRecord, or update by name if it already exists."""
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return None
        pkgs = draft.setdefault("packages", [])
        for i, p in enumerate(pkgs):
            if p.get("name") == package_record.get("name"):
                pkgs[i] = {**p, **package_record}
                self._persist(pipeline_id)
                return i
        pkgs.append(package_record)
        self._persist(pipeline_id)
        return len(pkgs) - 1

    def cache_search_result(self, pipeline_id: str, name: str, entry: dict) -> bool:
        """Store search_package metadata (description, homepage, input/output types)
        keyed by package name. Used by spec_writer's finalize-time package
        derivation to annotate records without re-querying."""
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return False
        cache = draft.setdefault("search_cache", {})
        cache[name] = {**(cache.get(name) or {}), **entry}
        self._persist(pipeline_id)
        return True

    def cache_verification(self, pipeline_id: str, name: str, entry: dict) -> bool:
        """Store verify_installation result (verify_command, verify_output)
        keyed by package name. Used by spec_writer's finalize-time package
        derivation to attach verification info to the rebuilt records."""
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return False
        verifications = draft.setdefault("verifications", {})
        verifications[name] = {**(verifications.get(name) or {}), **entry}
        self._persist(pipeline_id)
        return True

    def patch_package(self, pipeline_id: str, package_name: str, patch: dict) -> bool:
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return False
        for p in draft.get("packages", []):
            if p.get("name") == package_name:
                p.update(patch)
                self._persist(pipeline_id)
                return True
        return False

    def set_conda_env(
        self, pipeline_id: str, env_name: str, python_version: str = ""
    ) -> bool:
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return False
        draft["conda_env"] = env_name
        if python_version:
            draft["python_version"] = python_version
        self._persist(pipeline_id)
        return True

    def add_step(
        self,
        pipeline_id: str,
        step_data: dict,
        replace_step: int = 0,
    ) -> Optional[int]:
        """Append (default) or replace step N (1-based) in pipeline_steps.
        Returns the step number."""
        return self._add_to_step_list(pipeline_id, "pipeline_steps", step_data, replace_step)

    def add_install_step(
        self,
        pipeline_id: str,
        step_data: dict,
        replace_step: int = 0,
    ) -> Optional[int]:
        """Append (default) or replace step N (1-based) in install_steps.
        Returns the step number."""
        return self._add_to_step_list(pipeline_id, "install_steps", step_data, replace_step)

    def _add_to_step_list(
        self,
        pipeline_id: str,
        list_name: str,
        step_data: dict,
        replace_step: int,
    ) -> Optional[int]:
        """Append (default) or replace step N. `replace_step=N` always REMOVES
        the prior entry at slot N — the caller is asserting 'this IS my step
        N, throw away whatever was there'. The new entry's POSITION depends
        on chronological-order semantics:

          - existing FAILED (rc != 0, != None) AND new SUCCEEDED (rc == 0):
            the new entry lands at the END (after any intervening successful
            steps). Preserves the install-order replay invariant: a retry of
            step N after the agent installed the missing dep at N+1 must
            replay AS step N+1 (after the dep), not at the original N.

          - all other cases: the new entry lands at slot N (edit in place).
            Agent is e.g. bumping a version on an already-successful step.

        Either way the prior entry at N is DELETED. N4 (batch-3 Apollo3 fix):
        pre-fix the smart-append-on-failed path APPENDED without removing the
        prior entry, leaving both rc=1 and rc=0 records in install_steps —
        which then surfaced as duplicate installs in the freeze replay and
        required hand-editing the draft yaml to remove.
        """
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return None
        steps = draft.setdefault(list_name, [])
        if replace_step > 0 and replace_step <= len(steps):
            existing = steps[replace_step - 1]
            existing_rc = existing.get("returncode")
            new_rc = step_data.get("returncode")
            # ALWAYS remove the prior entry at this slot first — the user's
            # contract: 'replace_step=N means throw away whatever was at N'.
            # (N4 fix: this previously only happened in the edit-in-place
            # branch; the smart-replace path appended without removing,
            # leaving the failed entry behind.)
            del steps[replace_step - 1]
            if existing_rc not in (0, None) and new_rc == 0:
                # Smart-replace: previous attempt failed, new succeeded → new
                # lands at end (after any intervening successful steps).
                new_index = len(steps) + 1
                step_data = {**step_data, "step": new_index}
                steps.append(step_data)
                # Re-number any subsequent steps so step fields stay 1-based-
                # contiguous (we just removed at replace_step - 1 and appended
                # at end; everything between shifted down by one).
                for i, st in enumerate(steps, start=1):
                    if isinstance(st, dict):
                        st["step"] = i
                self._persist(pipeline_id)
                return new_index
            # Edit-in-place semantic (success-keeps-position OR rc unknown):
            # merge the new fields over the existing record at the same slot.
            merged = {**existing, **step_data, "step": replace_step}
            # `validation` is captured OUT OF BAND (add_validation runs AFTER
            # add_step), so a re-run's step_data never carries it — and the merge
            # above would preserve the PRIOR command's per-file validation records.
            # That silently violates the "throw away whatever was at N" contract:
            # replacing an index-build step (8 .ht2 outputs validated 'any') with a
            # fastqc step leaves the 8 stale 'any' records behind, which then fail
            # I3.declared_output_type at seal even though the current step's outputs
            # are all clean. A re-run re-validates its OWN outputs, so drop the
            # inherited records unless the caller explicitly provided replacements.
            if "validation" not in step_data:
                merged.pop("validation", None)
            steps.insert(replace_step - 1, merged)
            self._persist(pipeline_id)
            return replace_step
        new_index = len(steps) + 1
        step_data = {**step_data, "step": new_index}
        steps.append(step_data)
        self._persist(pipeline_id)
        return new_index

    def mark_pipeline_step_validated(
        self,
        pipeline_id: str,
        step: int,
        validation_status: str,
    ) -> bool:
        """Set validation_status on pipeline_steps[step-1]. Returns False if
        the pipeline or step is unknown."""
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return False
        steps = draft.get("pipeline_steps", [])
        if step < 1 or step > len(steps):
            return False
        steps[step - 1]["validation_status"] = validation_status
        self._persist(pipeline_id)
        return True

    def add_validation(
        self,
        pipeline_id: str,
        step: int,
        path: str,
        validation_result: dict,
    ) -> bool:
        """File one output's validation result under the step.

        The key is normalized HERE, by `validation_key`, and not by the caller. Five call
        sites used to compute it themselves and all five chose `Path(path).name` — so two
        outputs of one step that share a basename (`/out/a/result.bam` and
        `/out/b/result.bam`, the ordinary shape of a per-sample fan-out) collided on one
        key and the SECOND write silently destroyed the first.

        That is not a cosmetic loss. I3's non-overridable clause refuses a seal when any
        validation record says `passed: False` — and the erased record was the failing one
        whenever the failure came first. Reproduced: one step, two same-named outputs, the
        first truncated; the runtime recorded a single `{"passed": true}` and
        `check_workflow_invariants` returned no I3 violation at all. Evidence that exists
        and says FAILED cannot be un-failed by an assertion — but it could be overwritten
        by an unrelated file that happened to share a name.

        A key rule enforced at five call sites is a key rule with five chances to differ;
        the store owns it now, so passing a bare basename simply files it under that
        basename and a path files it under the path."""
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return False
        steps = draft.get("pipeline_steps", [])
        if step < 1 or step > len(steps):
            return False
        s = steps[step - 1]
        s.setdefault("validation", {})[validation_key(path)] = validation_result
        self._persist(pipeline_id)
        return True

    def set_test_data(self, pipeline_id: str, test_data: dict) -> bool:
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return False
        draft["test_data"] = test_data
        self._persist(pipeline_id)
        return True

    def upsert_service_dependency(
        self, pipeline_id: str, name: str, fields: dict
    ) -> Optional[int]:
        """Append or update a service_dependency by name. Used by start_service
        / verify_service_dependency to register declarations and append probes
        to health_check_log. Replacement is field-merge: new fields are deep-
        merged into the existing entry, NOT a wholesale clobber, so an initial
        declaration (name/start_command/health_check_command/…) survives a
        later upsert that only appends a probe.

        For the `health_check_log` field specifically, source entries are
        APPENDED to the existing list rather than replacing it — every probe
        observed by the runtime accumulates.
        """
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return None
        deps = draft.setdefault("service_dependencies", [])
        for i, d in enumerate(deps):
            if isinstance(d, dict) and d.get("name") == name:
                merged = dict(d)
                for k, v in fields.items():
                    if k == "health_check_log" and isinstance(v, list):
                        existing = merged.get("health_check_log") or []
                        merged["health_check_log"] = list(existing) + list(v)
                    else:
                        merged[k] = v
                deps[i] = merged
                self._persist(pipeline_id)
                return i
        new_entry = {"name": name, **fields}
        deps.append(new_entry)
        self._persist(pipeline_id)
        return len(deps) - 1

    def add_authored_artifact(self, pipeline_id: str, artifact: dict) -> Optional[int]:
        """Append (or replace by path) an AuthoredArtifact entry.

        Records of agent-authored files — driver scripts, synthetic test
        inputs, staged transformations — so the pipeline composes honestly:
        every step input traces to either a prior step's output OR a recorded
        external source. The path is added to the I8 universe at finalize,
        and I9 re-verifies sha256(file_on_disk) matches the recorded value.

        Replace-by-path semantics means the agent can iterate on a script
        without leaving stale entries.
        """
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return None
        artifacts = draft.setdefault("authored_artifacts", [])
        target_path = artifact.get("path")
        for i, a in enumerate(artifacts):
            if a.get("path") == target_path:
                artifacts[i] = artifact
                self._persist(pipeline_id)
                return i
        artifacts.append(artifact)
        self._persist(pipeline_id)
        return len(artifacts) - 1

    # Top-level keys patch_pipeline is allowed to write. Anything else is
    # runtime-captured (pipeline_steps / install_steps / packages / validation
    # / verifications / resource_usage) or finalize-derived (env_status,
    # pipeline_status, docker_status, usage_verified, lock_sha256) and cannot
    # be hand-supplied via patch — otherwise the honesty contract is bypassable.
    #
    # The agent's role via patch is limited to fields no primitive captures:
    # author-supplied prose (description, notes, final_summary), the runtime-
    # environment hint, runtime / reference configs, service dependencies,
    # and the canonical usage block (whose self-test is re-run live anyway).
    PATCHABLE_KEYS: frozenset[str] = frozenset({
        "description", "notes", "final_summary",
        "conda_env", "created_at", "python_version", "reference_free",
        "runtime_environment", "runtime_configs", "reference_databases",
        "usage",
        # Acceleration + licensing claims. Agent-authored, but their internal
        # consistency is guarded by I12 / I13 (an inconsistent claim — e.g.
        # runtime_verified with no probe, or license_gated + redistributable —
        # is rejected at finalize). The resolver is the *detector*; these
        # invariants are the *guard*.
        "accelerator", "license_gated", "licenses", "redistributable",
    })

    # Keys that are runtime-captured or finalize-derived. Blocked from patch
    # entirely. If the agent needs to modify one of these, it must go through
    # the dedicated primitive (run_pipeline_step, verify_installation, etc.).
    BLOCKED_PATCH_KEYS: frozenset[str] = frozenset({
        "pipeline_steps", "install_steps", "packages",
        "verifications", "search_cache", "test_data", "docker",
        "env_status", "pipeline_status", "docker_status",
        "usage_verified", "lock_sha256",
        "authored_artifacts",      # owned by stage_authored_artifact (sha256-anchored)
        "service_dependencies",    # owned by start_service / verify_service_dependency (I10 probe-anchored)
        # Lifecycle POINTERS — written only by freeze / seal (set_frozen_pointer /
        # append_sealed_pointer). Hand-supplying one would forge a lifecycle claim;
        # current_state re-earns it anyway, but block the patch as defense-in-depth.
        "frozen_as", "sealed_as",
    })

    def patch(self, pipeline_id: str, patches: dict) -> dict:
        """Deep-merge agent-authored patches into the draft.

        Only keys in PATCHABLE_KEYS are accepted. Patches to runtime-captured
        or finalize-derived fields are rejected with a clear violation —
        those have to flow through their dedicated primitive so the spec's
        claims stay anchored to observed reality.
        """
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return refused("pipeline_state.unknown_pipeline", error=f"unknown pipeline_id: {pipeline_id}")

        rejected = [k for k in patches if k in self.BLOCKED_PATCH_KEYS]
        unknown  = [k for k in patches if k not in self.PATCHABLE_KEYS and k not in self.BLOCKED_PATCH_KEYS]
        if rejected:
            return refused(
                "pipeline_state.blocked_keys",
                error=(
                    f"patch refused — these keys are runtime-captured or "
                    f"finalize-derived and cannot be hand-supplied: {rejected}. "
                    f"Use the dedicated primitive (run_pipeline_step, "
                    f"verify_installation, install_conda_packages, freeze) "
                    f"so the spec stays anchored to observed reality."
                ),
                rejected_keys=rejected,
                patchable_keys=sorted(self.PATCHABLE_KEYS),
            )
        if unknown:
            return refused(
                "pipeline_state.unknown_keys",
                error=(
                    f"patch refused — unknown keys: {unknown}. "
                    f"Did you mean one of {sorted(self.PATCHABLE_KEYS)}?"
                ),
                unknown_keys=unknown,
                patchable_keys=sorted(self.PATCHABLE_KEYS),
            )

        _deep_merge(draft, patches)
        self._persist(pipeline_id)
        return {
            "draft_path":   str(self._draft_path(pipeline_id)),
            "patched_keys": list(patches.keys()),
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _draft_path(self, pipeline_id: str) -> Path:
        return self.drafts_dir / f"{pipeline_id}.draft.yaml"

    def _persist(self, pipeline_id: str) -> None:
        """Atomic write of the in-memory draft to disk, under the store lock.

        The lock makes this write not INTERLEAVE with another process's read-modify-write
        of the same draft. It does NOT make the write correct when this process's copy is
        stale — for that a writer must re-read first, which is what `mutate_on_disk` is
        for. Locking a stale write only makes it lose the race cleanly."""
        with _store_lock.locked(self._draft_path(pipeline_id)):
            self._write_draft_file(pipeline_id, self._drafts[pipeline_id])

    def _write_draft_file(self, pipeline_id: str, draft: dict) -> None:
        """The atomic write itself — assumes the caller holds the lock."""
        path = self._draft_path(pipeline_id)
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(path.parent),
            prefix=path.stem, suffix=".tmp", delete=False,
        ) as f:
            yaml.dump(draft, f, default_flow_style=False, sort_keys=False)
            tmp = f.name
        os.replace(tmp, path)

    def _read_draft_file(self, pipeline_id: str) -> Optional[dict]:
        """Re-read one draft from disk, or None if it is absent/unreadable. Absent is a
        real answer (the draft was discarded); unreadable returns None too so a writer
        falls back to its in-memory copy rather than destroying the file with a partial
        one — the pointer this serves is explicitly best-effort."""
        path = self._draft_path(pipeline_id)
        try:
            data = yaml.safe_load(path.read_text())
        except (OSError, yaml.YAMLError):
            return None
        return data if isinstance(data, dict) else None

    def _load_existing_drafts(self) -> None:
        """Recover drafts from disk on server startup. Scans both the new
        drafts_dir AND the legacy pipelines_dir location so an upgrade picks
        up drafts that pre-date the split. New writes always go to drafts_dir."""
        scan_dirs = [self.drafts_dir]
        if self.pipelines_dir != self.drafts_dir:
            scan_dirs.append(self.pipelines_dir)
        seen: set[str] = set()
        for d in scan_dirs:
            if not d.exists():
                continue
            for f in d.glob("*.draft.yaml"):
                pid = f.name.removesuffix(".draft.yaml")
                if pid in seen:
                    continue
                seen.add(pid)
                try:
                    with open(f) as fp:
                        self._drafts[pid] = yaml.safe_load(fp) or {}
                except Exception:
                    continue


# Lists whose elements are merged by their `step` field rather than replaced
# wholesale. A partial patch like {"pipeline_steps": [{"step": 2, "validation_status": "passed"}]}
# updates the existing step 2 in place, preserving the rest of its data and any
# other entries in the list.
_STEP_KEYED_LISTS = frozenset({"pipeline_steps", "install_steps"})


def _deep_merge(target: dict, source: dict) -> None:
    """Recursively merge source into target (mutates target in place).

    Dicts merge recursively. For lists named in _STEP_KEYED_LISTS, elements
    are merged by their `step` field (existing step N updated; new step N
    appended) — so a partial patch can update validation_status without
    clobbering the rest of the step's data. Other lists are replaced wholesale.

    Deletion: set a value to the literal sentinel "__DELETE__" to remove that
    key from target. Lets patch_pipeline express removals (e.g., dropping a
    `verifications.foo` entry) without resorting to writing null and inviting
    downstream None-handling bugs.
    """
    for key, val in source.items():
        if val == "__DELETE__":
            target.pop(key, None)
            continue
        if key in _STEP_KEYED_LISTS and isinstance(val, list) and isinstance(target.get(key), list):
            _merge_step_keyed_list(target[key], val)
        elif key in target and isinstance(target[key], dict) and isinstance(val, dict):
            _deep_merge(target[key], val)
        else:
            target[key] = val


def _merge_step_keyed_list(target: list, source: list) -> None:
    """Merge `source` entries into `target` by matching on the `step` field."""
    by_step = {s.get("step"): i for i, s in enumerate(target) if isinstance(s, dict)}
    for entry in source:
        if not isinstance(entry, dict):
            target.append(entry)
            continue
        step = entry.get("step")
        if step is not None and step in by_step:
            target[by_step[step]] = {**target[by_step[step]], **entry}
        else:
            target.append(entry)
            if step is not None:
                by_step[step] = len(target) - 1


# ---------------------------------------------------------------------------
# Rail lifecycle — the ONE answer to "where is this pipeline?" (Phase-3 Piece B)
# ---------------------------------------------------------------------------
#
# Before this, the answer was SCATTERED across four unlinked places (draft
# key-population + the EnvCache record + a digest-membership walk + a
# {name}.workflow.yaml on disk), and the two fields that LOOKED like the state
# machine — draft['env_status'] / draft['pipeline_status'] — were stamped
# 'in_progress' at birth and NEVER transitioned, so they answered nothing. This
# collapses the scatter into one deriver, computed EVERY read from the artifacts
# that already own the truth — never a stored boolean.

ABSENT     = "absent"        # no draft in the store for this pipeline_id
DRAFT      = "draft"         # a draft exists; nothing installed yet
ENV_BUILT  = "env_built"     # a host conda env + at least one install step
ENV_FROZEN = "env_frozen"    # a frozen env, RE-EARNED against the honesty contract
SEALED     = "sealed"        # a WorkflowSpec that matches this pipeline + pins a live env

# Ordering low→high, for a caller that wants to compare progress.
LIFECYCLE_ORDER = (ABSENT, DRAFT, ENV_BUILT, ENV_FROZEN, SEALED)


def current_state(draft: Optional[dict], *, verify_frozen, spec_sealed) -> str:
    """The pipeline's highest reached lifecycle state, RE-EARNED from artifacts.

    Re-earned, never trusted-because-stored: the draft carries `frozen_as` /
    `sealed_as` POINTERS (written by freeze / seal), but this asks the injected
    checks to CONFIRM them every read — so a forged pointer, an evicted env, or a
    hand-edited spec self-heals DOWN to the highest state that still verifies (the
    same posture freeze/seal/run take when they re-anchor a cache hit).

    The two checks are REQUIRED (no default): an unwired call RAISES rather than
    silently assuming a state — a permissive default would be the 'gate present in
    code, absent in effect' disease this codebase is defined by. Callers build
    them once via `state_checks(...)`.
        verify_frozen(request_key) -> bool   # EnvCache.lookup_verified is green
        spec_sealed(draft)         -> bool   # a sealed spec matches THIS pipeline
                                             #   AND still pins a live env digest

    RUN_VALIDATED is deliberately NOT a state here: this deriver is ORIENTATION,
    not a gate (the only Phase-3 enforcement is the seal write-guard), so the
    frozen-vs-sealed distinction is what a reader needs; run-in-shipped-image is a
    per-run quality axis carried by WorkflowSpec.pipeline_status, not a lifecycle
    position. (Dropping it also keeps this free of a user_guide dependency.)"""
    if not draft:
        return ABSENT
    if draft.get("sealed_as") and spec_sealed(draft):
        return SEALED
    if draft.get("frozen_as") and verify_frozen(draft["frozen_as"]):
        return ENV_FROZEN
    if draft.get("conda_env") and (draft.get("install_steps") or []):
        return ENV_BUILT
    return DRAFT


def state_checks(env_cache, reports_dir) -> dict:
    """Build current_state's two RE-EARNED checks, bound to the live deps. ONE
    definition, injected by every caller — so agent_status and the resume summary
    ask the IDENTICAL question rather than forking the derivation (this codebase's
    signature defect). `reports_dir` is where {name}.workflow.yaml lives."""
    reports_dir = Path(reports_dir)

    def verify_frozen(request_key: str) -> bool:
        try:
            rec, violations = env_cache.lookup_verified(request_key)
            return bool(rec) and not violations
        except Exception:
            return False

    def spec_sealed(draft: dict) -> bool:
        # SEALED re-earns like ENV_FROZEN — never a bare os.stat. A named sealed
        # spec must (a) exist, (b) IDENTITY-match this pipeline (its
        # env_request_key equals the draft's frozen_as — because workflow_name is
        # caller-overridable, so two pipelines can collide on one filename), and
        # (c) still pin an env image_digest that's in the EnvCache.
        names = draft.get("sealed_as") or []
        if not names:
            return False
        try:
            live_digests = {r.get("image_digest") for r in (env_cache.all() or {}).values()
                            if r.get("image_digest")}
        except Exception:
            live_digests = set()
        frozen_key = draft.get("frozen_as")
        for nm in names:
            p = reports_dir / f"{nm}.workflow.yaml"
            if not p.exists():
                continue
            try:
                spec = yaml.safe_load(p.read_text()) or {}
            except Exception:
                continue
            if frozen_key and spec.get("env_request_key") and spec.get("env_request_key") != frozen_key:
                continue                              # a different pipeline's spec on a colliding name
            spec_digests = {e.get("image_digest") for e in (spec.get("envs") or [])
                            if isinstance(e, dict) and e.get("image_digest")}
            if spec_digests and not (spec_digests & live_digests):
                continue                              # the env it pinned is gone from the cache
            return True
        return False

    return {"verify_frozen": verify_frozen, "spec_sealed": spec_sealed}
