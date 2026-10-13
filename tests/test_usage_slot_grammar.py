"""The how-to's slot grammar has ONE home (core_data) and is enforced where the block is
WRITTEN, naming the fix.

The self-test, the I6 walk, the record derivation, the Nextflow renderer and the patch
gate all used to spell `{UPPER_SNAKE}` for themselves — and the patch gate had no
spelling at all, so a block written with `{output_fastq}`, `${SAMPLE}` or `{{THREADS}}`
was accepted and the seal then reported "no slots", `$<value>` or `{<value>}` one round
trip later. The grammar now lives beside `usage_commands`, every reader imports it, and
`patch_pipeline` refuses what the seal could not run with the exact rewrite.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.models import core_data as CD
from agent.skills import pipeline_state as PS
from agent.skills import spec_writer as SW

AGENT = Path(__file__).resolve().parent.parent / "agent"


def _usage(template, inputs=("INPUT_FASTQ",), trials=None,
           outputs=(("trimmed", ["*_trimmed.fastq.gz"]),)):
    return {"description": "trim", "command_template": template,
            "inputs": [{"name": n, "format": "fastq.gz"} for n in inputs],
            "outputs": [{"name": n, "files": f} for n, f in outputs],
            "trials": trials if trials is not None else []}


# ── one spelling ───────────────────────────────────────────────────────────────────────
def test_no_module_but_core_data_spells_the_slot_grammar():
    """The regex literal and the output-slot rule exist once. A second spelling is how the
    write gate and the seal came to disagree."""
    literal = CD.SLOT_RE.pattern
    offenders = []
    for p in AGENT.rglob("*.py"):
        if p.name == "core_data.py":
            continue
        text = p.read_text()
        if literal in text or re.search(r"def _?is_output_slot\b", text):
            offenders.append(str(p.relative_to(AGENT)))
    assert offenders == [], f"the slot grammar is spelled outside core_data in {offenders}"


@pytest.mark.parametrize("slot,expected", [
    ("OUTPUT_DIR", True), ("OUT_DIR", True), ("OUTDIR", True), ("OUTPUTDIR", True),
    ("MY_OUTPUT", True), ("OUTPUT_BAM", True),
    ("OUT_TSV", False), ("OUTFILE", False), ("OUPUT_DIR", False), ("FASTQ", False),
])
def test_is_output_slot_folds_the_underscore(slot, expected):
    assert CD.is_output_slot(slot) is expected


def test_slots_in_keeps_first_occurrence_order_without_duplicates():
    assert CD.slots_in("a {B} {A} {B} {out} {C_1}") == ["B", "A", "C_1"]
    assert CD.usage_slots({"command_template": ["x {R1}", "y {R1} {OUTPUT_DIR}"]}) == ["R1", "OUTPUT_DIR"]


def test_fill_slots_is_textual():
    assert CD.fill_slots("-o {OUTPUT_DIR}/{SAMPLE}.bam", {"OUTPUT_DIR": "/s", "SAMPLE": "x"}) == "-o /s/x.bam"


# ── the rules, at the write ────────────────────────────────────────────────────────────
def test_a_lowercase_slot_the_block_treats_as_a_slot_is_named_with_its_rename():
    u = _usage("cutadapt -a AGATCG -o {output_fastq} {input_fastq}", inputs=("input_fastq",),
               trials=[{"name": "t", "substitutions": {"input_fastq": "/d/r1.fq.gz",
                                                       "output_fastq": "/d/o.fq.gz"}}])
    problems = PS.usage_block_problems(u)
    assert len(problems) == 1
    assert "UPPER_SNAKE" in problems[0]
    assert "`{output_fastq}` → `{OUTPUT_DIR}/<name>`" in problems[0], "an output file slot is not renamed to a slot a directory would fill"
    assert "`{input_fastq}` → `{INPUT_FASTQ}`" in problems[0]
    assert "trials[*].substitutions" in problems[0] and "inputs[*].name" in problems[0]


def test_a_template_with_no_slot_at_all_says_how_to_write_one():
    problems = PS.usage_block_problems(_usage("Rscript deseq2_de.R", inputs=()))
    assert len(problems) == 1 and "no `{SLOT}`" in problems[0] and "{OUTPUT_DIR}/<name>" in problems[0]


def test_shell_variable_syntax_around_a_slot_is_refused_with_the_bare_form():
    """`${SAMPLE}` fills to `$SRR1039508`: the shell reads an unset variable. Seen on a
    real run, where it cost one seal round trip."""
    u = _usage('hisat2 -U {FASTQ} | samtools sort -o "{OUTPUT_DIR}/${SAMPLE}.bam"',
               inputs=("FASTQ", "SAMPLE"),
               trials=[{"name": "t", "substitutions": {"FASTQ": "/a", "SAMPLE": "s"}}])
    problems = PS.usage_block_problems(u)
    assert any("`${SAMPLE}` → `{SAMPLE}`" in p and "unset variable" in p for p in problems), problems


def test_double_braces_are_refused_as_not_an_escape():
    """`THREADS={{THREADS}}` fills to `THREADS={4}`. Also seen on a real run."""
    u = _usage("THREADS={{THREADS}}\nhisat2 -p $THREADS {FASTQ} > {OUTPUT_DIR}/x.sam",
               inputs=("FASTQ", "THREADS"),
               trials=[{"name": "t", "substitutions": {"FASTQ": "/a", "THREADS": "4"}}])
    problems = PS.usage_block_problems(u)
    assert any("`{{THREADS}}` → `{THREADS}`" in p and "not an escape" in p for p in problems), problems


def test_an_awk_brace_is_not_mistaken_for_a_slot_when_real_slots_exist():
    u = _usage("samtools view {BAM} | awk '{print $3}' > {OUTPUT_DIR}/contigs.txt", inputs=("BAM",),
               trials=[{"name": "t", "substitutions": {"BAM": "/d/a.bam"}}])
    assert PS.usage_block_problems(u) == []


def test_a_well_formed_block_passes_including_outdir_and_slots_in_output_patterns():
    u = _usage("cutadapt -a AGATCG -o {OUTDIR}/{SAMPLE}_trimmed.fastq.gz {INPUT_FASTQ}",
               inputs=("INPUT_FASTQ", "SAMPLE"),
               outputs=(("trimmed", ["{SAMPLE}_trimmed.fastq.gz"]),),
               trials=[{"name": "t", "substitutions": {"INPUT_FASTQ": "/d/r1.fq.gz", "SAMPLE": "s1"}}])
    assert PS.usage_block_problems(u) == []


def test_an_undeclared_input_slot_and_a_stray_input_name_are_each_named():
    """I6's rule, at the write instead of one seal later."""
    u = _usage("tool -i {INPUT_FASTQ} -s {SAMPLE} -o {OUTPUT_DIR}/x", inputs=("INPUT_FASTQ", "SAMPEL"),
               trials=[{"name": "t", "substitutions": {"INPUT_FASTQ": "/a", "SAMPLE": "s"}}])
    problems = PS.usage_block_problems(u)
    assert any("{SAMPLE} not declared under inputs[*].name" in p for p in problems), problems
    assert any("'SAMPEL'" in p and "match no slot" in p for p in problems), problems


def test_missing_output_slot_and_trial_binding_errors_are_each_named():
    u = _usage("tool -i {INPUT_FASTQ} -o /tmp/fixed.txt", inputs=("INPUT_FASTQ",),
               trials=[{"name": "t1", "substitutions": {}},
                       {"name": "t2", "substitutions": {"INPUT_FASTQ": "/d/x", "GHOST": "y"}}])
    problems = PS.usage_block_problems(u)
    assert any("no output slot" in p and "INPUT_FASTQ" in p for p in problems)
    assert any("trial 't1' binds no value for {INPUT_FASTQ}" in p for p in problems)
    assert any("trial 't2' binds {GHOST}, which the template does not contain" in p for p in problems)
    assert not any("UPPER_SNAKE" in p for p in problems)


def test_the_patch_refuses_the_block_the_seal_cannot_run(tmp_path):
    ps = PS.PipelineState({})
    ps.start("p1", "desc")
    out = ps.patch("p1", {"usage": _usage("tool -o {out} {inp}", inputs=("inp",),
                                          trials=[{"name": "t", "substitutions": {"inp": "/a", "out": "/b"}}])})
    assert out.get("code") == "pipeline_state.usage_invalid", out
    assert "`{out}` → `{OUT}`" in out["error"] and "`{inp}` → `{INP}`" in out["error"]
    assert "usage" not in (ps.get_draft("p1") or {}), "a refused patch must not land"
    good = _usage("tool -o {OUTPUT_DIR}/x.txt {INP}", inputs=("INP",),
                  trials=[{"name": "t", "substitutions": {"INP": "/a"}}])
    assert "code" not in ps.patch("p1", {"usage": good})


def test_the_seal_reason_names_the_rename_when_a_draft_arrives_with_lowercase_slots():
    spec = {"usage": _usage("seqtk seq -A {r1} > {output_fasta}", inputs=("r1",)), "conda_env": "e"}

    class _Runner:
        is_image_runner = False
    r = SW.self_test_usage(spec, _Runner())
    assert r["status"] == "not_attempted"
    assert "no {PLACEHOLDER} slots" in r["reason"]
    assert "`{r1}`" in r["reason"] and "`{output_fasta}`" in r["reason"] and "`{R1}`" in r["reason"]


# ── the seal reads the same grammar ────────────────────────────────────────────────────
class _FakeEM:
    """Runs the command by writing the named file into the scratch dir, rc=0."""
    def __init__(self, write_rel):
        self.write_rel = write_rel

    def run_in_env(self, env_name, command, timeout=None, watch_dir=None, **kw):
        (Path(watch_dir) / self.write_rel).write_text("x")
        return {"returncode": 0, "stdout": "", "stderr": ""}


def test_output_patterns_are_filled_with_the_trials_bindings():
    """`{SAMPLE}.sorted.bam` declared, `SRR1039508.sorted.bam` produced: a match. The
    renderer already read slots in these patterns; the self-test did not, and a run
    that wrote every file it promised failed as 'expected outputs missing'."""
    plan = {"name": "s", "substitutions": {"FASTQ": "/d/in.fq", "SAMPLE": "SRR1039508"}}
    cmds = ["hisat2 -U {FASTQ} -o {OUTDIR}/{SAMPLE}.sorted.bam"]
    outputs = [{"name": "sorted_bam", "files": ["{SAMPLE}.sorted.bam"]}]
    res = SW._run_one_trial(plan, cmds, {"FASTQ", "SAMPLE", "OUTDIR"}, outputs,
                            _FakeEM("SRR1039508.sorted.bam"), "x", {"pipeline_name": "p"}, None)
    assert res["ok"] is True, res
    assert res["output_slots"] == ["OUTDIR"]


def test_a_pattern_spelling_the_output_directory_is_read_relative_to_scratch():
    plan = {"name": "s", "substitutions": {"IN": "/d/in"}}
    res = SW._run_one_trial(plan, ["t {IN} > {OUTPUT_DIR}/o.tsv"], {"IN", "OUTPUT_DIR"},
                            [{"name": "o", "files": ["{OUTPUT_DIR}/o.tsv"]}],
                            _FakeEM("o.tsv"), "x", {"pipeline_name": "p"}, None)
    assert res["ok"] is True, res


def test_a_missed_pattern_reports_both_spellings():
    plan = {"name": "s", "substitutions": {"IN": "/d/in", "SAMPLE": "s1"}}
    res = SW._run_one_trial(plan, ["t {IN} > {OUTPUT_DIR}/o.tsv"], {"IN", "SAMPLE", "OUTPUT_DIR"},
                            [{"name": "o", "files": ["{SAMPLE}.bam"]}],
                            _FakeEM("o.tsv"), "x", {"pipeline_name": "p"}, None)
    assert res["ok"] is False
    assert res["missing_outputs"] == [{"slot": "o", "pattern": "{SAMPLE}.bam", "pattern_filled": "s1.bam"}]


def test_i6_accepts_any_output_slot_the_self_test_accepts_and_still_refuses_a_typo():
    """The walk and the self-test used to disagree: I6 knew only OUTPUT_DIR/OUT_DIR while
    the self-test filled every name containing 'output'. One rule now."""
    from agent.skills.spec_writer import check_invariants

    def _v(template):
        spec = {"pipeline_name": "t", "packages": [], "install_steps": [], "pipeline_steps": [],
                "usage": {"description": "x", "command_template": template,
                          "inputs": [{"name": "INPUT_BAM", "format": "bam"}],
                          "outputs": [{"name": "o", "files": ["*"]}]}}
        return [x for x in check_invariants(spec)
                if x["invariant"] == "I6.template_placeholders_declared"]
    assert _v("samtools view {INPUT_BAM} -o {OUTDIR}/out.bam") == []
    assert _v("samtools view {INPUT_BAM} -o {OUTPUT_BAM}") == []
    bad = _v("samtools view {INPUT_BAM} -o {OUPUT_DIR}/out.bam")
    assert bad and bad[0]["undeclared_placeholders"] == ["OUPUT_DIR"]
    assert "output directory" in bad[0]["message"]


def test_a_block_without_trials_is_refused_naming_the_slots_a_trial_must_bind():
    """Guessing the values from the recorded steps filled a script's slots positionally
    and burned a seal on a command that never could have worked. The author ran the
    commands and knows the values: they are the trial."""
    u = _usage("Rscript {SCRIPT} {COUNTS_DIR} {OUTPUT_DIR}/de.csv", inputs=("SCRIPT", "COUNTS_DIR"), trials=[])
    problems = PS.usage_block_problems(u)
    assert len(problems) == 1 and problems[0].startswith("no trials:")
    assert "{COUNTS_DIR}, {SCRIPT}" in problems[0] and "values the run used" in problems[0]
    u["trials"] = [{"name": "airway", "substitutions": {"SCRIPT": "/s.R", "COUNTS_DIR": "/c"}}]
    assert PS.usage_block_problems(u) == []
    only_output = _usage("make-report > {OUTPUT_DIR}/r.txt", inputs=(), trials=[])
    assert PS.usage_block_problems(only_output) == [], "nothing to bind, nothing to declare"


def test_a_file_shaped_output_slot_is_refused_with_the_directory_idiom():
    """`-o {OUTPUT_FASTQ}` reads as an output slot, so the self-test filled it with the
    scratch directory and cutadapt answered "Is a directory". Three runs paid a seal for it."""
    u = _usage("cutadapt -a AGATCG -o {OUTPUT_FASTQ} {INPUT_FASTQ}", inputs=("INPUT_FASTQ",),
               trials=[{"name": "t", "substitutions": {"INPUT_FASTQ": "/d/r1.fq.gz"}}])
    problems = PS.usage_block_problems(u)
    assert len(problems) == 1 and problems[0].startswith("output slots are directories")
    assert "`{OUTPUT_FASTQ}` → `{OUTPUT_DIR}/<name>`" in problems[0] and "outputs[*].files" in problems[0]
    for ok_slot in ("OUTPUT_DIR", "OUTDIR", "OUTPUT_FOLDER", "OUTPUT_DIRECTORY"):
        assert CD.is_output_dir_slot(ok_slot) and PS.usage_block_problems(
            _usage(f"cutadapt -o {{{ok_slot}}}/t.fq.gz {{INPUT_FASTQ}}",
                   trials=[{"name": "t", "substitutions": {"INPUT_FASTQ": "/d/r1.fq.gz"}}])) == []
    assert not CD.is_output_dir_slot("OUTPUT_PREFIX") and not CD.is_output_dir_slot("FASTQ")


def test_an_is_a_directory_failure_names_the_output_slot_trap():
    class _Dir:
        def run_in_env(self, env_name, command, timeout=None, watch_dir=None, **kw):
            return {"returncode": 1, "stdout": "", "stderr": "[Errno 21] Is a directory: '/tmp/x'"}
    res = SW._run_one_trial({"name": "t", "substitutions": {"IN": "/d/in"}}, ["seqkit stats -o {OUTPUT_DIR} {IN}"],
                            {"IN", "OUTPUT_DIR"}, [{"name": "o", "files": ["*.tsv"]}], _Dir(), "x",
                            {"pipeline_name": "p"}, None)
    assert res["ok"] is False and "{OUTPUT_DIR}/<name>" in res["hint"] and "{OUTPUT_DIR}" in res["hint"]


# ── a machine path in the template is a slot that was not declared ─────────────────────
def test_a_machine_path_in_the_template_is_refused_at_the_write_and_named():
    """The self-test mounts only the paths bound through slots, so a how-to carrying an
    absolute path from this machine's zones can never resolve at the image locus — and it
    is not re-runnable anywhere else either. Caught at the write, with the slot remedy."""
    from agent.skills import workspace
    idx = str(workspace.workspace_root() / "common_data" / "hisat2_chr22_index" / "chr22")
    ref = str(workspace.resources_root() / "core_test_data_hg38" / "genome" / "chr22.fa")
    u = _usage(f"hisat2 -x {idx} -U {{INPUT_FASTQ}} | samtools sort -o {{OUTPUT_DIR}}/x.bam && "
               f"samtools faidx {ref}",
               trials=[{"name": "t", "substitutions": {"INPUT_FASTQ": "/a"}}])
    problems = PS.usage_block_problems(u)
    hit = [p for p in problems if "names a path on this machine" in p]
    assert len(hit) == 1, problems
    assert f"`{idx}`" in hit[0] and f"`{ref}`" in hit[0]
    assert "write it as a slot" in hit[0] and "trial's substitutions" in hit[0]
    assert CD.machine_paths_in(u["command_template"]) == [idx, ref]


def test_paths_inside_the_image_and_slot_paths_are_not_machine_paths():
    """`/opt/tool/db` lives in the image; `{OUTPUT_DIR}/x.bam` is a slot's own path."""
    u = _usage("tool --db /opt/tool/db -i {INPUT_FASTQ} -o {OUTPUT_DIR}/x.bam 2>{OUTPUT_DIR}/x.log",
               trials=[{"name": "t", "substitutions": {"INPUT_FASTQ": "/a"}}])
    assert not any("names a path on this machine" in p for p in PS.usage_block_problems(u))
    assert CD.machine_paths_in(u["command_template"]) == []
    assert CD.ABS_PATH_RE.findall("x {OUTPUT_DIR}/s.bam -o /tmp/a.b -x=/data/idx") == ["/tmp/a.b", "/data/idx"]
