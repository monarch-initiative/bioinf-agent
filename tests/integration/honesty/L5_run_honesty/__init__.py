"""
L5 — Workflow run honesty. The recorded run IS the truth. Tests ensure
detected_outputs come from inside project_root (not harness tmpfiles),
output_types lookups don't silently fall through to expected_type="any",
seal refuses on I3/I6/I7/I8 violations, and the soup-to-nuts lifecycle
ends in an honest WorkflowSpec.
"""
