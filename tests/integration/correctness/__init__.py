"""
Non-honesty integration tests: capability coverage (install primitives,
state-management semantics) + plumbing (boundary coercion, process
lifecycle) + wire (MCP schema). These verify the system WORKS correctly —
they do NOT prevent the orchestrating agent from cheating.

If a test could be reframed as 'this prevents the agent from claiming X
when X isn't true', it's an honesty test and belongs under honesty/<L-level>/.
"""
