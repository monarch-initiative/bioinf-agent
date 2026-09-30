#!/usr/bin/env bash
# activate.sh — put this checkout's runtime env on PATH for the current shell:
#
#     source /path/to/bioinf_agent/scripts/activate.sh
#
# The runtime env (built by setup.sh; _env.sh is the one place its path is spelled)
# carries nextflow and its own Java, so a rendered pipeline runs on this machine with
# nothing else installed. Nextflow's launcher prefers JAVA_HOME / JAVA_CMD over the
# JDK beside it, so both are pointed at the runtime env's JDK: a stray system Java
# cannot answer for it. Sourced, never executed; bash and zsh.
source "$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)/_env.sh"
export PATH="$BIOINF_RUNTIME/bin:$PATH"
export JAVA_HOME="$BIOINF_RUNTIME/lib/jvm"
export JAVA_CMD="$JAVA_HOME/bin/java"
