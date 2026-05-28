"""
L3 — Cache integrity (request_key / EnvCache / content_digest). A cache hit
must return the SAME artifact the user asked for — not a policy-distinct or
spelling-aliased neighbor that happened to share a key slot.
"""
