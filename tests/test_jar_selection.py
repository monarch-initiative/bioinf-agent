"""Jar selection — one rule, two languages, proven to agree on real trees.

THE BUG (Phase D, 2026-08-02). `install_jar_tool` picks the "primary" jar out of an
unpacked distribution, and it did so twice: `EnvManager._select_jar` in Python on the
host, and a shell one-liner baked into the image. The shell one drifted:

    find /opt/tools/exomiser -name '*.jar' | grep -i exomiser | head -n1

`grep` matches the whole PATH and the unpack destination is `/opt/tools/<name>`, so for
exomiser EVERY line contains "exomiser" — the filter selected nothing, `head -n1` took
find's directory order, and the shipped wrapper ran
`exomiser-cli-15.1.0/lib/ontologizer-0.0.1.jar`, a DEPENDENCY. The host picked correctly
throughout, so nothing on the validate side could see it. Only re-running the evidence
INSIDE the image that ships surfaced it, which is the entire point of VALIDATED_IN_IMAGE.

Two defects, not one: matching the path instead of the basename, AND no shortest-name
tiebreak. Either alone still picks a dependency jar out of exomiser's ~50-jar `lib/`.

WHY THIS FILE RUNS THE SHELL. The two implementations cannot share code across the
language boundary, so the only defence against them drifting again is to execute BOTH
over the SAME tree and require the same answer. `find` and `awk` are the same tools the
image has; no docker needed, which is what lets this live in the hermetic tier.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from agent.skills import install_commands as IC
from agent.skills.env_manager import EnvManager


#: The real thing: exomiser's zip unpacks a versioned dir whose `lib/` holds its
#: dependencies, every one of them under a path containing the tool name.
EXOMISER = [
    "exomiser-cli-15.1.0/exomiser-cli-15.1.0.jar",
    "exomiser-cli-15.1.0/lib/ontologizer-0.0.1.jar",
    "exomiser-cli-15.1.0/lib/exomiser-spring-data-genome-15.1.0.jar",
    "exomiser-cli-15.1.0/lib/HikariCP-4.0.3.jar",
    "exomiser-cli-15.1.0/lib/commons-csv-1.9.0.jar",
]

TREES = {
    "exomiser": (EXOMISER, "exomiser", "exomiser-cli-15.1.0.jar"),
    # A flat distribution, the easy shape — must not regress.
    "picard": (["picard.jar", "picard-lib.jar"], "picard", "picard.jar"),
    # NOTHING matches the tool name → fall back to the shortest basename overall,
    # rather than refusing. Same fallback the host has always had.
    "no-name-match": (["a/aaaa.jar", "b/bb.jar"], "gatk", "bb.jar"),
    # The tool name appears ONLY in a directory. The old grep matched these and the
    # new rule must not: a directory is not a filename.
    "name-only-in-dir": (["snpeff/core.jar", "snpeff/z.jar"], "snpeff", "z.jar"),
}


def _materialise(root: Path, rel_paths) -> None:
    for rel in rel_paths:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"PK\x03\x04")


def _shell_pick(root: Path, name: str) -> str:
    """Run the GENERATED shell — the bytes that go into the image — and return the pick.

    Both steps, because the shipped command is both:
        JAR="$(<select by name>)"; JAR="${JAR:-$(<select any>)}"
    Testing only the first would grade the name-matching half against fixtures where
    nothing matches, and call an empty answer a failure when the real command recovers."""
    def _run(sh: str) -> str:
        out = subprocess.run(["sh", "-c", sh], capture_output=True, text=True, timeout=30)
        assert out.returncode == 0, out.stderr
        return out.stdout.strip()

    picked = _run(IC._jar_select_sh(str(root), name)) or _run(IC._jar_select_sh(str(root), ""))
    return Path(picked).name if picked else ""


def _host_pick(root: Path, name: str) -> str:
    return EnvManager._select_jar(sorted(root.rglob("*.jar")), name).name


pytestmark = pytest.mark.skipif(shutil.which("awk") is None, reason="needs awk")


@pytest.mark.parametrize("case", list(TREES), ids=list(TREES))
def test_the_shipped_shell_picks_the_primary_jar(tmp_path, case):
    paths, name, expected = TREES[case]
    _materialise(tmp_path, paths)
    assert _shell_pick(tmp_path, name) == expected


@pytest.mark.parametrize("case", list(TREES), ids=list(TREES))
def test_the_shell_and_the_host_agree(tmp_path, case):
    """THE ANTI-DRIFT CHECK. Two implementations of one rule is this codebase's signature
    defect; they cannot share code across sh/Python, so they are made to answer the same
    question over the same bytes instead."""
    paths, name, _ = TREES[case]
    _materialise(tmp_path, paths)
    assert _shell_pick(tmp_path, name) == _host_pick(tmp_path, name)


def test_the_exact_regression_a_dependency_jar_is_never_the_wrapper_target(tmp_path):
    """The shipped exomiser wrapper ran ontologizer. Named on its own so the failure
    message says what actually went wrong."""
    _materialise(tmp_path, EXOMISER)
    assert _shell_pick(tmp_path, "exomiser") != "ontologizer-0.0.1.jar"


def test_selection_survives_find_returning_any_order(tmp_path):
    """`head -n1` made the answer depend on directory order — which is why this bug
    reproduced in the linux image and not on the dev host. Shortest-basename is a total
    order, so the pick is stable however `find` enumerates.

    MEASURED on the real 96-jar exomiser 15.1.0 tree: the old `grep -i exomiser` let
    through 96 OF 96 (it filtered nothing at all, because the parent directory is named
    exomiser), and reversing find's order moved the pick to `zstd-jni-1.5.7-3.jar`. On
    the dev host's filesystem the old rule happened to answer correctly, which is exactly
    how it survived to be shipped."""
    _materialise(tmp_path, EXOMISER)
    forward = _shell_pick(tmp_path, "exomiser")

    # Feed the SAME selector a deliberately hostile enumeration. Splicing at the pipe
    # keeps this honest: it is the shipped awk program being tested, not a copy.
    sh = IC._jar_select_sh(str(tmp_path), "exomiser")
    find_part, _, awk_part = sh.partition(" | ")
    out = subprocess.run(["sh", "-c", f"{find_part} | sort -r | {awk_part}"],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    assert Path(out.stdout.strip()).name == forward == "exomiser-cli-15.1.0.jar"


def test_a_tool_name_needing_shell_quoting_does_not_break_the_command(tmp_path):
    """The name reaches awk through `-v`; it is `shlex.quote`d for the same reason every
    other generated argument in this module is."""
    _materialise(tmp_path, ["it's a tool/x.jar", "it's a tool/tool-cli.jar"])
    assert _shell_pick(tmp_path, "tool") == "tool-cli.jar"


def test_the_generator_is_wired_into_the_real_jar_install_command():
    """A correct helper nothing calls is the shape of the original bug's sibling (the
    control experiment wired to one of two freeze paths). Assert the zip branch uses it
    and that the old path-matching form is gone for good."""
    cmd = IC.jar("exomiser", "https://example.org/exomiser-cli-15.1.0-distribution.zip",
                 evidence="exomiser --help")["command"]
    assert "awk -F/" in cmd, cmd
    assert "grep -i" not in cmd, "the path-matching grep is back"
    assert "head -n1" not in cmd, "the order-dependent head is back"
    # ...and the fallback is still a real second selection, not a silent empty JAR.
    assert 'JAR="${JAR:-$(' in cmd and 'test -n "$JAR"' in cmd
