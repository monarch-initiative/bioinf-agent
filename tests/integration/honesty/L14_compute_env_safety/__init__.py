"""
L14 — Compute-env command surface. The agent's interaction with a user's
compute env (cluster, laptop) is mediated by a fixed, small set of primitives,
each gated by a per-directory permission declared in
~/.bioinf/projects_access.yaml. Tests here pin the exact shape of every shell
invocation, the fail-closed default, and the absence of any unwhitelisted
subprocess call.

The L14 contract is the security boundary for "the agent on your cluster":
no arbitrary commands, no unauthorized paths, no permission escalation, no
overwrite of existing files (planned for the upload primitive).
"""
