"""A failure terminal always says what went wrong.

`refused` / `broke` / `vanished` carry a top-level `error` by construction: when the
builder set none, the tagger derives it from the fields it did set — a prose field,
the first violation, the last informative line of stderr — and never leaves a reader
to dig through `build.stderr` or a violations list for the cause. `degraded`, `loop`
and `proven` did the work and get nothing added.
"""
from __future__ import annotations

from agent.skills import outcomes
from agent.skills.outcomes import broke, degraded, derive_error, last_informative_line, loop, proven, refused


def test_a_builder_that_set_error_is_never_overridden():
    assert refused("x.y", error="the real cause", stderr="noise\n")["error"] == "the real cause"


def test_a_bare_failure_names_its_code_and_exit():
    assert refused("x.y", success=False)["error"] == "x.y"
    assert broke("x.y", returncode=2)["error"] == "x.y (exit 2)"


def test_prose_fields_are_used_in_order():
    assert refused("x.y", reason="r", message="m")["error"] == "r"
    assert refused("x.y", message="m", detail="d")["error"] == "m"
    assert refused("x.y", detail="d")["error"] == "d"
    assert refused("x.y", reason="  ", message="m")["error"] == "m"       # blank prose is absent


def test_a_violations_list_is_summarised_from_its_first_entry():
    r = refused("seal.workflow_invariants", violations=[
        {"invariant": "I8.composition_coherence", "message": "step 2 input /x has no producing source"},
        {"invariant": "I5.reference_database_missing", "message": "gone"}])
    assert r["error"] == "2 violations; first: I8.composition_coherence: step 2 input /x has no producing source"
    assert refused("x", violations=[{"clause": "C1", "detail": "d"}])["error"] == "1 violation; first: C1: d"
    assert refused("x", violations=["plain text"])["error"] == "1 violation; first: plain text"


def test_the_last_informative_stderr_line_is_the_error():
    stderr = ("Collecting pkg\nERROR: No matching distribution found for pkg==9\n"
              "ERROR conda.cli.main_run:execute(148): `conda run bash -c pip install pkg==9` failed. (See above for error)\n")
    assert broke("env_manager.pip_install_failed", returncode=1, stderr=stderr)["error"] \
        == "ERROR: No matching distribution found for pkg==9"


def test_a_stream_nested_one_level_down_is_read_too():
    r = broke("freeze.container_build_failed", build={"stderr": "step 3/7: RUN pixi add x\nerror: no candidates for x\n"})
    assert r["error"] == "error: no candidates for x"


def test_stdout_is_read_when_stderr_is_empty():
    assert broke("x", stderr="", stdout="FAILED: tool exited 3\n")["error"] == "FAILED: tool exited 3"


def test_success_classes_get_no_error():
    assert "error" not in proven("x", success=True)
    assert "error" not in degraded("x", reason="less assurance")
    assert "error" not in loop("x", reason="retry")


def test_last_informative_line_skips_punctuation_and_wrapper_lines():
    assert last_informative_line("ERROR: lazy loading failed\n]\n") == "ERROR: lazy loading failed"
    assert last_informative_line("a\n}\n)\n") == "a"
    assert last_informative_line("real cause\nTraceback (most recent call last)\n") == "real cause"
    assert last_informative_line("real cause\n(See above for error)\n") == "real cause"
    assert last_informative_line("]\n") == "]"                        # nothing better exists
    assert last_informative_line("") == "" and last_informative_line(None) == ""
    assert len(last_informative_line("x" * 1000, limit=50)) == 50


def test_derive_error_is_the_one_reading_and_is_capped():
    assert derive_error({"reason": "r" * 1000}, "c") == "r" * outcomes.ERROR_LINE_LIMIT
    assert derive_error({}, "a.b") == "a.b"
