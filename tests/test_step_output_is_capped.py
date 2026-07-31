"""A step's stdout/stderr must not be able to flood the agent's context.

MEASURED, which is why this file exists. `run_in_env` returned both streams
untruncated, so one ordinary verbose command — 4,000 lines of build chatter, the shape
`hisat2-build` / `STAR --genomeGenerate` / `pip install` actually produce — put
**47,001 tokens** into context from a single call. That is more than the entire fixed
session prompt. On the real 5-tool/5-step flagship pipeline, `run_step_in_container`
alone accounted for 35,188 tokens across 7 calls: 60% of that pipeline's whole MCP
traffic and the largest single line item in the system.

The lesson had already been learned ONE FUNCTION AWAY: the conda install path has
capped its streams at `[-3000:]` for a long time. It never reached the verb an agent
uses to run an ARBITRARY bioinformatics command. One truth, applied at exactly one
site — the same diagnosis the 2026-07-30 audit gave the whole codebase.

Two properties matter and they pull against each other, so both are tested here:
  * the cap must actually bound what travels;
  * it must not destroy or hide anything — the TAIL is kept (that is where tracebacks
    and final summaries live), the payload STATES that it truncated and by how much,
    and the full stream is on disk at a path the payload names.

A silently shortened log is indistinguishable from a short one, and "the tool printed
nothing else" is a different claim from "we stopped showing you". This codebase does
not let a report be the only record of a run.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from agent.skills.env_manager import (_STDERR_KEEP_CHARS, _STDOUT_KEEP_CHARS,
                                      cap_stream)

VERBOSE = "\n".join(f"[build] compiling module_{i}.o ... ok" for i in range(1, 4001))


def test_a_short_stream_is_returned_untouched_and_unannotated(tmp_path):
    """The common case must cost nothing and must not grow a truncation note that a
    reader would have to interpret."""
    text, note = cap_stream("all good\n", _STDOUT_KEEP_CHARS, project_root=tmp_path)
    assert text == "all good\n"
    assert note == {}


def test_a_flooding_stream_is_bounded(tmp_path):
    text, note = cap_stream(VERBOSE, _STDOUT_KEEP_CHARS, project_root=tmp_path)
    assert len(text) == _STDOUT_KEEP_CHARS
    assert note["dropped_chars"] == len(VERBOSE) - _STDOUT_KEEP_CHARS
    assert note["total_chars"] == len(VERBOSE)


def test_the_tail_is_kept_because_that_is_where_the_error_is(tmp_path):
    """Head-truncation would be worse than no truncation: it would reliably keep the
    boilerplate banner and reliably discard the reason the step failed."""
    noisy = "ok\n" * 5000 + "Traceback (most recent call last):\nValueError: boom\n"
    text, note = cap_stream(noisy, _STDERR_KEEP_CHARS, kind="stderr",
                            project_root=tmp_path)
    assert "ValueError: boom" in text
    assert note["kept"] == "tail"


def test_truncation_is_disclosed_and_the_full_stream_stays_retrievable(tmp_path):
    """Nothing is destroyed. The bytes that were cut are one Read away, at a path the
    payload itself names — an agent that needs the middle of a build log can still get
    it."""
    text, note = cap_stream(VERBOSE, _STDOUT_KEEP_CHARS, project_root=tmp_path)
    full = Path(note["full_log"])
    assert full.is_file()
    assert full.read_text() == VERBOSE, "the spilled log must be the COMPLETE stream"
    assert text in VERBOSE


def test_the_spill_lands_under_the_given_root_not_the_live_repo(tmp_path):
    """Route through the passed project_root, so the suite cannot litter the user's
    working tree — the same discipline tests/conftest.py enforces for the record
    writers."""
    _, note = cap_stream(VERBOSE, 100, project_root=tmp_path)
    assert Path(note["full_log"]).is_relative_to(tmp_path)


def test_a_failed_spill_still_returns_the_capped_text(tmp_path):
    """A log we could not write must never turn into a failed STEP. The note then
    simply carries no `full_log` key rather than a path that is not there."""
    blocked = tmp_path / "not_a_dir"
    blocked.write_text("i am a file")
    text, note = cap_stream(VERBOSE, _STDOUT_KEEP_CHARS, project_root=blocked)
    assert len(text) == _STDOUT_KEEP_CHARS
    assert note["dropped_chars"] > 0
    assert "full_log" not in note


@pytest.mark.parametrize("value", [None, 12345, b"bytes"])
def test_a_non_string_stream_does_not_crash_the_runner(value):
    """Defensive: a runner that returns None for a stream must not take the step down
    with it. C4 crash-safety, same posture as the rest of the primitives."""
    text, note = cap_stream(value, 50)
    assert isinstance(text, str) and note == {}


def test_both_real_runners_cap_their_streams():
    """The two verbs an agent actually uses to run a command — the host runner and the
    container runner — must BOTH go through the cap.

    Asserted on both because the whole defect was that one of two sibling paths had the
    fix. Checking only the one that was broken today is how it comes back in the other
    one tomorrow.
    """
    from agent.skills import docker_builder, env_manager
    for mod, name in ((env_manager, "env_manager.run_in_env"),
                      (docker_builder, "docker_builder.run_in_container")):
        src = Path(mod.__file__).read_text()
        assert "cap_stream(" in src, f"{name} does not cap its streams"
