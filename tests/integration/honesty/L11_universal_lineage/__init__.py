"""
L11 — Universal file-type-agnostic lineage. When a step's input path matches
a prior step's output, the on-disk bytes must match the producer's recorded
sha256 from run time. Catches an agent silently mutating a file between
steps. File-type-agnostic by design — works for BAM, VCF, parquet, .weird,
anything. Per-file-type lineage (PG chains, sample IDs) stays optional.
"""
