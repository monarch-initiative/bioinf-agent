"""The RUN dashboard must SHOW what the seal GATED ON.

A field the invariants read and the renderer does not is a fact the record knows and
the artifact refuses to say — and the reader's only recourse is to open the yaml, which
is precisely what these pages exist to spare them. Three fields were in exactly that
state, all live in code, and the reason nothing caught them is that the suite tested
the renderer against fixtures that also omitted them:

  * the I4 self-test's ACTUAL INVOCATION. `self_test_usage` resolves every
    {PLACEHOLDER} to a concrete path and executes the result; seal kept the count of
    trials that passed and threw the transcript away. So the how-to panel could show
    `hisat2 -x {OUTPUT_DIR}/idx -1 {R1}` and the sealed artifact had no answer anywhere
    to "proven against WHICH genome, WHICH reads" — the first question a reader asks
    before committing real data to a pipeline.
  * `runtime_configs` — in I8's traceable universe (a step consuming an Exomiser
    analysis YAML seals cleanly on the strength of it), read by no renderer. It carries
    the HPO terms, the filters, the memory settings: the parameters under review.
  * `service_dependencies` — I10 REFUSES a seal for a service with no healthy probe,
    and the page never mentioned that the workflow needs Redis on :6379 at all.

These tests are the standing form of that rule. The last one is mechanical: populate
every gated block, render, and require a distinctive value from each to reach the page.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from agent.models import core_data
from agent.skills.run_dashboard_html import render_run_dashboard_html
from agent.skills.spec_writer import self_test_usage
from agent.mcp_tools.workflow_tools import _proven_trial_records


# ---------------------------------------------------------------------------
# A real runner + a real self-test, so the transcript under test is one the
# production path actually produced rather than one this file typed out.
# ---------------------------------------------------------------------------

class _ShellRunner:
    """Minimal stand-in for EnvManager: runs the command for real, in a shell."""
    is_image_runner = False

    def run_in_env(self, env_name, command, timeout=None, watch_dir=None):
        p = subprocess.run(command, shell=True, capture_output=True, text=True)
        return {"returncode": p.returncode, "stdout": p.stdout, "stderr": p.stderr}


class _AlwaysPasses:
    def validate(self, path, expected_type, env_name=None):
        return {"passed": Path(path).exists(), "validation_method": "exists"}


def _spec_with_real_selftest(tmp_path) -> tuple[dict, Path]:
    """Run the genuine I4 self-test, then build the spec seal would have written."""
    src = tmp_path / "reference_chr22.txt"
    src.write_text("ACGT\n")
    draft = {
        "pipeline_name": "copy_demo",
        "conda_env": "bioinf_demo",
        "usage": {
            "description": "copy the reference into the output directory",
            "command_template": "cp {SRC} {OUTPUT_DIR}/copy.txt",
            "inputs": [{"name": "SRC", "format": "txt"}],
            "outputs": [{"name": "copy", "files": ["copy.txt"], "type": "txt"}],
            "trials": [{"name": "one_file", "substitutions": {"SRC": str(src)},
                        "description": "a single small reference"}],
        },
    }
    detail = self_test_usage(draft, _ShellRunner(), validator=_AlwaysPasses())
    assert detail["status"] == "verified", detail
    spec = {
        "workflow_name": "copy_demo",
        "description": "demo",
        "created_at": "2026-08-07T00:00:00+00:00",
        "env_request_key": "demo|linux-64|none",
        "env_content_digest": "sha256:" + "ab" * 32,
        "env_image": "example/demo@sha256:" + "cd" * 32,
        "usage": draft["usage"],
        "usage_verified": True,
        "usage_verification": {
            "status": detail["status"], "reason": "", "locus": detail["locus"],
            "trial_count": detail["trial_count"], "passed": detail["passed"],
            "trials": _proven_trial_records(detail),
        },
        "pipeline_steps": [],
    }
    return spec, src


@pytest.mark.integration
def test_the_page_shows_the_command_that_actually_ran_not_only_the_template(tmp_path):
    """The substituted invocation is the point. A template is a form; this is evidence."""
    spec, src = _spec_with_real_selftest(tmp_path)
    html = render_run_dashboard_html(spec)

    # The template still appears (it is what you adapt for your own data)...
    assert "cp {SRC} {OUTPUT_DIR}/copy.txt" in html
    # ...and so does the literal text that was executed, with the real path in it.
    trials = core_data.usage_proven_trials(spec)
    assert len(trials) == 1
    ran = trials[0]["commands_run"][0]
    assert str(src) in ran, "the self-test must have substituted the real path"
    assert ran in html, "the executed command must reach the page"
    assert str(src) in html, "which file it was proven against must be readable"


@pytest.mark.integration
def test_the_ephemeral_output_directory_is_named_as_ephemeral(tmp_path):
    """The substitution map has two halves with different lifetimes.

    An input slot holds a durable path to real data. An output slot holds the scratch
    dir the self-test created and deleted. Printing them in one table without comment
    would hand the reader a path that no longer exists.
    """
    spec, _ = _spec_with_real_selftest(tmp_path)
    trials = core_data.usage_proven_trials(spec)
    assert trials[0]["output_slots"] == ["OUTPUT_DIR"], trials[0]
    html = render_run_dashboard_html(spec)
    assert "already deleted; supply your own" in html
    assert "{OUTPUT_DIR}" in html and "{SRC}" in html


@pytest.mark.integration
def test_a_transcript_is_never_invented_for_a_spec_that_has_none(tmp_path):
    """A verified spec sealed before the transcript existed must say so.

    The tempting fix is to re-substitute `usage.trials[*].substitutions` into
    `command_template` at render time and print THAT. It would look identical and be a
    claim — the author's declared values presented as a record of what ran, differing
    from the truth on exactly the things that are easy to get wrong (output slots are
    overridden with a scratch dir, not given their declared value). Absence renders as
    absence here as everywhere else.
    """
    spec, src = _spec_with_real_selftest(tmp_path)
    del spec["usage_verification"]["trials"]          # the pre-2026-08-07 shape
    html = render_run_dashboard_html(spec)

    assert core_data.usage_status(spec) == "verified"
    assert "what the author DECLARED" in html
    assert "cannot show the transcript of what ran" in html
    # The declared value is still shown — it is useful and it is honestly labelled.
    assert str(src) in html
    # But nothing on the page claims it as a transcript.
    assert "LITERAL text that was executed" not in html


@pytest.mark.integration
def test_declared_trials_on_an_unverified_spec_are_marked_not_executed():
    spec = {
        "workflow_name": "w", "description": "", "created_at": "2026-08-07T00:00:00+00:00",
        "env_request_key": "k", "env_content_digest": "d", "env_image": "i",
        "usage": {
            "command_template": "samtools dict {REF} -o {OUTPUT_DIR}/genome.dict",
            "inputs": [{"name": "REF", "format": "fasta"}],
            "outputs": [{"name": "d", "files": ["genome.dict"]}],
            "trials": [{"name": "fasta", "substitutions": {"REF": "/data/genome.fa"}}],
        },
        "usage_verification": {"status": "not_attempted", "reason": "no runner"},
        "pipeline_steps": [],
    }
    html = render_run_dashboard_html(spec)
    assert "NOT self-tested" in html
    assert "these are authored values" in html
    assert "/data/genome.fa" in html          # shown, because a reader wants it
    assert "outputs validated" not in html    # but never badged as proven


@pytest.mark.integration
def test_a_verified_spec_with_no_trials_at_all_says_unrecorded():
    """The inferred-trial path on an old spec: proven, but against nothing we recorded."""
    spec = {
        "workflow_name": "w", "description": "", "created_at": "2026-08-07T00:00:00+00:00",
        "env_request_key": "k", "env_content_digest": "d", "env_image": "i",
        "usage": {"command_template": "tool {IN} -o {OUTPUT_DIR}/x",
                  "inputs": [{"name": "IN"}], "outputs": [{"name": "x", "files": ["x"]}],
                  "trials": []},
        "usage_verification": {"status": "verified", "reason": "", "locus": "image",
                               "trial_count": 1, "passed": 1},
        "pipeline_steps": [],
    }
    html = render_run_dashboard_html(spec)
    assert "<b>unrecorded</b> here" in html


# ---------------------------------------------------------------------------
# The two blocks no renderer read.
# ---------------------------------------------------------------------------

def _spec_with_gated_blocks() -> dict:
    return {
        "workflow_name": "gated_demo",
        "description": "every gated block populated",
        "created_at": "2026-08-07T00:00:00+00:00",
        "env_request_key": "k", "env_content_digest": "d", "env_image": "i",
        "usage": {"command_template": "tool {IN}", "inputs": [{"name": "IN"}],
                  "outputs": [{"name": "o", "files": ["o"]}]},
        "usage_verification": {"status": "not_attempted", "reason": "hermetic test"},
        "pipeline_steps": [],
        "test_data": {"r1": "/data/PINNED_READS.fastq.gz"},
        "reference_databases": [{"name": "PINNED_REFDB", "sha256": "f" * 64,
                                 "size_bytes": 1234}],
        "authored_artifacts": [{"role": "driver", "path": "/work/PINNED_DRIVER.sh",
                                "sha256": "a" * 64}],
        "runtime_configs": [
            {"name": "analysis_yaml", "format": "yaml",
             "path": "/work/PINNED_ANALYSIS.yml", "sha256": "b" * 64,
             "content": "hpoIds: [HP:0001250]\nminQuality: 20\n"},
            {"name": "unpinned_props", "format": "properties",
             "path": "/work/PINNED_APP.properties"},
        ],
        "service_dependencies": [{
            "type": "cache", "name": "PINNED_REDIS", "version": "7.2",
            "start_command": "redis-server --port 6379",
            "stop_command": "redis-cli shutdown",
            "health_check_command": "redis-cli -p 6379 ping",
            "port": 6379, "status": "stopped",
            "env_vars": {"REDIS_URL": "redis://localhost:6379"},
            "health_check_log": [
                {"timestamp": "2026-08-07T00:00:01+00:00",
                 "command": "redis-cli -p 6379 ping", "returncode": 1, "healthy": False},
                {"timestamp": "2026-08-07T00:00:03+00:00",
                 "command": "redis-cli -p 6379 ping", "returncode": 0, "healthy": True},
            ],
        }],
    }


@pytest.mark.integration
def test_runtime_configs_reach_the_page_with_their_contents():
    html = render_run_dashboard_html(_spec_with_gated_blocks())
    assert "/work/PINNED_ANALYSIS.yml" in html
    assert "Runtime configuration" in html
    # The snapshot itself — on a cluster run the reader may have no shell at the locus
    # the file lives on, so "go look at it" is not advice they can take.
    assert "minQuality: 20" in html
    # An unpinned config must not read as pinned at a glance beside the pinned rows.
    assert "/work/PINNED_APP.properties" in html
    assert "not pinned" in html


@pytest.mark.integration
def test_service_dependencies_reach_the_page_with_their_health_evidence():
    html = render_run_dashboard_html(_spec_with_gated_blocks())
    assert "Runtime prerequisites" in html
    assert "PINNED_REDIS" in html
    assert "6379" in html
    assert "redis-cli -p 6379 ping" in html
    # 1 healthy of 2 probes — the badge counts, and the log shows both.
    assert "1/2 probe(s)" in html
    # `stopped` is the lifecycle, not a verdict: healthy-then-cleanly-stopped passes I10.
    assert "Lifecycle" in html


@pytest.mark.integration
def test_a_service_that_never_came_up_is_badged_as_such():
    """Unreachable on anything sealed since I10 was restored — seal refuses it. Rendered
    anyway rather than assumed away, because an older artifact predates the gate and
    this page is where a reader would find out."""
    spec = _spec_with_gated_blocks()
    spec["service_dependencies"][0]["health_check_log"] = [
        {"timestamp": "t", "command": "redis-cli ping", "returncode": 1, "healthy": False}]
    html = render_run_dashboard_html(spec)
    assert "never observed healthy" in html
    assert "0 healthy" in html


@pytest.mark.integration
def test_the_renderer_and_I10_read_health_the_same_way():
    """Both go through core_data.service_healthy_probes.

    The two-clause rule (`healthy is True` OR `returncode == 0`) is exactly the shape
    that gets re-typed with one clause missing, and the copy that drifts is always the
    one facing the user. This asserts the rc-only probe — the clause a hand-copy drops —
    counts on BOTH sides.
    """
    from agent.skills.spec_writer import _check_service_health
    svc = {"type": "cache", "name": "svc", "start_command": "s", "stop_command": "x",
           "health_check_command": "p", "status": "running",
           "health_check_log": [{"timestamp": "t", "command": "p", "returncode": 0}]}
    assert len(core_data.service_healthy_probes(svc)) == 1
    assert _check_service_health({"service_dependencies": [svc]}) == []

    spec = _spec_with_gated_blocks()
    spec["service_dependencies"] = [svc]
    assert "observed healthy" in render_run_dashboard_html(spec)


# ---------------------------------------------------------------------------
# The standing rule, mechanically.
# ---------------------------------------------------------------------------

#: Each gated block, with a value that can only appear on the page if the renderer
#: reads that block. Add a row when a new field starts gating a seal.
_MUST_REACH_THE_PAGE = [
    ("test_data (I8)",            "PINNED_READS.fastq.gz"),
    ("reference_databases (I5)",  "PINNED_REFDB"),
    ("authored_artifacts (I8)",   "PINNED_DRIVER.sh"),
    ("runtime_configs (I8)",      "PINNED_ANALYSIS.yml"),
    ("service_dependencies (I10)", "PINNED_REDIS"),
]


@pytest.mark.integration
@pytest.mark.parametrize("block,marker", _MUST_REACH_THE_PAGE,
                         ids=[b for b, _ in _MUST_REACH_THE_PAGE])
def test_every_gated_block_reaches_the_page(block, marker):
    html = render_run_dashboard_html(_spec_with_gated_blocks())
    assert marker in html, (
        f"{block} is read by a seal-side gate and does NOT reach the run dashboard. "
        f"The record knows it and the artifact will not say it, so the reader's only "
        f"recourse is the yaml — which is what this page exists to spare them.")


@pytest.mark.integration
def test_a_runtime_config_hash_does_not_borrow_the_meaning_of_the_ones_above_it():
    """Three sha256 columns on one page, verified by three different things.

    `authored_artifacts` are re-hashed by I8 at seal and `reference_databases` by I5.
    A `runtime_configs` hash is read by NEITHER — it is an anchor
    `run_production_pipeline`'s data-pin check compares before a production run. Three
    identical-looking columns stacked in one panel would let the third borrow the
    standing of the first two, which is the quiet way a page overclaims.
    """
    html = render_run_dashboard_html(_spec_with_gated_blocks())
    assert "NOT verified at seal" in html
    assert "run_production_pipeline re-checks" in html


@pytest.mark.integration
def test_the_provenance_footer_accounts_for_every_panel_on_the_page():
    """The footer enumerates observed-vs-authored, so a new panel makes it stale.

    It has been wrong before in exactly this way: it claimed no field on the page was
    agent-authored while rendering three from patch_pipeline's allowlist. Adding the
    service probes and the self-test transcript (observed) and the runtime configs
    (authored) without updating it would repeat that.
    """
    html = render_run_dashboard_html(_spec_with_gated_blocks())
    tail = html[html.index('class="gen"'):]
    for observed in ("self-test transcript", "service health probes"):
        assert observed in tail, f"{observed!r} is machine-observed and unlisted"
    assert "runtime configuration entries" in tail, (
        "runtime_configs are in patch_pipeline's allowlist and now rendered — the "
        "footer must not imply the page is purer than it is")


@pytest.mark.integration
def test_the_new_panels_do_not_break_purity(tmp_path):
    """Same honesty posture as before: deterministic, escaped, no clock read."""
    spec = _spec_with_gated_blocks()
    spec["runtime_configs"][0]["content"] = "cmd: <script>alert(1)</script>\n"
    a = render_run_dashboard_html(spec)
    assert a == render_run_dashboard_html(spec)
    assert "<script>alert(1)</script>" not in a
    assert "&lt;script&gt;" in a
