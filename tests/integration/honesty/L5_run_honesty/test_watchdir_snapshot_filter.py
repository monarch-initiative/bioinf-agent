"""
N7 (batch-3 Apollo3 stress): when a step's `watch_dir` is a system-shared
location like /tmp, the snapshot must filter to files that resolve UNDER
project_root. Otherwise foreign files (the Claude harness's own subagent
transcripts under ~/.claude/projects/, other processes' tmpfiles) get
recorded as step outputs and trip I8 at seal time.

The filter only fires when watch_dir is OUTSIDE project_root — a watch
INSIDE project_root needs no filter (every produced file is project-
resident by construction). And without project_root, behavior is
unchanged (back-compat).

Why integration, not unit: the bug lives in symlink resolution. macOS
resolves /tmp → /private/tmp, and the snapshot logic walks the SOURCE dir
but filters on RESOLVED paths. A unit test using a constructed dict of
paths can't expose this — it has to be real files on a real filesystem,
which is why every prior unit test passed even with the bug live.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from agent.skills.env_manager import EnvManager


@pytest.mark.integration
def test_external_watch_dir_filters_foreign_files(tmp_path):
    """watch_dir is OUTSIDE project_root → foreign files (resolving outside
    project_root) are filtered out."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    watch_dir = tmp_path / "shared"        # outside project_root
    watch_dir.mkdir()

    before = EnvManager._snapshot(watch_dir)

    # File 1: a project-resident output (resolves under project_root via symlink).
    project_file = project_root / "real_output.bam"
    project_file.write_text("BAM")
    link_in_watch = watch_dir / "step_output.bam"
    link_in_watch.symlink_to(project_file)

    # File 2: a FOREIGN file written directly in the watch_dir (resolves
    # outside project_root). This is the harness-transcript / other-process
    # case Apollo3 hit.
    foreign = watch_dir / "agent-XYZ.jsonl"
    foreign.write_text("[]")

    time.sleep(0.01)
    detected = EnvManager._diff_snapshot(before, watch_dir, project_root)

    # The project-resident file (via symlink) survives the filter.
    resolved_outputs = {str(Path(p).resolve()) for p in detected}
    assert str(project_file.resolve()) in resolved_outputs, \
        f"project-resident output filtered out by mistake: {detected!r}"
    # The foreign file is filtered out.
    assert str(foreign.resolve()) not in resolved_outputs, \
        f"foreign file leaked through scope filter: {detected!r}"


@pytest.mark.integration
def test_internal_watch_dir_no_filter(tmp_path):
    """watch_dir is INSIDE project_root → no filter. Every produced file in
    the watch_dir is by construction project-resident; we don't waste an
    extra resolve() on it."""
    project_root = tmp_path / "project"
    project_root.mkdir()
    watch_dir = project_root / "step1_out"
    watch_dir.mkdir()

    before = EnvManager._snapshot(watch_dir)
    out = watch_dir / "out.vcf"
    out.write_text("##fileformat=VCFv4.2")
    time.sleep(0.01)

    detected = EnvManager._diff_snapshot(before, watch_dir, project_root)
    assert any(Path(p).name == "out.vcf" for p in detected), \
        f"in-project output was incorrectly dropped: {detected!r}"


@pytest.mark.integration
def test_no_project_root_preserves_legacy_behavior(tmp_path):
    """Back-compat: callers that don't pass project_root get the pre-N7
    behavior (every changed file is returned, no scope filter)."""
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    before = EnvManager._snapshot(watch_dir)
    f = watch_dir / "anything.txt"
    f.write_text("x")
    time.sleep(0.01)

    detected = EnvManager._diff_snapshot(before, watch_dir)   # no project_root
    assert any(Path(p).name == "anything.txt" for p in detected)
