"""
L6 — Provenance / authored artifacts. Agent-written files (driver scripts,
synthetic test inputs, hand-staged BAM/VCF) must be sha256-anchored at
stage time and re-verified at seal time, so the spec's claim about the file
keeps matching the bytes on disk.
"""
