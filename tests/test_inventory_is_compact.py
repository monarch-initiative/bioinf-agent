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

So: three warnings must survive compaction, and each is tested against a record built
to trigger it.
"""
from __future__ import annotations

import json

import pytest
import tiktoken

from agent.skills import resources

ENC = tiktoken.get_encoding("cl100k_base")


def _tok(o) -> int:
    return len(ENC.encode(json.dumps(o, default=str)))


class _Cache:
    """Minimal EnvCache stand-in: `all()` + `contract_report()` is the whole surface
    list_pipelines uses."""

    def __init__(self, records, violations=()):
        self._records = records
        self._violations = list(violations)

    def all(self):
        return self._records

    def contract_report(self, rec):
        viol = self._violations

        class _R:
            violations = viol
            unobserved = []

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
    assert _tok(compact) < _tok(detail) / 2, (
        f"compact {_tok(compact)} vs detail {_tok(detail)} — not worth the extra "
        f"parameter unless it is a real reduction")


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
