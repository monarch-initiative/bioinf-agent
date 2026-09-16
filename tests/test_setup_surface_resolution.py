"""The setup surface answers "where is conda?" in exactly ONE place.

WHY THIS FILE EXISTS. Four scripts each carried their own hand-maintained list of
conda locations — setup.sh, doctor.py, start_mcp_server.sh, setup_core_test_data.sh —
and two of them had already DIVERGED on order: setup.sh searched the repo-local
private conda LAST, doctor.py searched it FIRST. On a machine carrying both a private
./.miniforge and an off-PATH system conda they resolve to different binaries, so the
systems check could report PASS for a conda that setup never used. A check that
describes something other than what happened is the defect this whole repo is built to
refuse, and it had reached the first script a new user runs.

The fix was structural (scripts/_env.sh, one implementation and four callers) and this
lint is what keeps it that way — the shell-layer form of
tests/test_one_reading_per_field.py. The failure mode it guards is not malice but
convenience: the next person who needs a conda path in a new script will copy five
lines rather than source one file, and nothing else in the build would notice.

SCOPE: scripts/ only. agent/ resolves conda through shutil.which at runtime (PATH is
set up by the launcher), which is a different mechanism and deliberately not policed
here.
"""
from __future__ import annotations

import os
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
ENV_SH = SCRIPTS / "_env.sh"

#: Substrings that only ever appear in a hand-rolled conda/interpreter SEARCH list.
#: Deliberately not ".miniforge" (the repo-local install TARGET, which setup.sh must
#: name to create it) and not "Miniforge3-" (the installer download URL).
_SEARCH_TOKENS = (
    "miniconda3",
    "anaconda3",
    "/opt/conda",
    "/opt/homebrew/opt/miniforge3",
    "$HOME/miniforge3",
)

#: Constructing the runtime interpreter's path, as opposed to merely naming
#: ".conda_runtime" in a message to the user (which any script may do).
_INTERPRETER_PATH = ("conda_runtime/bin", '".conda_runtime" / "bin"')


def _surface_files() -> list[Path]:
    return sorted(p for p in SCRIPTS.iterdir()
                  if p.is_file() and p.suffix in {".sh", ".py"} and p != ENV_SH)


def test_env_sh_actually_holds_the_search_lists():
    """Falsifiability: if _env.sh stopped carrying them, every assertion below would
    pass vacuously and this file would be a lint comparing against nothing."""
    assert ENV_SH.is_file(), f"{ENV_SH} missing — it is the single resolver"
    text = ENV_SH.read_text()
    missing = [t for t in _SEARCH_TOKENS if t not in text]
    assert not missing, (
        f"_env.sh no longer contains {missing}, so this lint proves nothing. Either the "
        f"search moved (point this test at its new home) or it was deleted (say so).")


@pytest.mark.parametrize("path", _surface_files(), ids=lambda p: p.name)
def test_no_second_conda_search_list_in_the_setup_surface(path):
    text = path.read_text()
    found = [t for t in _SEARCH_TOKENS if t in text]
    assert not found, (
        f"{path.name} spells out conda search locations {found}.\n\n"
        f"That is a SECOND answer to a question scripts/_env.sh already answers, and "
        f"the last time two answers existed they disagreed on order — the doctor "
        f"reported PASS on a conda setup had not used.\n"
        f"Fix: shell — `source \"$(dirname \"${{BASH_SOURCE[0]}}\")/_env.sh\"` then call "
        f"bioinf_find_conda / bioinf_conda_on_path. Python — shell out to "
        f"`bash scripts/_env.sh conda`, the way doctor.py does.")


@pytest.mark.parametrize("path", _surface_files(), ids=lambda p: p.name)
def test_the_runtime_interpreter_path_is_not_respelled(path):
    """`.conda_runtime/bin/python` is a literal any script could retype, and a rename
    would then leave one caller pointed at nothing. doctor.py is allowed exactly one —
    its documented fallback for a checkout where _env.sh itself is missing."""
    text = path.read_text()
    hits = sum(text.count(t) for t in _INTERPRETER_PATH)
    allowed = 1 if path.name == "doctor.py" else 0
    assert hits <= allowed, (
        f"{path.name} constructs the runtime interpreter path {hits} time(s) "
        f"(allowed: {allowed}). Use $BIOINF_RUNTIME_PY after sourcing _env.sh, or ask "
        f"`bash scripts/_env.sh runtime-python` — it prints the path whether or not the "
        f"env exists, so a missing runtime can still be named.")


@pytest.mark.parametrize(
    "path", [p for p in _surface_files() if p.suffix == ".sh"], ids=lambda p: p.name)
def test_every_conda_using_shell_script_sources_the_resolver(path):
    text = path.read_text()
    if "conda" not in text:
        pytest.skip(f"{path.name} does not touch conda")
    assert "_env.sh" in text, (
        f"{path.name} uses conda but never sources scripts/_env.sh, so it is resolving "
        f"conda some other way. Add: "
        f"source \"$(cd \"$(dirname \"${{BASH_SOURCE[0]}}\")\" && pwd)/_env.sh\"")


def test_the_doctor_delegates_rather_than_searching():
    """The doctor is the row a user reads to decide the machine is healthy, so it is
    the one place a divergent answer does the most damage."""
    text = (SCRIPTS / "doctor.py").read_text()
    assert "_env.sh" in text and "conda" in text, (
        "doctor.py must ask scripts/_env.sh for conda, not carry its own search")


# ---------------------------------------------------------------------------
# The workspace prompt — setup must never PICK where a user's records live
# ---------------------------------------------------------------------------
#
# Decision 8 of the workspace split: "setup always asks and never picks
# silently", because silent picking is what produced CS55 (the config menu wrote
# to one path while the doctor read another, and three surfaces reported valid
# for a file the agent could not see).
#
# The first implementation broke it in one clause. `[ ! -t 0 ]` was meant to
# handle CI, and it made ANY non-terminal stdin take the default — so a piped
# answer was read, discarded, and replaced by a guess, and the two refusals
# below never ran at all. Driven rather than grepped: the bug was in what the
# script DID with a correct-looking condition.

_WS_START = "# --- 1. workspace ---"
_WS_END = "# --- 2. runtime env ---"


def _workspace_block() -> str:
    """The prompt, lifted out of setup.sh so it can be driven without running a
    setup. Extracted by marker rather than copied, so it cannot drift."""
    text = (SCRIPTS / "setup.sh").read_text()
    start, end = text.index(_WS_START), text.index(_WS_END)
    return text[start:end]


def _drive(tmp_path, answer: str | None, *, assume_yes: bool = False,
           checkout_under_home: bool = False) -> subprocess.CompletedProcess:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    checkout = (home if checkout_under_home else tmp_path) / "checkout"
    checkout.mkdir(parents=True, exist_ok=True)
    block = tmp_path / "block.sh"
    block.write_text(_workspace_block())
    driver = tmp_path / "drive.sh"
    driver.write_text(textwrap.dedent(f"""
        set -euo pipefail
        say() {{ echo "[setup] $*"; }}
        PROJECT_ROOT="{checkout}"
        ASSUME_YES="{'1' if assume_yes else '0'}"
        source "{block}"
        echo "CHOSE:$(grep -v '^#' "$POINTER" | tail -1)"
    """))
    return subprocess.run(
        ["bash", str(driver)],
        input="" if answer is None else answer + "\n",
        capture_output=True, text=True, timeout=30,
        env={"HOME": str(home), "PATH": os.environ["PATH"]},
    )


def _chosen(r: subprocess.CompletedProcess) -> str:
    for line in r.stdout.splitlines():
        if line.startswith("CHOSE:"):
            return line[len("CHOSE:"):]
    return ""


def test_a_typed_answer_is_the_one_recorded(tmp_path):
    """THE REGRESSION. An answer arriving on a pipe is an answer; `read` handles
    a pipe exactly as it handles a terminal. Discarding it for the default means
    setup decided where the user's records live and told them it was their choice."""
    r = _drive(tmp_path, str(tmp_path / "home" / "mywork"))
    assert r.returncode == 0, r.stderr
    assert _chosen(r) == str(tmp_path / "home" / "mywork"), (
        "setup ignored the answer it was given and took its own default")


def test_bare_enter_accepts_the_shown_default(tmp_path):
    """Not silent: the prompt PRINTS the default, so pressing Enter is consent."""
    r = _drive(tmp_path, "")
    assert r.returncode == 0, r.stderr
    assert _chosen(r) == str(tmp_path / "home" / "bioinf_agent")


def test_a_tilde_answer_is_expanded(tmp_path):
    """`read` does no expansion, so `~/work` would otherwise be recorded verbatim
    and every later resolution would look for a directory literally named `~`."""
    r = _drive(tmp_path, "~/tildework")
    assert r.returncode == 0, r.stderr
    assert _chosen(r) == str(tmp_path / "home" / "tildework")


def test_no_answer_and_no_flag_refuses_rather_than_guessing(tmp_path):
    """End of input means nobody is there to ask. Refuse — and name BOTH ways a
    scripted install can state its intent, because a refusal that doesn't say
    what to do instead just relocates the dead end."""
    r = _drive(tmp_path, None)
    assert r.returncode == 2
    assert "BIOINF_WORKSPACE=" in r.stderr and "--yes" in r.stderr


def test_the_yes_flag_is_consent_to_the_default(tmp_path):
    """The escape hatch the refusal names has to actually work."""
    r = _drive(tmp_path, None, assume_yes=True)
    assert r.returncode == 0, r.stderr
    assert _chosen(r) == str(tmp_path / "home" / "bioinf_agent")


def test_a_workspace_outside_home_is_refused(tmp_path):
    """Docker Desktop shares a fixed set of host prefixes with its VM; a path
    outside them bind-mounts EMPTY rather than failing, so an env frozen there
    would validate against nothing. Same rule as
    workspace.home_containment_error, which the doctor uses."""
    r = _drive(tmp_path, "/opt/nowhere")
    assert r.returncode == 2
    assert "$HOME" in r.stderr


def test_a_workspace_inside_the_checkout_is_refused(tmp_path):
    """The whole point of the split. Checked with the checkout under $HOME, so
    the $HOME guard cannot be what fires."""
    home = tmp_path / "home"
    r = _drive(tmp_path, str(home / "checkout" / "ws"), checkout_under_home=True)
    assert r.returncode == 2
    assert "inside the checkout" in r.stderr


def test_a_second_run_reads_the_pointer_instead_of_asking_again(tmp_path):
    """Setup is idempotent; a re-run must not re-prompt for something already
    recorded, and must not need stdin to get past this step."""
    first = _drive(tmp_path, str(tmp_path / "home" / "mywork"))
    assert first.returncode == 0, first.stderr
    again = _drive(tmp_path, None)
    assert again.returncode == 0, again.stderr
    assert _chosen(again) == str(tmp_path / "home" / "mywork")
