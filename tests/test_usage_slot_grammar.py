"""The how-to's slot grammar has ONE home (core_data) and is enforced where the step is
RUN, naming every fix in one answer.

The seal, the I6 walk, the record derivation and the Nextflow renderer all read the
`{UPPER_SNAKE}` grammar from core_data. A step's command is checked against it at the run
call — before anything executes — because the step's template IS the how-to the seal
derives: a slot in the wrong case, a `${SLOT}` the shell would eat, a file-shaped output
slot, an unbound slot or a machine path would otherwise reach the record and come back as
a refusal one seal later.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from agent.models import core_data as CD

AGENT = Path(__file__).resolve().parent.parent / "agent"


def _problems(template, bindings=None, formats=None):
    return CD.step_template_problems(template, bindings or {}, formats or {})


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
    ("MY_OUTPUT", True), ("OUTPUT_BAM", True), ("output_path", True),
    ("OUT_TSV", False), ("OUTFILE", False), ("OUPUT_DIR", False), ("FASTQ", False),
    ("REF", False),
])
def test_is_output_slot_folds_the_underscore(slot, expected):
    assert CD.is_output_slot(slot) is expected


def test_slots_in_keeps_first_occurrence_order_without_duplicates():
    assert CD.slots_in("a {B} {A} {B} {out} {C_1}") == ["B", "A", "C_1"]
    assert CD.usage_slots({"command_template": ["x {R1}", "y {R1} {OUTPUT_DIR}"]}) == ["R1", "OUTPUT_DIR"]


def test_fill_slots_is_textual_and_fill_template_puts_the_work_dir_in_the_output_slot():
    assert CD.fill_slots("-o {OUTPUT_DIR}/{SAMPLE}.bam", {"OUTPUT_DIR": "/s", "SAMPLE": "x"}) == "-o /s/x.bam"
    assert CD.fill_template("t {IN} -o {OUTDIR}/{SAMPLE}.bam", {"IN": "/a", "SAMPLE": "s"}, "/w") == "t /a -o /w/s.bam"
    assert CD.output_slots_of("t {IN} -o {OUTDIR}/x > {OUTPUT_DIR}/y") == ["OUTDIR", "OUTPUT_DIR"]


# ── the rules, at the run call ─────────────────────────────────────────────────────────
def test_a_lowercase_slot_the_call_binds_is_named_with_its_rename():
    problems = _problems("cutadapt -a AGATCG -o {output_fastq} {input_fastq}",
                         {"input_fastq": "/d/r1.fq.gz"})
    assert len(problems) == 1
    assert "UPPER_SNAKE" in problems[0]
    assert "`{output_fastq}` → `{OUTPUT_DIR}/<name>`" in problems[0], "an output file slot is not renamed to a slot a directory would fill"
    assert "`{input_fastq}` → `{INPUT_FASTQ}`" in problems[0]


def test_a_command_with_no_slot_and_no_binding_is_a_legal_fixed_step():
    assert _problems("Rscript deseq2_de.R") == []
    assert _problems("samtools index aligned.bam") == []


def test_shell_variable_syntax_around_a_slot_is_refused_with_the_bare_form():
    """`${SAMPLE}` fills to `$SRR1039508`: the shell reads an unset variable. Seen on a
    real run, where it cost one seal round trip."""
    problems = _problems('hisat2 -U {FASTQ} | samtools sort -o "{OUTPUT_DIR}/${SAMPLE}.bam"',
                         {"FASTQ": "/a", "SAMPLE": "s"})
    assert any("`${SAMPLE}` → `{SAMPLE}`" in p and "unset variable" in p for p in problems), problems


def test_double_braces_are_refused_as_not_an_escape():
    """`THREADS={{THREADS}}` fills to `THREADS={4}`. Also seen on a real run."""
    problems = _problems("THREADS={{THREADS}}\nhisat2 -p $THREADS {FASTQ} > {OUTPUT_DIR}/x.sam",
                         {"FASTQ": "/a", "THREADS": "4"})
    assert any("`{{THREADS}}` → `{THREADS}`" in p and "not an escape" in p for p in problems), problems


def test_an_awk_brace_is_not_mistaken_for_a_slot_when_real_slots_exist():
    assert _problems("samtools view {BAM} | awk '{print $3}' > {OUTPUT_DIR}/contigs.txt", {"BAM": "/d/a.bam"}) == []


def test_a_well_formed_call_passes_including_outdir_and_an_empty_value():
    assert _problems("cutadapt -a AGATCG -o {OUTDIR}/{SAMPLE}_trimmed.fastq.gz {INPUT_FASTQ} {EXTRA}",
                     {"INPUT_FASTQ": "/d/r1.fq.gz", "SAMPLE": "s1", "EXTRA": ""}) == []


def test_an_unbound_slot_a_stray_binding_and_a_bound_output_slot_are_each_named_in_one_answer():
    problems = _problems("tool -i {INPUT_FASTQ} -s {SAMPLE} -o {OUTPUT_DIR}/x",
                         {"INPUT_FASTQ": "/a", "SAMPEL": "s", "OUTPUT_DIR": "/elsewhere"})
    assert any("{SAMPLE} not bound" in p for p in problems), problems
    assert any("bindings name {SAMPEL}" in p for p in problems), problems
    assert any("{OUTPUT_DIR} is an output slot" in p and "never bound" in p for p in problems), problems


def test_formats_must_name_a_bound_slot_and_a_known_format():
    problems = _problems("hisat2 -p {THREADS} -U {R1} -S {OUTPUT_DIR}/x.sam", {"THREADS": "4", "R1": "/a"},
                         {"THREADS": "cpus", "GHOST": "value"})
    assert any("formats take one of threads, value, samplesheet" in p and "THREADS='cpus'" in p for p in problems)
    assert any("formats name {GHOST}" in p for p in problems)
    assert _problems("hisat2 -p {THREADS} -U {R1} -S {OUTPUT_DIR}/x.sam", {"THREADS": "4", "R1": "/a"},
                     {"THREADS": "threads"}) == []


def test_a_file_shaped_output_slot_is_refused_with_the_directory_idiom():
    """`-o {OUTPUT_FASTQ}` reads as an output slot, so the run fills it with the working
    directory and cutadapt answers "Is a directory". Three runs paid a seal for it."""
    problems = _problems("cutadapt -a AGATCG -o {OUTPUT_FASTQ} {INPUT_FASTQ}", {"INPUT_FASTQ": "/d/r1.fq.gz"})
    assert len(problems) == 1 and problems[0].startswith("output slots are directories")
    assert "`{OUTPUT_FASTQ}` → `{OUTPUT_DIR}/<name>`" in problems[0]
    for ok_slot in ("OUTPUT_DIR", "OUTDIR", "OUTPUT_FOLDER", "OUTPUT_DIRECTORY"):
        assert CD.is_output_dir_slot(ok_slot)
        assert _problems(f"cutadapt -o {{{ok_slot}}}/t.fq.gz {{INPUT_FASTQ}}", {"INPUT_FASTQ": "/d/r1.fq.gz"}) == []
    assert not CD.is_output_dir_slot("OUTPUT_PREFIX") and not CD.is_output_dir_slot("FASTQ")


# ── a machine path in the command is a slot that was not declared ──────────────────────
def test_a_machine_path_in_the_command_is_refused_at_the_run_and_named():
    from agent.skills import workspace
    idx = str(workspace.workspace_root() / "common_data" / "hisat2_chr22_index" / "chr22")
    ref = str(workspace.resources_root() / "core_test_data_hg38" / "genome" / "chr22.fa")
    problems = _problems(f"hisat2 -x {idx} -U {{INPUT_FASTQ}} | samtools sort -o {{OUTPUT_DIR}}/x.bam && "
                         f"samtools faidx {ref}", {"INPUT_FASTQ": "/a"})
    hit = [p for p in problems if "names a path on this machine" in p]
    assert len(hit) == 1, problems
    assert f"`{idx}`" in hit[0] and f"`{ref}`" in hit[0] and "write it as a slot" in hit[0]
    assert CD.machine_paths_in(f"hisat2 -x {idx} -U {{INPUT_FASTQ}} && samtools faidx {ref}") == [idx, ref]


def test_paths_inside_the_image_and_slot_paths_are_not_machine_paths():
    """`/opt/tool/db` lives in the image; `{OUTPUT_DIR}/x.bam` is a slot's own path."""
    cmd = "tool --db /opt/tool/db -i {INPUT_FASTQ} -o {OUTPUT_DIR}/x.bam 2>{OUTPUT_DIR}/x.log"
    assert not any("names a path on this machine" in p for p in _problems(cmd, {"INPUT_FASTQ": "/a"}))
    assert CD.machine_paths_in(cmd) == []
    assert CD.ABS_PATH_RE.findall("x {OUTPUT_DIR}/s.bam -o /tmp/a.b -x=/data/idx") == ["/tmp/a.b", "/data/idx"]


# ── a how-to that patches the image is not a how-to ────────────────────────────────────
def test_a_command_that_modifies_the_image_is_refused_at_the_run_with_the_fragment():
    shim = ("cat > /usr/lib/python3.10/sitecustomize.py << 'PYEOF'\nimport sys\nPYEOF\n"
            "/opt/deepvariant/bin/run_deepvariant --reads {INPUT_FASTQ} --output_vcf {OUTPUT_DIR}/out.vcf.gz")
    hit = [p for p in _problems(shim, {"INPUT_FASTQ": "/a"}) if "modifies the image before running the tool" in p]
    assert len(hit) == 1
    assert "`> /usr/lib/python3.10/sitecustomize.py`" in hit[0] and "Build the fix into the image" in hit[0]
    assert any("`pip install`" in p for p in _problems("pip install pyarrow && tool {INPUT_FASTQ} -o {OUTPUT_DIR}/x.txt",
                                                       {"INPUT_FASTQ": "/a"}))
    assert not any("modifies the image" in p for p in _problems("tool {INPUT_FASTQ} > {OUTPUT_DIR}/x.txt 2>&1",
                                                                {"INPUT_FASTQ": "/a"}))


# ── the run tools refuse before anything runs, in one answer ───────────────────────────
def test_run_pipeline_step_refuses_a_bad_template_before_running_and_records_nothing(monkeypatch):
    import agent.mcp_server as ms
    from agent.mcp_tools import run_tools as R
    ran, recorded = [], []
    monkeypatch.setattr(ms._env_mgr, "run_in_env", lambda *a, **k: ran.append(a) or {"returncode": 0}, raising=False)
    monkeypatch.setattr(ms._pipeline_state, "add_step", lambda *a, **k: recorded.append(a) or 1, raising=False)
    monkeypatch.setattr(ms._pipeline_state, "get_draft", lambda pid: {"pipeline_steps": []}, raising=False)
    out = R.run_pipeline_step(env_name="x", command="tool -o {output_tsv} {in}", pipeline_id="p",
                              bindings={"in": "/a"})
    assert out["outcome"] == "refused" and out["code"] == "run_pipeline_step.howto_invalid"
    assert "`{in}` → `{IN}`" in out["error"] and ran == [] and recorded == []


def test_the_i6_walk_accepts_any_output_slot_and_still_refuses_a_typo():
    from agent.skills.spec_writer import check_invariants

    def _v(template):
        spec = {"pipeline_name": "t", "packages": [], "install_steps": [], "pipeline_steps": [],
                "usage": {"description": "x", "command_template": template,
                          "inputs": [{"name": "INPUT_BAM", "format": "bam"}],
                          "outputs": [{"name": "o", "files": ["*"]}]}}
        return [x for x in check_invariants(spec) if x["invariant"] == "I6.template_placeholders_declared"]
    assert _v("samtools view {INPUT_BAM} -o {OUTDIR}/out.bam") == []
    assert _v("samtools view {INPUT_BAM} -o {OUTPUT_BAM}") == []
    bad = _v("samtools view {INPUT_BAM} -o {OUPUT_DIR}/out.bam")
    assert bad and bad[0]["undeclared_placeholders"] == ["OUPUT_DIR"]
    assert "output directory" in bad[0]["message"]
