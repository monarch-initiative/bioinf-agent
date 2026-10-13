"""A failed self-test trial says what the trial left behind, names a kill, and names the
paths the image could not see.

A how-to that redirects a tool's stderr into its own log leaves the cause in the trial's
scratch dir, not in the runner's stderr; a trial the runner killed is reported as killed at
the limit, not as "rc=-1"; and at the image locus, where only slot paths are mounted, an
absolute path in the command that exists on this host but under no mount is named with the
slot remedy. The host locus has no mounts and no such list.
"""
from __future__ import annotations

from pathlib import Path

from agent.skills import spec_writer as SW


def _spec(template: str, subs: dict) -> dict:
    return {"usage": {"description": "d", "command_template": template,
                      "inputs": [{"name": k} for k in subs],
                      "outputs": [{"name": "bam", "files": ["{OUTPUT_DIR}/{SAMPLE}.bam"]}],
                      "trials": [{"name": "t1", "substitutions": subs}]},
            "conda_env": "e"}


class _KilledRunner:
    """Writes the log the template would have written, then reports the runner's kill."""
    is_image_runner = True

    def __init__(self, mounts):
        self.mounts = mounts

    def run_in_env(self, env_name, command, timeout=None, watch_dir=None, **kw):
        (Path(watch_dir) / "S1.hisat2.log").write_text(
            '(ERR): "/x/idx/chr22" does not exist\nExiting now ...\n')
        (Path(watch_dir) / "big.bin").write_bytes(b"\0" * 70000)
        return {"returncode": -1, "stdout": "", "stderr": " [killed: timeout]"}


def test_a_killed_trial_names_the_limit_its_logs_and_the_unmounted_paths(tmp_path):
    fastq_dir = tmp_path / "reads"; fastq_dir.mkdir()
    fastq = fastq_dir / "s1.fq.gz"; fastq.write_text("x")
    idx_dir = tmp_path / "idx"; idx_dir.mkdir()
    idx = idx_dir / "chr22"; idx.write_text("x")
    spec = _spec(f"hisat2 -x {idx} -U {{FASTQ}} 2>{{OUTPUT_DIR}}/{{SAMPLE}}.hisat2.log | "
                 f"samtools sort -o {{OUTPUT_DIR}}/{{SAMPLE}}.bam",
                 {"FASTQ": str(fastq), "SAMPLE": "S1"})
    r = SW.self_test_usage(spec, _KilledRunner(mounts=[(str(fastq_dir), str(fastq_dir))]))
    assert r["ok"] is False
    trial = r["trials"][0]
    assert trial["killed_on_timeout"] is True
    assert f"killed at the self-test's {SW.TRIAL_TIMEOUT_S} s limit" in trial["reason"]
    assert {f["path"] for f in trial["scratch_files"]} == {"S1.hisat2.log", "big.bin"}
    assert "does not exist" in trial["scratch_tails"]["S1.hisat2.log"]
    assert "big.bin" not in trial["scratch_tails"]                 # too large, not text
    assert trial["paths_not_in_image"] == [str(idx)]              # the FASTQ dir is mounted, the index is not
    assert "write each as a slot" in trial["hint"] and str(idx) in trial["hint"]


class _HostRunner(_KilledRunner):
    is_image_runner = False

    def __init__(self):
        pass                                        # no `mounts`: the host sees everything


def test_the_host_locus_reports_the_logs_but_has_no_unmounted_paths(tmp_path):
    fastq = tmp_path / "s1.fq.gz"; fastq.write_text("x")
    idx = tmp_path / "chr22"; idx.write_text("x")
    spec = _spec(f"hisat2 -x {idx} -U {{FASTQ}} 2>{{OUTPUT_DIR}}/{{SAMPLE}}.hisat2.log",
                 {"FASTQ": str(fastq), "SAMPLE": "S1"})
    trial = SW.self_test_usage(spec, _HostRunner())["trials"][0]
    assert trial["killed_on_timeout"] is True and "S1.hisat2.log" in trial["scratch_tails"]
    assert "paths_not_in_image" not in trial and "hint" not in trial


def test_a_plain_failure_is_not_called_a_kill():
    class _Runner:
        is_image_runner = False

        def run_in_env(self, env_name, command, timeout=None, watch_dir=None, **kw):
            return {"returncode": 2, "stdout": "", "stderr": "boom"}
    trial = SW.self_test_usage(_spec("tool {IN} -o {OUTPUT_DIR}/{SAMPLE}.bam", {"IN": "/a", "SAMPLE": "S"}),
                               _Runner())["trials"][0]
    assert "killed_on_timeout" not in trial and "rc=2" in trial["reason"]
    assert trial["scratch_files"] == [] and trial["scratch_tails"] == {}
