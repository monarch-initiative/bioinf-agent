"""
PipelineState — server-side accumulator for in-progress pipeline installs.

Each merging MCP tool can optionally pass `pipeline_id`; the accumulator
appends or patches the right slot in a disk-backed draft so the LLM never
has to hand-assemble the final spec.

  Draft path  → env_reports/{pipeline_id}.draft.yaml      (during install)
  Final path  → env_reports/{pipeline_name}_{version}.yaml (after finalize)

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


class PipelineState:
    def __init__(self, config: dict):
        self.config = config
        project_root = Path(__file__).parent.parent.parent.resolve()
        self.pipelines_dir = project_root / config["paths"]["pipelines_dir"]
        self.pipelines_dir.mkdir(parents=True, exist_ok=True)
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
        self._drafts[pipeline_name] = {
            "pipeline_name":   pipeline_name,
            "description":     description,
            "env_status":      "in_progress",
            "pipeline_status": "in_progress",
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
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return None
        steps = draft.setdefault(list_name, [])
        if replace_step > 0 and replace_step <= len(steps):
            existing = steps[replace_step - 1]
            merged = {**existing, **step_data, "step": replace_step}
            steps[replace_step - 1] = merged
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
        filename: str,
        validation_result: dict,
    ) -> bool:
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return False
        steps = draft.get("pipeline_steps", [])
        if step < 1 or step > len(steps):
            return False
        s = steps[step - 1]
        s.setdefault("validation", {})[filename] = validation_result
        self._persist(pipeline_id)
        return True

    def set_test_data(self, pipeline_id: str, test_data: dict) -> bool:
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return False
        draft["test_data"] = test_data
        self._persist(pipeline_id)
        return True

    def set_docker(self, pipeline_id: str, docker_result: dict) -> bool:
        """Pluck just the DockerBuild-shaped fields from a build_docker_image return."""
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return False
        keys = {
            "build_attempted", "build_success", "image_tag", "registry",
            "pushed_to_registry", "reason", "nvidia_runtime",
            "volume_mounts", "runtime_data_env",
        }
        draft["docker"] = {k: docker_result[k] for k in keys if k in docker_result}
        self._persist(pipeline_id)
        return True

    def patch(self, pipeline_id: str, patches: dict) -> dict:
        """Deep-merge arbitrary patches into the draft. Escape hatch."""
        draft = self._drafts.get(pipeline_id)
        if draft is None:
            return {"error": f"unknown pipeline_id: {pipeline_id}"}
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
        return self.pipelines_dir / f"{pipeline_id}.draft.yaml"

    def _persist(self, pipeline_id: str) -> None:
        """Atomic write of the draft to disk."""
        path = self._draft_path(pipeline_id)
        draft = self._drafts[pipeline_id]
        with tempfile.NamedTemporaryFile(
            mode="w", dir=str(path.parent),
            prefix=path.stem, suffix=".tmp", delete=False,
        ) as f:
            yaml.dump(draft, f, default_flow_style=False, sort_keys=False)
            tmp = f.name
        os.replace(tmp, path)

    def _load_existing_drafts(self) -> None:
        """Recover drafts from disk on server startup."""
        for f in self.pipelines_dir.glob("*.draft.yaml"):
            try:
                pid = f.name.removesuffix(".draft.yaml")
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
