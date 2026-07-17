"""Shared EnvCache record fixtures — shapes that can actually exist in production.

Imported as `from env_records import ...` (not `tests.env_records`): `tests/` has no
`__init__.py`, so pytest puts it on sys.path directly, and an unrelated `tests` package
in site-packages shadows the dotted form.

WHY THIS FILE EXISTS. Fixtures across the suite hand-rolled EnvCache records as
`{"image": ..., "image_digest": ...}` — a shape NO producer can emit, because all three
of them gate on `env_honesty.check_build`, which requires in-image evidence. Asserting
against records that cannot occur is how the cache-hit hole survived 1297 green tests:
the tests agreed with each other that an evidence-free record was normal, so nothing
objected when `lookup_anchored` served one as `proven` (audit 2026-07-16).

Build VIOLATING records deliberately — `env_record(verifications=[])` — to assert a
refusal. Never build one by accident.
"""
from __future__ import annotations


def env_evidence(tool: str = "samtools") -> list[dict]:
    """In-image evidence that satisfies VALIDATED_IN_IMAGE: names the tool as a real
    word-boundary token (not an echo/print cheat) and passed."""
    return [{"label": tool, "tool": tool, "check": f"{tool} --version",
             "rc": 0, "passed": True, "out": f"{tool} 1.21"}]


def shipped_binary(tool: str, *, version=None, provenance=None, install_command=None,
                   tier=None, verified=None, assurance=None) -> dict:
    """One `shipped_binaries[]` entry, CONSTRUCTED THROUGH THE MODEL so a fixture can
    never encode a shape the producers don't emit.

    This helper exists because the suite proved the point the hard way. Four fixtures
    hand-rolled four mutually exclusive dialects and all four were green:

        test_env_recipe_render.py:71  {"command": "bcftools", "version": …}
        test_invariants.py:3444       {"name": "seqkit (release binary)", "command": "curl …"}
        test_invariants.py:4732       {"name": "seqtk (synthesized @ …)", "command": "git clone …"}
        html_structure.py:155         {"name": "samtools (conda)", "command": "conda install …"}

    Meanwhile the shape `freeze_from_image` ACTUALLY emitted was exercised by NO test.
    The suite was 100% green on a record shape that did not exist and blind to the one
    that did — which is how the ENV report came to cite htslib's version for bcftools.
    An untyped dict makes the fixture the schema, and every test file writes its own.

    Go through here. `ShippedBinary(extra="forbid")` rejects a key no producer writes.
    """
    from agent.models.core_data import ShippedBinary
    return ShippedBinary(tool=tool, version=version, provenance=provenance,
                         install_command=install_command, tier=tier,
                         verified=verified, assurance=assurance).model_dump()


def env_record(**overrides) -> dict:
    """An EnvCache record that satisfies the Layer-1 honesty contract."""
    rec = {
        "image": "demo:1.0",
        "image_digest": "sha256:img",
        "verifications": env_evidence(),
    }
    rec.update(overrides)
    return rec
