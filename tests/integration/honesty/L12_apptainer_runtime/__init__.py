"""
L12 — Apptainer/Singularity runtime. The shipped .sif must actually exec
under the HPC consumer's apptainer. Tests here exercise the conversion
(docker save → apptainer build docker-archive) and a smoke run inside the
.sif. Skipped on hosts without apptainer; HPC consumers + CI runners execute.
"""
