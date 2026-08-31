"""F10 — one `verify_command`, two working directories.

`install_git_repo(..., verify_command=…)` runs it on the HOST from the clone dir
(`env_manager.py:889` passes `working_dir=share_dir`; the docstring at `:771`
says so). The same string is then handed to `freeze` as the IN-IMAGE evidence,
where it ran from the container's default cwd. For a run-by-path script repo the
cwd IS part of how the tool is invoked, so the two runs asked different
questions.

Measured on the S4a specimen. `sys.path.insert(0,'HIC_ASSEMBLER'); import
scaffoldToChromosomes, orderGenome`:

    host      cwd=clone dir  -> ModuleNotFoundError: 'matplotlib'          <- the cause
    in-image  cwd=/work      -> ModuleNotFoundError: 'scaffoldToChromosomes' <- an artifact

`freeze` REFUSED, correctly — this never shipped a false green. What it cost was
the diagnosis: the refusal named a missing module from the tool's own repo, so a
reader goes hunting a packaging bug in someone else's code, while the real fix
(`conda install matplotlib`) had been identified by name on the host and thrown
away.

**The trap this file mostly exists for.** The obvious fix is to prepend
`cd /opt/tools/<name> && (…)` to the evidence string. That is a laundering hole,
because the clone path CONTAINS THE TOOL NAME and therefore satisfies
`evidence_shape_violation`'s word-boundary token rule on its own. Measured
before it was adopted:

    evidence_shape_violation('python -c "import foo"', 'gab')
      -> "never references the tool token 'gab' … cannot prove 'gab' is present"
    evidence_shape_violation('cd /opt/tools/gab && ( python -c "import foo" )', 'gab')
      -> None

So the cwd travels as execution metadata (`evidence_workdir`) and the recorded
`check` stays the author's raw string, which is the one the anti-echo-cheat rule
has to keep seeing.
"""
from __future__ import annotations

from agent.skills import install_commands as ic
from agent.skills.env_honesty import evidence_shape_violation

_VERIFY = ("python -c \"import sys; sys.path.insert(0,'HIC_ASSEMBLER'); "
           "import scaffoldToChromosomes\"")


def _spec(**kw):
    return ic.script_repo("gab", "https://github.com/x/GAB", ref="v1",
                          script_rel="HIC_ASSEMBLER/run.py", interpreter="python", **kw)


def test_a_caller_supplied_verify_declares_the_clone_dir_as_its_workdir():
    spec = _spec(evidence=_VERIFY)
    assert spec["evidence_workdir"].endswith("/gab"), spec.get("evidence_workdir")


def test_the_workdir_matches_where_the_host_ran_the_same_command():
    """The whole point: one command, one root. The host uses the clone dir, so
    the in-image run must use the clone dir."""
    spec = _spec(evidence=_VERIFY)
    assert spec["evidence_workdir"] == f"{ic._TOOLS}/gab"


def test_the_recorded_check_is_the_authors_raw_command():
    """Not `cd … && ( … )`. The contract asserts VALIDATED_IN_IMAGE's SHAPE on
    this string; a wrapper we added is not the author's evidence and must not be
    what the rule judges."""
    spec = _spec(evidence=_VERIFY)
    assert spec["evidence"] == _VERIFY
    assert not spec["evidence"].startswith("cd ")


def test_the_cd_prefix_would_have_laundered_the_token_rule():
    """The measurement that rejected the obvious fix, kept executable. If this
    ever stops holding, the shape rule has changed and the decision above should
    be revisited rather than silently inherited."""
    bare = 'python -c "import foo"'
    assert evidence_shape_violation(bare, "gab"), (
        "evidence that never names the tool must be refused")
    assert evidence_shape_violation(f"cd {ic._TOOLS}/gab && ( {bare} )", "gab") is None, (
        "if a cd-prefixed cheat is now caught, the workdir indirection may no "
        "longer be necessary — but check before removing it")


def test_the_default_wrapper_smoke_declares_no_workdir():
    """The wrapper is on PATH by construction, so its cwd is irrelevant. Every
    env that did not supply its own verify must be untouched by this change."""
    spec = _spec()
    assert "evidence_workdir" not in spec
    assert spec["evidence"].startswith("gab --help")


def test_a_declared_workdir_reaches_the_in_image_run():
    """The plumbing, end to end without a daemon: `validate_in_image` must cd to
    the declared dir for that check and to its default for every other."""
    import agent.skills.container_build as cbmod

    cb = cbmod.ContainerBuild()
    seen = []

    def fake_sh(args, timeout=1800):
        seen.append(args)
        return {"returncode": 0, "stdout": "", "stderr": ""}

    cb._sh = fake_sh
    cb.validate_in_image("img@sha256:d", [_VERIFY, "other --version"],
                         probe_tools=[], workdirs={_VERIFY: "/opt/tools/gab"})

    scripts = [a[-1] for a in seen if a[:3] == ["docker", "run", "--rm"]]
    mine = next(s for s in scripts if _VERIFY in s)
    other = next(s for s in scripts if "other --version" in s)
    assert mine.startswith("cd /opt/tools/gab "), mine
    assert other.startswith(f"cd {cb.workdir} "), other


def test_verifications_without_a_workdir_do_not_synthesize_one():
    """conda/pip verifications have no clone dir. The map handed to
    validate_in_image must simply omit them rather than pass an empty string that
    would `cd ''`."""
    from agent.skills.env_build import EnvBuild

    eb = EnvBuild.__new__(EnvBuild)
    eb.verifications = [
        {"label": "samtools", "tool": "samtools", "check": "samtools --version",
         "engine_coupled": True},
        {"label": "gab", "tool": "gab", "check": _VERIFY,
         "workdir": "/opt/tools/gab", "engine_coupled": True},
    ]
    workdirs = {v["check"]: v["workdir"] for v in eb.verifications if v.get("workdir")}
    assert workdirs == {_VERIFY: "/opt/tools/gab"}
