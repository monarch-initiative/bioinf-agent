"""
N3 (batch-3 Apollo3 stress): wrapper-tier evidence must SMOKE-TEST the
wrapped binary, not just check that the wrapper file exists.

Pre-fix, the default evidence for `script_repo` / `source` / `cargo` / `go`
was a bare `command -v {wrap}`. Apollo3's `command -v apollo3` passed even
though running `apollo3` raised `Cannot find module .../dist/main.js` —
structurally the same cheat shape that the contract already rejects for
`echo` / `true` / `:`, just one tier up.

Why integration, not unit: the unit suite could grep the evidence string,
but only the agent's choice to upgrade the FOUR generators consistently
matters — a test that pins one generator while another regresses misses
the whole point. This single integration sweep asserts the contract holds
across all four tiers.
"""
from __future__ import annotations

import pytest

from agent.skills import install_commands as ic


def _smoke_chain_for(binary: str, *, evidence: str) -> bool:
    """A smoke chain references --help/--version/-h as well as command -v,
    not bare command -v alone. We accept any ordering as long as the three
    invocation probes are present alongside command -v."""
    return all(probe in evidence for probe in (
        f"{binary} --help",
        f"{binary} --version",
        f"{binary} -h",
        f"command -v {binary}",
    ))


@pytest.mark.integration
def test_script_repo_default_evidence_is_a_smoke_chain():
    spec = ic.script_repo("apollo3", "https://example.org/apollo3.git",
                          ref="abc123", script_rel="dist/main.js",
                          interpreter="node")
    assert _smoke_chain_for("apollo3", evidence=spec["evidence"]), \
        f"script_repo default evidence not a smoke chain: {spec['evidence']!r}"


@pytest.mark.integration
def test_source_default_evidence_is_a_smoke_chain():
    spec = ic.source("seqtk", "https://github.com/lh3/seqtk",
                     ref="v1.4", bin_path="seqtk")
    assert _smoke_chain_for("seqtk", evidence=spec["evidence"]), \
        f"source default evidence not a smoke chain: {spec['evidence']!r}"


@pytest.mark.integration
def test_cargo_default_evidence_is_a_smoke_chain():
    spec = ic.cargo("ripgrep", "ripgrep", version="14.1.0")
    assert _smoke_chain_for("ripgrep", evidence=spec["evidence"]), \
        f"cargo default evidence not a smoke chain: {spec['evidence']!r}"


@pytest.mark.integration
def test_go_default_evidence_is_a_smoke_chain():
    spec = ic.go("gh", "github.com/cli/cli/v2/cmd/gh", binary_name="gh")
    assert _smoke_chain_for("gh", evidence=spec["evidence"]), \
        f"go default evidence not a smoke chain: {spec['evidence']!r}"


@pytest.mark.integration
def test_caller_supplied_evidence_still_wins():
    """The fix preserves the override — if the caller passes evidence, the
    generator does NOT replace it with the smoke chain. The contract is a
    DEFAULT, not a coercion."""
    spec = ic.source("acme", "https://example.org/acme.git", ref="v1",
                     bin_path="acme", evidence="acme --selftest")
    assert spec["evidence"] == "acme --selftest"
