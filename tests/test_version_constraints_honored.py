"""A version constraint the agent asked with is the version the record requests.

`install_conda_packages("notebook<7")` records `version: "7"` with
`version_constraint: "<"`. The constraint travels: the engine is asked for
`notebook<7`, the recipe says `notebook<7`, a request key built from it says
`notebook<7`, and the ENV report's requested column reads `<7` and flags an
installed 7.6.2 as diverging. Nothing along the way rewrites the ask into the pin
`notebook=7`, which is a different request and one the user never made.
"""
from __future__ import annotations

import pytest

from agent.skills import freeze
from agent.skills.env_report_helpers import (constraint_holds, requested_versions,
                                             split_spec, version_divergences,
                                             versions_diverge)
from agent.skills.env_report_html import render_env_report_html
from env_records import env_evidence, env_record


def _draft(*pkgs: dict) -> dict:
    return {"install_steps": [{"tool": "conda", "subcommand": "install", "returncode": 0,
                               "installed_packages": list(pkgs)}]}


# ---------------------------------------------------------------------------
# the specs handed to the engine
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("op", ["<", ">=", "!=", "<=", ">"])
def test_requested_conda_specs_keeps_the_operator_it_was_asked_with(op):
    draft = _draft({"name": "notebook", "version": "7", "version_constraint": op})
    assert freeze.requested_conda_specs(draft) == [f"notebook{op}7"]


def test_a_pin_still_reads_as_a_pin_and_a_bare_name_as_a_name():
    draft = _draft({"name": "samtools", "version": "1.21"}, {"name": "bcftools"})
    assert freeze.requested_conda_specs(draft) == ["samtools=1.21", "bcftools"]


def test_a_retry_supersedes_the_constraint_it_replaces():
    """Move-to-end dedup by name, unchanged: the retried install's spec wins."""
    draft = {"install_steps": [
        {"tool": "conda", "subcommand": "install", "returncode": 0,
         "installed_packages": [{"name": "notebook", "version": "7", "version_constraint": "<"}]},
        {"tool": "conda", "subcommand": "install", "returncode": 0,
         "installed_packages": [{"name": "notebook", "version": "6.5", "version_constraint": ">="}]},
    ]}
    assert freeze.requested_conda_specs(draft) == ["notebook>=6.5"]


def test_the_request_key_carries_a_constraint_as_the_spec_it_came_from():
    key = freeze.request_key([("notebook", "<7"), ("samtools", "1.21")], "linux-64")
    assert key.startswith("notebook<7,samtools=1.21|"), key


# ---------------------------------------------------------------------------
# the requested column of the ENV report
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("tok,want", [
    ("samtools=1.21", ("samtools", "1.21")),
    ("bwa==0.7.17", ("bwa", "0.7.17")),
    ("notebook<7", ("notebook", "<7")),
    ("numpy>=1.2", ("numpy", ">=1.2")),
    ("x!=3", ("x", "!=3")),
    ("fastqc", ("fastqc", "")),
])
def test_split_spec_keeps_a_constraint_and_drops_an_equality(tok, want):
    assert split_spec(tok) == want


def test_requested_versions_reads_the_constraint_off_the_conda_specs():
    """The request_key token is the version the install record RESOLVED; the conda
    spec is the literal ask. When the ask was a constraint, that is what the reader
    is shown."""
    rec = {"request_key": "notebook=7|linux/amd64|none", "conda_specs": ["notebook<7"]}
    assert requested_versions(rec) == {"notebook": "<7"}


def test_a_plain_conda_spec_does_not_override_the_request_key_pin():
    rec = {"request_key": "samtools=1.21|linux/amd64|none", "conda_specs": ["samtools=999"]}
    assert requested_versions(rec)["samtools"] == "1.21"


@pytest.mark.parametrize("requested,installed,holds", [
    ("<7", "7.6.2", False), ("<7", "6.5.4", True),
    (">=1.2", "1.21", True), (">=1.2", "1.1", False),
    ("!=3", "3.0", False), ("!=3", "3.1", True),
    ("<=1.21", "1.21", True), (">7", "7.0.1", True),
    ("~=1.4", "1.5", True), ("~=1.4", "2.0", False),
])
def test_constraint_holds(requested, installed, holds):
    assert constraint_holds(requested, installed) is holds


def test_a_value_that_is_not_a_constraint_or_not_a_version_is_never_a_verdict():
    assert constraint_holds("1.21", "1.21") is None
    assert constraint_holds("<7", "notebook, build unknown") is None
    assert versions_diverge("<7", "notebook, build unknown") is False


def test_versions_diverge_evaluates_a_constraint():
    assert versions_diverge("<7", "7.6.2") is True
    assert versions_diverge("<7", "6.5.4") is False
    assert versions_diverge(">=1.2", "1.21") is False
    assert versions_diverge("!=3", "3.0") is True


def test_the_env_report_flags_an_installed_version_outside_the_constraint():
    rec = env_record(name="nb", requested_tools=["notebook"],
                     request_key="notebook=7|linux/amd64|none", conda_specs=["notebook<7"],
                     resolved_packages=[{"name": "notebook", "version": "7.6.2", "kind": "conda"}],
                     verifications=env_evidence("notebook"))
    assert version_divergences(rec) == [{"tool": "notebook", "requested": "<7", "installed": "7.6.2"}]
    html = render_env_report_html(rec)
    assert "Version check" in html
    assert "requested &lt;7" in html


def test_an_installed_version_inside_the_constraint_is_not_flagged():
    rec = env_record(name="nb", requested_tools=["notebook"],
                     request_key="notebook=6|linux/amd64|none", conda_specs=["notebook<7"],
                     resolved_packages=[{"name": "notebook", "version": "6.5.4", "kind": "conda"}],
                     verifications=env_evidence("notebook"))
    assert version_divergences(rec) == []
    assert "Version check" not in render_env_report_html(rec)
