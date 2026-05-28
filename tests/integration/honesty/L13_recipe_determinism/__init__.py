"""
L13 — Recipe-replay determinism. Same recipe inputs (lock + longtail +
platform + engine + base_image + apt_snapshot) → byte-identical
content_digest. Pure-function variant ships here; full-rebuild variant is
reserved for the integration_docker_slow tier (manual, pre-release).
"""
