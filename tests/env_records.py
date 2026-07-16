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


def env_record(**overrides) -> dict:
    """An EnvCache record that satisfies the Layer-1 honesty contract."""
    rec = {
        "image": "demo:1.0",
        "image_digest": "sha256:img",
        "verifications": env_evidence(),
    }
    rec.update(overrides)
    return rec
