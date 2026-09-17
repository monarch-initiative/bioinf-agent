"""
L14 cheat-guards — cluster_partitions (SLURM partition discovery).

The probe exists so a GPU convention can be read off the cluster instead of
typed by hand. What it must NOT do is report the partition half of that
convention as though it were the whole thing: `workflow_render` requires
`slurm.gpu: {partition, qos}`, and a half-declared convention renders a
broken GPU header — the job lands on a CPU partition and the tool silently
falls back. So `qos_observable: False` is pinned here as a contract, not a
nicety.

Pinned:
  - the remote command shape (login shell + the exact pinned -o format)
  - the parser against realistic multi-row sinfo output, including the
    default-partition `*` marker and per-state row duplication
  - gres → gpus[] extraction, with and without a recorded GPU type
  - refusals BEFORE any ssh: unknown project/env, non-ssh env, unsafe pattern
  - a non-zero sinfo is an ERROR, never an empty partition list
"""
from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from agent.skills import cluster_partitions as cp


# Realistic output: `%P|%a|%l|%c|%m|%G|%D|%T`, one row per partition+state.
_SINFO_OUT = """\
general*|up|7-00:00:00|24|128000|(null)|40|idle
general*|up|7-00:00:00|24|128000|(null)|12|alloc
bigmem|up|14-00:00:00|64|2048000|(null)|4|idle
gpu|up|3-00:00:00|32|256000|gpu:a100:8|6|idle
gpu|up|3-00:00:00|32|256000|gpu:a100:8|2|mix
volta-gpu|up|3-00:00:00|32|192000|gpu:4|3|idle
"""


# `scontrol show partition -o` — one record per line, key=value. Note
# volta-gpu is deliberately ABSENT: a partition scontrol didn't describe must
# stay visible with qos_observed False rather than vanishing.
_SCONTROL_OUT = """\
PartitionName=general AllowGroups=ALL AllowAccounts=ALL AllowQos=ALL \
AllocNodes=ALL Default=YES QoS=N/A MaxTime=7-00:00:00
PartitionName=bigmem AllowGroups=ALL AllowAccounts=ALL AllowQos=ALL \
AllocNodes=ALL Default=NO QoS=N/A MaxTime=14-00:00:00
PartitionName=gpu AllowGroups=ALL AllowAccounts=ALL \
AllowQos=gpu_access,gpu_access_plus AllocNodes=ALL Default=NO QoS=N/A \
MaxTime=3-00:00:00
"""

_COMBINED_OUT = _SINFO_OUT + cp._SCONTROL_SENTINEL + "\n" + _SCONTROL_OUT


def _access(tmp_path: Path, *, env_type: str = "ssh") -> Path:
    env = {"name": "hpc", "type": env_type}
    if env_type == "ssh":
        env.update({"host": "hpc.example.edu", "user": "user1"})
    access = {
        "compute_envs": [env],
        "projects": [{"name": "demo", "compute_envs": ["hpc"],
                      "directories": []}],
    }
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(access, sort_keys=False))
    return p


def _fake_sinfo(stdout: str = _SINFO_OUT, rc: int = 0, stderr: str = ""):
    captured: list = []

    def fake_run(argv, *a, **kw):
        captured.append(argv)
        mock = MagicMock()
        mock.returncode = rc
        mock.stdout = stdout
        mock.stderr = stderr
        return mock
    return fake_run, captured


# ===========================================================================
# Pure parsing — no I/O
# ===========================================================================

class TestParser:
    @pytest.mark.integration
    def test_rows_fold_into_one_record_per_partition(self):
        parts = cp._parse_sinfo_output(_SINFO_OUT)
        names = [p["name"] for p in parts]
        # `general` appeared twice (idle + alloc) and must not become two.
        assert names == ["bigmem", "general", "gpu", "volta-gpu"]

    @pytest.mark.integration
    def test_default_marker_is_stripped_but_recorded(self):
        parts = {p["name"]: p for p in cp._parse_sinfo_output(_SINFO_OUT)}
        # The `*` is a FACT (what a job gets when it names no partition),
        # so it is recorded — but never left in the name a caller would use.
        assert parts["general"]["is_default"] is True
        assert parts["bigmem"]["is_default"] is False
        assert "*" not in parts["general"]["name"]

    @pytest.mark.integration
    def test_states_and_node_counts_accumulate_across_rows(self):
        parts = {p["name"]: p for p in cp._parse_sinfo_output(_SINFO_OUT)}
        assert sorted(parts["general"]["states"]) == ["alloc", "idle"]
        assert parts["general"]["node_count"] == 52        # 40 + 12

    @pytest.mark.integration
    def test_gres_yields_typed_gpus(self):
        parts = {p["name"]: p for p in cp._parse_sinfo_output(_SINFO_OUT)}
        assert parts["gpu"]["has_gpu"] is True
        assert parts["gpu"]["gpus"] == [{"type": "a100", "count_per_node": 8}]

    @pytest.mark.integration
    def test_untyped_gres_still_counts_as_gpu(self):
        # `gpu:4` — the site records no type. "Has GPUs" is still knowable;
        # the type is simply absent rather than invented.
        parts = {p["name"]: p for p in cp._parse_sinfo_output(_SINFO_OUT)}
        assert parts["volta-gpu"]["has_gpu"] is True
        assert parts["volta-gpu"]["gpus"] == [{"type": "", "count_per_node": 4}]

    @pytest.mark.integration
    def test_null_gres_is_not_a_gpu_partition(self):
        parts = {p["name"]: p for p in cp._parse_sinfo_output(_SINFO_OUT)}
        assert parts["general"]["has_gpu"] is False
        assert parts["general"]["gpus"] == []
        assert parts["general"]["gres"] == ""       # "(null)" never leaks out

    @pytest.mark.integration
    def test_malformed_rows_are_skipped_not_misaligned(self):
        # A row with the wrong field count must be dropped, never parsed by
        # position — that would silently assign a memory value to `gres`.
        bad = "general*|up|7-00:00:00|24\ngpu|up|1:00:00|8|64000|gpu:1|1|idle\n"
        parts = cp._parse_sinfo_output(bad)
        assert [p["name"] for p in parts] == ["gpu"]


# ===========================================================================
# Remote command shape
# ===========================================================================

class TestCommandShape:
    @pytest.mark.integration
    def test_uses_login_shell_and_the_pinned_format(self):
        cmd = cp._build_sinfo_cmd()
        # Login shell: SLURM's bin dir is often added by profile.d.
        assert cmd.startswith("bash -lc ")
        assert "sinfo -h -o" in cmd
        # The parser depends on this exact field order.
        assert cp._SINFO_FORMAT in cmd

    @pytest.mark.integration
    def test_sinfo_rc_is_not_laundered_by_the_second_probe(self):
        # Two commands in one shell: the LAST one's rc would normally win, so
        # scontrol's `|| true` would swallow a sinfo failure and turn a
        # cluster with no SLURM into a cluster with no partitions — a false
        # "no GPUs here". `|| exit $?` on sinfo is what stops that.
        cmd = cp._build_sinfo_cmd()
        assert "sinfo -h -o" in cmd
        sinfo_seg = cmd.split("echo")[0]
        assert "|| exit $?" in sinfo_seg
        # scontrol is enrichment and SHOULD degrade quietly.
        assert "scontrol show partition -o" in cmd
        assert cmd.rstrip("'\" ").endswith("|| true")

    @pytest.mark.integration
    def test_both_probes_ride_one_ssh_round_trip(self):
        cmd = cp._build_sinfo_cmd()
        assert cmd.startswith("bash -lc ")
        assert cp._SCONTROL_SENTINEL in cmd

    @pytest.mark.integration
    def test_sentinel_is_shell_safe(self):
        # The sentinel was `###BIOINF_SCONTROL###`. Bash starts a comment at
        # any word beginning with `#`, so `echo ###X###; scontrol …` commented
        # out the rest of the line — silently dropping the SECOND PROBE. Every
        # assertion above still passed, because none of them runs a shell; the
        # live cluster call reported qos_observable=False with six GPU
        # partitions "unobserved".
        assert not cp._SCONTROL_SENTINEL.startswith("#")

    @pytest.mark.integration
    def test_command_really_survives_a_shell(self):
        # The guard the string assertions could not give. Run the ACTUAL
        # command through a real shell with sinfo/scontrol stubbed on PATH,
        # and require BOTH probes' output to come back. This is what catches
        # a quoting bug that every string check reads as fine.
        import os
        import shutil
        import subprocess as sp
        import tempfile
        bash = shutil.which("bash")
        if not bash:
            pytest.skip("no bash on PATH")
        with tempfile.TemporaryDirectory() as d:
            for name, body in (("sinfo", _SINFO_OUT),
                               ("scontrol", _SCONTROL_OUT)):
                p = Path(d) / name
                p.write_text("#!/bin/sh\ncat <<'XEOF'\n" + body + "XEOF\n")
                p.chmod(0o755)
            env = dict(os.environ, PATH=f"{d}:{os.environ['PATH']}")
            # _build_sinfo_cmd returns `bash -lc '<inner>'`; run it as-is.
            res = sp.run(cp._build_sinfo_cmd(), shell=True, env=env,
                         capture_output=True, text=True, timeout=60)
        assert res.returncode == 0, res.stderr
        sinfo_text, scontrol_text = cp._split_probe_output(res.stdout)
        assert "general*" in sinfo_text, "sinfo half missing"
        assert "PartitionName=gpu" in scontrol_text, (
            "scontrol half missing — the second probe did not run")


# ===========================================================================
# The half-answer contract
# ===========================================================================

class TestGpuConvention:
    @pytest.mark.integration
    def test_candidates_pair_partition_with_each_allowed_qos(self, monkeypatch,
                                                             tmp_path):
        fake, _ = _fake_sinfo(stdout=_COMBINED_OUT)
        monkeypatch.setattr(subprocess, "run", fake)
        out = cp.cluster_partitions("demo", "hpc",
                                    access_path=str(_access(tmp_path)))
        assert "error" not in out, out
        assert out["qos_observable"] is True
        pairs = {(c["partition"], c["qos"])
                 for c in out["gpu_convention_candidates"]}
        # Both allowed QoS values on the gpu partition become candidates.
        assert ("gpu", "gpu_access") in pairs
        assert ("gpu", "gpu_access_plus") in pairs
        # The GPU type rides along, because "has a GPU" and "has an A100"
        # are different answers to a sizing question.
        a100 = [c for c in out["gpu_convention_candidates"]
                if c["partition"] == "gpu"][0]
        assert a100["gpu_types"] == ["a100"]

    @pytest.mark.integration
    def test_unobserved_qos_yields_no_candidate_but_stays_visible(
            self, monkeypatch, tmp_path):
        # volta-gpu is absent from the scontrol output. Half a convention
        # renders a broken GPU header, so it must produce NO candidate — but
        # the gap has to be visible, not silently dropped.
        fake, _ = _fake_sinfo(stdout=_COMBINED_OUT)
        monkeypatch.setattr(subprocess, "run", fake)
        out = cp.cluster_partitions("demo", "hpc",
                                    access_path=str(_access(tmp_path)))
        assert "volta-gpu" not in {c["partition"]
                                   for c in out["gpu_convention_candidates"]}
        volta = [p for p in out["partitions"] if p["name"] == "volta-gpu"][0]
        assert volta["has_gpu"] is True
        assert volta["qos_observed"] is False

    @pytest.mark.integration
    def test_unrestricted_qos_is_not_a_candidate_and_not_a_gap(
            self, monkeypatch, tmp_path):
        # AllowQos=ALL constrains nothing — there is no qos to NAME, so no
        # candidate. But that is a different fact from "we never looked",
        # and the record must keep them apart.
        fake, _ = _fake_sinfo(stdout=_COMBINED_OUT)
        monkeypatch.setattr(subprocess, "run", fake)
        out = cp.cluster_partitions("demo", "hpc",
                                    access_path=str(_access(tmp_path)))
        gen = [p for p in out["partitions"] if p["name"] == "general"][0]
        assert gen["qos_observed"] is True          # we DID look
        assert gen["qos_unrestricted"] is True      # and it constrains nothing
        assert gen["allowed_qos"] == []

    @pytest.mark.integration
    def test_missing_scontrol_degrades_without_failing(self, monkeypatch,
                                                       tmp_path):
        # scontrol restricted or absent → the hardware half still lands, and
        # qos is reported as unobserved rather than as "none".
        fake, _ = _fake_sinfo(stdout=_SINFO_OUT)      # no sentinel at all
        monkeypatch.setattr(subprocess, "run", fake)
        out = cp.cluster_partitions("demo", "hpc",
                                    access_path=str(_access(tmp_path)))
        assert "error" not in out, out
        assert out["partition_count"] == 4
        assert out["qos_observable"] is False
        assert out["gpu_convention_candidates"] == []
        assert all(p["qos_observed"] is False for p in out["partitions"])

    @pytest.mark.integration
    def test_default_partition_is_surfaced(self, monkeypatch, tmp_path):
        fake, _ = _fake_sinfo()
        monkeypatch.setattr(subprocess, "run", fake)
        out = cp.cluster_partitions("demo", "hpc",
                                    access_path=str(_access(tmp_path)))
        assert out["default_partition"] == "general"


# ===========================================================================
# Refusals — all BEFORE any ssh
# ===========================================================================

class TestRefusals:
    @pytest.mark.integration
    def test_unsafe_pattern_never_reaches_ssh(self, monkeypatch, tmp_path):
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        out = cp.cluster_partitions("demo", "hpc", "gpu; rm -rf /",
                                    access_path=str(_access(tmp_path)))
        assert "error" in out
        assert not called, "a smuggled command reached subprocess"

    @pytest.mark.integration
    def test_project_without_env_access_is_refused(self, monkeypatch,
                                                   tmp_path):
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        access = {
            "compute_envs": [{"name": "hpc", "type": "ssh",
                              "host": "h.example.edu", "user": "u"}],
            "projects": [{"name": "demo", "compute_envs": [],
                          "directories": []}],
        }
        p = tmp_path / "projects_access.yaml"
        p.write_text(yaml.safe_dump(access, sort_keys=False))
        out = cp.cluster_partitions("demo", "hpc", access_path=str(p))
        assert "error" in out
        assert not called

    @pytest.mark.integration
    def test_local_env_is_refused(self, monkeypatch, tmp_path):
        called = []
        monkeypatch.setattr(subprocess, "run",
                            lambda *a, **kw: called.append(a) or MagicMock())
        out = cp.cluster_partitions(
            "demo", "hpc", access_path=str(_access(tmp_path,
                                                   env_type="local")))
        assert "error" in out
        assert "ssh" in out["error"]
        assert not called

    @pytest.mark.integration
    def test_nonzero_sinfo_is_an_error_not_an_empty_list(self, monkeypatch,
                                                        tmp_path):
        # The dangerous failure: reporting "no partitions" (and therefore
        # "no GPUs here") because sinfo blew up.
        fake, _ = _fake_sinfo(stdout="", rc=127,
                              stderr="bash: sinfo: command not found")
        monkeypatch.setattr(subprocess, "run", fake)
        out = cp.cluster_partitions("demo", "hpc",
                                    access_path=str(_access(tmp_path)))
        assert "error" in out
        assert "partitions" not in out

    @pytest.mark.integration
    def test_pattern_filters_client_side(self, monkeypatch, tmp_path):
        fake, _ = _fake_sinfo()
        monkeypatch.setattr(subprocess, "run", fake)
        out = cp.cluster_partitions("demo", "hpc", "gpu",
                                    access_path=str(_access(tmp_path)))
        assert [p["name"] for p in out["partitions"]] == ["gpu", "volta-gpu"]
