"""`list_installed_pipelines` must be cheap to call, and must not get cheap by lying.

The protocol tells an agent to call this FIRST, before solving a tool again. It was
~811 tokens per env+workflow pair — 5,622 for a 7-env tree, and a LINEAR 40,000 at 50
envs, to answer "do I already have this?". In one real session it was called 9 times:
31,142 tokens of the same inventory. The cliff arrives exactly as the tool becomes
useful, which is the worst possible time.

Compaction is easy to do dishonestly, and this file is mostly about that half. Dropping
a field that is absent or at its default is compression. Dropping a field that carries a
WARNING — a contract violation, a version that diverges from what was requested, the
reason a how-to was never proven — is a smaller payload that means something different,
and it is the exact substitution ("never present a request as an observation") the
report-honesty work exists to prevent.

So: four warnings must survive compaction, and each is tested against a record built
to trigger it. The fourth arrived last and is the subtlest — not a failure but a PASS
that rests on less, which `contract_ok: true` cannot express and which detail=True had
been disclosing to almost nobody.
"""
from __future__ import annotations

import json

import pytest

from agent.skills import resources


def _size(o) -> int:
    """Serialised length. A CHARACTER count, not a token count, deliberately.

    The claim under test is a RATIO — "compact is much smaller than detail" — and
    characters carry that just as well as tokens for JSON of the same kind. The first
    version of this file imported tiktoken for it and went red in CI, because tiktoken
    is a local analysis tool that nothing declares. Reaching for a dev-only dependency
    to compute a ratio is a bad trade; the exact token figures belong in the commit
    message, where they were measured once, not in a test that has to run everywhere.
    """
    return len(json.dumps(o, default=str))


class _Cache:
    """Minimal EnvCache stand-in: `all()` + `contract_report()` is the whole surface
    list_pipelines uses."""

    def __init__(self, records, violations=(), unobserved=()):
        self._records = records
        self._violations = list(violations)
        self._unobserved = list(unobserved)

    def all(self):
        return self._records

    def contract_report(self, rec):
        viol = self._violations
        unobs = self._unobserved

        class _R:
            violations = viol
            unobserved = unobs

            def summary(self):
                return {"observed": 3, "total": 4}
        return _R()


def _cfg(tmp_path):
    return {"paths": {"pipelines_dir": str(tmp_path)}}


def _rec(**over):
    r = {"name": "envA", "requested_tools": ["samtools=1.21"], "image": "img:1",
         "image_digest": "sha256:" + "a" * 64, "content_digest": "sha256:" + "b" * 64,
         "build_method": "container-native", "platform": "linux/amd64",
         "validation_locus": "native", "created_at": "2026-01-01T00:00:00+00:00",
         "resolved_packages": [{"name": "samtools", "version": "1.21"}]}
    r.update(over)
    return r


def test_compact_is_the_default_and_is_much_smaller(tmp_path):
    cache = _Cache({"samtools=1.21|linux/amd64|none": _rec()})
    compact = resources.list_pipelines(_cfg(tmp_path), env_cache=cache)
    detail = resources.list_pipelines(_cfg(tmp_path), env_cache=cache, detail=True)
    assert _size(compact) < _size(detail) / 2, (
        f"compact {_size(compact)} vs detail {_size(detail)} chars — not worth the "
        f"extra parameter unless it is a real reduction")


def test_compact_still_answers_the_question_it_exists_for(tmp_path):
    """'Do I already have this, and would it still be served?' — the request_key to
    match against, the tools, and the earned contract verdict."""
    cache = _Cache({"samtools=1.21|linux/amd64|none": _rec()})
    row = resources.list_pipelines(_cfg(tmp_path), env_cache=cache)["envs"][0]
    assert row["request_key"] == "samtools=1.21|linux/amd64|none"
    assert row["contract_ok"] is True
    assert any("samtools" in str(t) for t in row["tools"])
    assert any("1.21" in str(t) for t in row["tools"]), "the version must survive"


def test_detail_still_carries_the_digests(tmp_path):
    """The opt-in must actually opt IN to something — these are what a caller pinning
    an env by digest needs."""
    cache = _Cache({"k": _rec()})
    row = resources.list_pipelines(_cfg(tmp_path), env_cache=cache, detail=True)["envs"][0]
    for k in ("image_digest", "content_digest", "platform", "validation_locus",
              "created_at", "contract_coverage"):
        assert k in row, f"detail=True dropped {k}"


# --- the three warnings that must survive compaction -------------------------------

def test_a_contract_violation_is_never_compacted_away(tmp_path):
    """`contract_ok: false` with no clause sends the reader back for a second call to
    find out what is wrong with an artifact we just told them not to trust."""
    cache = _Cache({"k": _rec()}, violations=["I13.gated_license_recorded"])
    row = resources.list_pipelines(_cfg(tmp_path), env_cache=cache)["envs"][0]
    assert row["contract_ok"] is False
    assert row["contract_violations"] == ["I13.gated_license_recorded"]


def test_a_diverging_tool_version_is_never_compacted_away(tmp_path):
    """The compact tool form is the string "samtools 1.21". A tool whose INSTALLED
    version differs from what was REQUESTED keeps its full record instead — that flag is
    the entire reason the row exists, and collapsing it to one version string would be
    presenting one number where two disagreed."""
    rec = _rec(requested_tools=["samtools=1.21"],
               resolved_packages=[{"name": "samtools", "version": "1.19"}])
    row = resources.list_pipelines(_cfg(tmp_path), env_cache=_Cache({"k": rec}))["envs"][0]
    entry = row["tools"][0]
    assert isinstance(entry, dict), f"a divergence was flattened to a string: {entry!r}"
    assert entry["diverges"] is True
    assert entry["requested"] == "1.21" and entry["installed"] == "1.19"


def test_an_unverified_how_to_keeps_its_reason(tmp_path):
    """`not_attempted` alone cannot distinguish "nobody authored a usage block" from
    "it spans two images and structurally cannot be self-tested" — two very different
    facts about the same artifact."""
    (tmp_path / "w.workflow.yaml").write_text(
        "workflow_name: w\n"
        "env_request_key: k\n"
        "pipeline_steps: []\n"
        "usage_verification:\n"
        "  status: not_attempted\n"
        "  reason: the workflow spans two images\n")
    row = resources.list_pipelines(_cfg(tmp_path), env_cache=_Cache({}))["workflows"][0]
    assert row["usage_verification_status"] == "not_attempted"
    assert "two images" in row["usage_verification_reason"]


def test_a_verified_how_to_does_not_carry_a_redundant_reason(tmp_path):
    """The other direction — compaction has to actually compact in the common case."""
    (tmp_path / "w.workflow.yaml").write_text(
        "workflow_name: w\n"
        "env_request_key: k\n"
        "pipeline_steps: []\n"
        "usage_verification:\n"
        "  status: verified\n"
        "  reason: I4 executed every trial\n")
    row = resources.list_pipelines(_cfg(tmp_path), env_cache=_Cache({}))["workflows"][0]
    assert row["usage_verification_status"] == "verified"
    assert "usage_verification_reason" not in row


def test_a_green_that_proved_less_says_so_in_compact_form(tmp_path):
    """The fourth warning, added after it was measured missing (G3 phase 5).

    `contract_ok` is a two-state answer to a three-state question: a clause can pass,
    fail, or have had NOTHING TO LOOK AT. Only the first two reach the boolean. detail=True
    has carried `contract_unobserved` since it was written — with a comment saying an
    inventory that hid it "would be making exactly the claim this field exists to
    qualify" — but the compact branch is the DEFAULT, so the disclosure lived on the path
    almost nobody takes.

    Measured: `repeatmasker_ancient` froze `outcome: degraded` with
    `VALIDATED_IN_IMAGE.discriminates` unobserved — nothing established its evidence would
    FAIL in an image lacking the tool — and its inventory row was an unqualified
    `contract_ok: true`. Across the real corpus the field separates 4 fully-proven envs
    from 11 that were previously indistinguishable from them.
    """
    class _Clause:
        def __init__(self, clause, establishes):
            self.clause, self.establishes = clause, establishes

    cache = _Cache({"k": _rec()}, unobserved=[
        _Clause("VALIDATED_IN_IMAGE.discriminates", "assurance"),
        _Clause("WELL_FORMED.shipped_binaries", "disclosure"),
    ])
    row = resources.list_pipelines(_cfg(tmp_path), env_cache=cache)["envs"][0]
    assert row["contract_ok"] is True, "this is a PASS — the point is that it proved less"
    assert row["assurance_unproven"] == ["VALIDATED_IN_IMAGE.discriminates"]
    assert "WELL_FORMED.shipped_binaries" not in row["assurance_unproven"], (
        "disclosure shortfalls stay on detail=True: shipped_binaries is unobserved on "
        "essentially every adopt record, and a field that is always present carries no "
        "signal and trains the reader to skip it")


def test_a_fully_observed_env_carries_no_unproven_field(tmp_path):
    """The other direction — compaction has to actually compact in the common case, and
    an always-present warning is one nobody reads."""
    row = resources.list_pipelines(_cfg(tmp_path),
                                   env_cache=_Cache({"k": _rec()}))["envs"][0]
    assert "assurance_unproven" not in row


def test_a_gated_env_says_so_in_compact_form(tmp_path):
    """I13's subject. A licence-gated artifact must not look unremarkable in the
    inventory just because we were saving tokens."""
    cache = _Cache({"k": _rec(license_gated=True)})
    row = resources.list_pipelines(_cfg(tmp_path), env_cache=cache)["envs"][0]
    assert row["license_gated"] is True


@pytest.mark.parametrize("detail", [False, True])
def test_steps_validated_is_correct_in_both_forms(detail, tmp_path):
    """The regression that reported `steps_validated: 0` for every workflow ever sealed
    lived in exactly this row. Adding a second rendering of the row is a fresh chance to
    reintroduce it, so both forms are checked against the NORMAL validation shape (per-
    file records) rather than the agent override that almost no real step has."""
    (tmp_path / "w.workflow.yaml").write_text(
        "workflow_name: w\n"
        "env_request_key: k\n"
        "pipeline_steps:\n"
        "  - step: 1\n"
        "    validation:\n"
        "      /out/a.bam: {passed: true}\n"
        "  - step: 2\n"
        "    validation_status: passed\n"
        "  - step: 3\n")
    row = resources.list_pipelines(_cfg(tmp_path), env_cache=_Cache({}),
                                   detail=detail)["workflows"][0]
    assert (row["steps_validated"], row["steps_total"]) == (2, 3)
