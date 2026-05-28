"""
F0 (batch-2 Apollo3 stress): some MCP clients wire-encode array arguments
as JSON-encoded strings rather than literal arrays. FastMCP's Pydantic
validator refuses string-when-list-expected and drops the call. The
boundary coercer normalises the shape so the primitive's contract stays
`list[str]` regardless of transport quirks.

Shapes the boundary must accept:
  - list                          → list (passthrough)
  - JSON-encoded list string      → parsed list
  - bare non-empty string         → [single-item]
  - empty string                  → []
  - None                          → None (Optional surface)

Why integration, not unit: this is a wire-layer concern with no logic
beyond shape-coercion. A regression would be silent — a string-encoded
list silently falls through as a string and Pydantic refuses one tier
later, dropping the call. The integration value is exercising every
shape an agent might send and asserting the post-coerce shape is what
the primitive will see.
"""
from __future__ import annotations

import pytest

from agent.mcp_server import _coerce_str_list


@pytest.mark.integration
def test_list_passes_through():
    assert _coerce_str_list(["a", "b"]) == ["a", "b"]
    assert _coerce_str_list([]) == []


@pytest.mark.integration
def test_json_encoded_list_string_is_parsed():
    """The headline F0 case: some MCP clients send `'["MIT", "GPL-3.0"]'`
    instead of `["MIT", "GPL-3.0"]`."""
    assert _coerce_str_list('["MIT", "GPL-3.0"]') == ["MIT", "GPL-3.0"]
    assert _coerce_str_list("[]") == []


@pytest.mark.integration
def test_bare_string_becomes_single_item_list():
    """A bare non-empty string is interpreted as a one-element list. An
    agent that passes a single value as a scalar (rather than wrapping in
    a list) is accommodated."""
    assert _coerce_str_list("MIT") == ["MIT"]


@pytest.mark.integration
def test_none_passes_through():
    """Optional surfaces keep None semantics — distinct from `[]`."""
    assert _coerce_str_list(None) is None


@pytest.mark.integration
def test_empty_string_becomes_empty_list():
    """A whitespace-only string is the agent's "I have no values" signal;
    coerce to `[]` rather than `['']` (which would land in `licenses[]` as
    a bogus empty-string license)."""
    assert _coerce_str_list("") == []
    assert _coerce_str_list("   ") == []


@pytest.mark.integration
def test_malformed_json_string_falls_through_as_single_item():
    """If the string looks like JSON but doesn't parse, the coercer must
    NOT raise — it falls back to single-item behaviour. The primitive's
    later validation (e.g. an unknown license string) will report a real
    error; this layer is shape-only."""
    # `[unclosed` is bracket-prefixed but invalid JSON → treat as a bare string
    result = _coerce_str_list("[unclosed")
    assert result == ["[unclosed"], f"unexpected fallback: {result!r}"
