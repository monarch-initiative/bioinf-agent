"""A render refusal names the cause and every field to change, in one answer.

An orphan artifact is usually a how-to command no sealed step matched, so nothing observed
what it writes; the refusal says so and names the command. A stage sizing the Nextflow
files cannot carry is reported for every bad field at once, not one per render.
"""
from __future__ import annotations

import pytest
from pipeline_fixtures import TEMPLATES, sealed_rnaseq_spec

from agent.skills import pipeline_record as pr
from agent.skills import pipeline_render_nextflow as nf
from agent.skills.pipeline_render import PipelineRenderError, render_pipeline_files


def test_an_orphan_names_the_command_no_sealed_step_matched():
    """The how-to's first command is rewritten so it matches no sealed step and names an
    output the command text does not show as a target."""
    spec = sealed_rnaseq_spec()
    cmds = list(TEMPLATES)
    cmds[0] = "hisat2 -x {HISAT2_INDEX} -U {READS} -S {OUTPUT_DIR}/aligned.sam --new-flag"
    spec = spec.model_copy(update={"usage": spec.usage.model_copy(update={"command_template": cmds})})
    with pytest.raises(pr.PipelineDerivationError) as e:
        pr.derive_pipeline_record(spec, name="x")
    assert e.value.code == "pipeline.orphan_artifact"
    assert "no sealed step matched how-to command 1" in e.value.error
    assert "aligned.sam" in e.value.error and "--new-flag" in e.value.error
    assert "literal command a sealed step ran" in e.value.remedy and "`-o`" in e.value.remedy


def test_every_bad_sizing_field_is_reported_at_once():
    rec = pr.derive_pipeline_record(sealed_rnaseq_spec(), name="x",
                                    resources={rec_stage: {"mem": "8 GB", "time": "2h"}
                                               for rec_stage in ["HISAT2"]})
    problems = nf.form_problems(rec)
    assert len(problems) == 2
    assert problems[0].startswith("stage HISAT2: mem='8 GB'") and "time='2h'" in problems[1]
    with pytest.raises(PipelineRenderError) as e:
        render_pipeline_files(rec)
    assert e.value.code == "pipeline.form_refused"
    assert "mem='8 GB'" in e.value.error and "time='2h'" in e.value.error


def test_a_well_sized_record_has_no_form_problems():
    rec = pr.derive_pipeline_record(sealed_rnaseq_spec(), name="x",
                                    resources={"HISAT2": {"mem": "8G", "time": "02:00:00"}})
    assert nf.form_problems(rec) == []
