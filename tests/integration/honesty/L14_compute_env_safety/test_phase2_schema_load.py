"""
L14 cheat-guards — Phase 2 schema extensions on compute_envs[].

Phase 2 adds three optional env-level blocks: `agent_scratch_target`,
`agent_common_data_target`, and `slurm`. The validator MUST refuse
every mis-shaped declaration BEFORE any primitive that would consume
it gets wired. Otherwise a typo in the user's yaml
("max_corees_per_job: 9999") silently passes the cap, or a misshaped
scratch target lets a primitive happily upload to a path with no
download capability.

These tests pin the rejection paths:

  - agent_scratch_target: requires upload + download + exec in
    permissions; otherwise the slot doesn't mean what it says (the
    sandbox must be writable, downloadable, AND job-executable)
  - agent_common_data_target: singular sibling of scratch; same
    permission requirements
  - slurm: closed key set, positive-int caps, queue_default ∈
    allowed_queues, all non-empty
  - disjoint-subtree check: no declared path on the same env may be a
    prefix of (or equal to) another (container_upload / scratch /
    common_data)
  - new permission tokens (`download`, `exec`) accepted in PERMISSIONS;
    propagate through the dir-block validator
  - happy-path acceptance: a complete well-formed Phase 2 env loads,
    and the new lookup helpers return the right shape

Pattern mirrors test_snapshot_command_surface.py — adversarial yaml +
pytest.raises on ConfigError, plus a small happy-path stanza per block.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from agent.skills import compute_access
from agent.skills.compute_access import ConfigError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write(tmp_path: Path, data: dict) -> Path:
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(data))
    return p


def _base_env(**overrides) -> dict:
    """A minimal Phase 1 compute_env block — no Phase 2 blocks. Tests add
    individual Phase 2 blocks via **overrides to keep each test focused."""
    blk: dict = {
        "name": "cluster",
        "type": "ssh",
        "host": "hpc.example.edu",
        "user": "u",
        "container_upload_target": None,
    }
    blk.update(overrides)
    return blk


def _wrap(env: dict) -> dict:
    """Wrap a compute_env in the top-level manifest shape."""
    return {"compute_envs": [env], "projects": []}


# ===========================================================================
# 0. the compute_env block itself — closed key set
# ===========================================================================

class TestEnvClosedKeys:
    """The env block must reject unknown keys, exactly as its nested `slurm` /
    `data_transfer` blocks already do.

    It did not, and the two typos below are why that matters (tier 7):

      data_transfr:          the globus block is never seen, so the env silently
                             falls back to scp_head_node and GB-scale .sif pushes
                             go over the HEAD NODE — the precise thing choosing
                             globus exists to prevent. Silent, and wrong in the
                             one direction the user has a standing rule about.
      agent_scratch_targets: no scratch target, so run_step_on_cluster refuses
                             much later with "env has no scratch target" and
                             nothing connects that to the typo.

    A key we never read is a setting that silently does nothing. The cost of a
    typo should be an error naming the key, not a head-node transfer."""

    @pytest.mark.integration
    @pytest.mark.parametrize("bad_key", [
        "data_transfr",             # → silent scp fallback
        "agent_scratch_targets",    # plural — → no sandbox
        "agent_common_data_targets",
        "container_upload_targets",
        "slurmm",
        "lol_wut",
    ])
    def test_refuses_unknown_env_key(self, tmp_path, bad_key):
        env = _base_env(**{bad_key: {"path": "/x"}})
        with pytest.raises(ConfigError, match="unknown keys"):
            compute_access.load_access(_write(tmp_path, _wrap(env)))
        # the message must NAME the offending key — that is the whole point
        try:
            compute_access.load_access(_write(tmp_path, _wrap(env)))
        except ConfigError as e:
            assert bad_key in str(e)

    @pytest.mark.integration
    @pytest.mark.parametrize("good_key,value", [
        ("email", "a@b.org"),
        ("job_manager", "slurm"),
        ("agent_scratch_target", None),
        ("data_transfer", None),
    ])
    def test_allows_every_key_the_code_actually_reads(self, tmp_path, good_key, value):
        """The guard must not refuse a legitimate key — a closed set that is too
        tight breaks real configs, which is how closed-key checks get reverted."""
        env = _base_env(**{good_key: value})
        compute_access.load_access(_write(tmp_path, _wrap(env)))


# ===========================================================================
# 1. agent_scratch_target — single dir-access block
# ===========================================================================

class TestAgentScratchTarget:
    @pytest.mark.integration
    def test_happy_path_loads(self, tmp_path):
        env = _base_env(agent_scratch_target={
            "path": "/scratch/u/agent_workspace/",
            "permissions": ["upload", "download", "exec"],
            "description": "agent sandbox",
        })
        p = _write(tmp_path, _wrap(env))
        access = compute_access.load_access(p)
        loaded = compute_access.get_agent_scratch_target(
            compute_access.get_compute_env("cluster", access))
        assert loaded["path"] == "/scratch/u/agent_workspace/"
        assert set(loaded["permissions"]) == {"upload", "download", "exec"}

    @pytest.mark.integration
    def test_lookup_returns_none_when_undeclared(self, tmp_path):
        env = _base_env()  # no agent_scratch_target
        p = _write(tmp_path, _wrap(env))
        access = compute_access.load_access(p)
        assert compute_access.get_agent_scratch_target(
            compute_access.get_compute_env("cluster", access)) is None

    @pytest.mark.integration
    @pytest.mark.parametrize("missing", ["upload", "download", "exec"])
    def test_refuses_missing_required_permission(self, tmp_path, missing):
        # The sandbox is for upload + download + exec; missing any of the
        # three means the slot doesn't mean what its presence implies.
        perms = [p for p in ["upload", "download", "exec"] if p != missing]
        env = _base_env(agent_scratch_target={
            "path": "/scratch/u/agent_workspace/",
            "permissions": perms,
            "description": "broken",
        })
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "must include" in str(exc.value)
        assert missing in str(exc.value)

    @pytest.mark.integration
    def test_refuses_relative_path(self, tmp_path):
        env = _base_env(agent_scratch_target={
            "path": "relative/agent_workspace/",
            "permissions": ["upload", "download", "exec"],
            "description": "x",
        })
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "absolute path" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_non_mapping(self, tmp_path):
        env = _base_env(agent_scratch_target="just a string oops")
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "must be a mapping" in str(exc.value)


# ===========================================================================
# 2. agent_common_data_target — singular sibling of agent_scratch_target
# ===========================================================================

class TestAgentCommonDataTarget:
    @pytest.mark.integration
    def test_happy_path_loads(self, tmp_path):
        env = _base_env(agent_common_data_target={
            "path": "/work/u/ref/common/",
            "permissions": ["upload", "download", "exec"],
            "description": "shared reference data zone",
        })
        p = _write(tmp_path, _wrap(env))
        access = compute_access.load_access(p)
        loaded = compute_access.get_agent_common_data_target(
            compute_access.get_compute_env("cluster", access))
        assert loaded["path"] == "/work/u/ref/common/"
        assert set(loaded["permissions"]) == {"upload", "download", "exec"}

    @pytest.mark.integration
    def test_lookup_returns_none_when_undeclared(self, tmp_path):
        env = _base_env()  # no agent_common_data_target
        p = _write(tmp_path, _wrap(env))
        access = compute_access.load_access(p)
        assert compute_access.get_agent_common_data_target(
            compute_access.get_compute_env("cluster", access)) is None

    @pytest.mark.integration
    @pytest.mark.parametrize("missing", ["upload", "download", "exec"])
    def test_refuses_missing_required_permission(self, tmp_path, missing):
        perms = [p for p in ["upload", "download", "exec"] if p != missing]
        env = _base_env(agent_common_data_target={
            "path": "/work/u/ref/common/",
            "permissions": perms,
        })
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "must include" in str(exc.value)
        assert missing in str(exc.value)

    @pytest.mark.integration
    def test_refuses_relative_path(self, tmp_path):
        env = _base_env(agent_common_data_target={
            "path": "ref/common/",
            "permissions": ["upload", "download", "exec"],
        })
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "absolute path" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_non_mapping(self, tmp_path):
        env = _base_env(agent_common_data_target=["wrong shape", "list"])
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "must be a mapping" in str(exc.value)


# ===========================================================================
# 3. slurm — closed-key config block with positive-int caps
# ===========================================================================

class TestSlurmBlock:
    """The env-level `slurm:` block is the HPC's scheduler POLICY. ALL keys are
    OPTIONAL now (Longleaf CPU jobs need none) — the old required
    queue_default/allowed_queues/account/max_* model was a queue-SELECTION scheme
    Longleaf doesn't use. Email moved to the env-level `email:` field. Unknown keys
    still rejected; the GPU convention {partition, qos} is a closed sub-block."""

    def _good_slurm(self) -> dict:
        return {
            "account": "tislab",
            "partition": "general",
            "gpu": {"partition": "a100-gpu", "qos": "gpu_access"},
        }

    @pytest.mark.integration
    def test_happy_path_loads(self, tmp_path):
        env = _base_env(slurm=self._good_slurm())
        access = compute_access.load_access(_write(tmp_path, _wrap(env)))
        cfg = compute_access.get_slurm_config(
            compute_access.get_compute_env("cluster", access))
        assert cfg["account"] == "tislab"
        assert cfg["partition"] == "general"
        assert cfg["gpu"] == {"partition": "a100-gpu", "qos": "gpu_access"}

    @pytest.mark.integration
    def test_empty_block_loads(self, tmp_path):
        """ALL keys optional — an empty slurm block is valid (a cluster that needs
        no account/partition, like Longleaf for CPU jobs)."""
        env = _base_env(slurm={})
        access = compute_access.load_access(_write(tmp_path, _wrap(env)))
        assert compute_access.get_slurm_config(
            compute_access.get_compute_env("cluster", access)) == {}

    @pytest.mark.integration
    def test_lookup_returns_none_when_undeclared(self, tmp_path):
        env = _base_env()
        access = compute_access.load_access(_write(tmp_path, _wrap(env)))
        assert compute_access.get_slurm_config(
            compute_access.get_compute_env("cluster", access)) is None

    @pytest.mark.integration
    def test_refuses_unknown_key(self, tmp_path):
        # A typo in a real key (account → accont) must be caught, not silently
        # accepted as a setting that does nothing.
        bad = self._good_slurm()
        bad["accont"] = "tislab"   # typo of `account`
        env = _base_env(slurm=bad)
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(_write(tmp_path, _wrap(env)))
        assert "unknown keys" in str(exc.value)
        assert "accont" in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("removed_key,value", [
        ("max_cores_per_job", 16),
        ("max_mem_gb_per_job", 64),
        ("max_time_hours_per_job", 24),
        ("module_loads", ["apptainer/1.4.1"]),
    ])
    def test_refuses_removed_dead_knobs(self, tmp_path, removed_key, value):
        """The per-env resource caps and module_loads were REMOVED 2026-07-20 — they
        were accepted + validated here but never enforced/emitted (the "gate present,
        absent in effect" anti-pattern). A config that still sets one must now fail
        LOUD via the closed-key check, not silently do nothing."""
        bad = self._good_slurm()
        bad[removed_key] = value
        env = _base_env(slurm=bad)
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(_write(tmp_path, _wrap(env)))
        assert "unknown keys" in str(exc.value) and removed_key in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("field", ["account", "partition"])
    def test_refuses_empty_string_fields(self, tmp_path, field):
        bad = self._good_slurm()
        bad[field] = ""
        env = _base_env(slurm=bad)
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(_write(tmp_path, _wrap(env)))
        assert "non-empty string" in str(exc.value) and field in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("field", ["account", "partition"])
    def test_refuses_shell_metacharacters(self, tmp_path, field):
        """account/partition land in an SBATCH header — a metacharacter would be
        header injection. Refuse before it ever reaches the cluster."""
        bad = self._good_slurm()
        bad[field] = "general; rm -rf /"
        env = _base_env(slurm=bad)
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(_write(tmp_path, _wrap(env)))
        assert "shell metacharacters" in str(exc.value)

    @pytest.mark.integration
    def test_partition_may_be_comma_separated(self, tmp_path):
        """Longleaf allows targeting several GPU partitions (a100-gpu,l40-gpu)."""
        good = self._good_slurm()
        good["gpu"] = {"partition": "a100-gpu,l40-gpu", "qos": "gpu_access"}
        access = compute_access.load_access(_write(tmp_path, _wrap(_base_env(slurm=good))))
        cfg = compute_access.get_slurm_config(
            compute_access.get_compute_env("cluster", access))
        assert cfg["gpu"]["partition"] == "a100-gpu,l40-gpu"

    # --- GPU convention sub-block {partition, qos} ---------------------------

    @pytest.mark.integration
    @pytest.mark.parametrize("missing", ["partition", "qos"])
    def test_gpu_block_requires_both_partition_and_qos(self, tmp_path, missing):
        bad = self._good_slurm()
        bad["gpu"] = {"partition": "a100-gpu", "qos": "gpu_access"}
        del bad["gpu"][missing]
        env = _base_env(slurm=bad)
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(_write(tmp_path, _wrap(env)))
        assert "missing required keys" in str(exc.value) and missing in str(exc.value)

    @pytest.mark.integration
    def test_gpu_block_refuses_unknown_key(self, tmp_path):
        bad = self._good_slurm()
        bad["gpu"] = {"partition": "a100-gpu", "qos": "gpu_access", "count": 4}
        env = _base_env(slurm=bad)
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(_write(tmp_path, _wrap(env)))
        assert "unknown keys" in str(exc.value) and "count" in str(exc.value)

    @pytest.mark.integration
    def test_optional_keys_absent_loads_clean(self, tmp_path):
        # The whole point of optional keys: they may be omitted without
        # the loader complaining.
        good = self._good_slurm()
        # No mail_user (email is the env-level field, not a slurm key).
        env = _base_env(slurm=good)
        p = _write(tmp_path, _wrap(env))
        access = compute_access.load_access(p)
        cfg = compute_access.get_slurm_config(
            compute_access.get_compute_env("cluster", access))
        assert "mail_user" not in cfg

    @pytest.mark.integration
    def test_still_refuses_truly_unknown_key(self, tmp_path):
        # The typo defense still works — the closed-key set accepts only the keys
        # the code actually reads, never an arbitrary field.
        bad = self._good_slurm()
        bad["typo_extra_field"] = "evil"
        env = _base_env(slurm=bad)
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "unknown keys" in str(exc.value)
        assert "typo_extra_field" in str(exc.value)


# ===========================================================================
# 4. Disjoint-subtree check across all of an env's declared paths
# ===========================================================================

class TestDisjointSubtreeCheck:
    @pytest.mark.integration
    def test_refuses_scratch_overlaps_container_upload(self, tmp_path):
        # The container upload is a CHILD of scratch — breach of one grants
        # access to the other. Refused.
        env = _base_env(
            container_upload_target={
                "path": "/scratch/u/agent_workspace/containers/",
                "permissions": ["upload"],
                "description": "x"},
            agent_scratch_target={
                "path": "/scratch/u/agent_workspace/",
                "permissions": ["upload", "download", "exec"],
                "description": "y"},
        )
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "PARENT" in str(exc.value) or "disjoint" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_common_data_overlaps_scratch(self, tmp_path):
        env = _base_env(
            agent_scratch_target={
                "path": "/scratch/u/agent_workspace/",
                "permissions": ["upload", "download", "exec"]},
            agent_common_data_target={
                "path": "/scratch/u/agent_workspace/ref/",
                "permissions": ["upload", "download", "exec"]},
        )
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "disjoint" in str(exc.value) or "PARENT" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_two_targets_with_identical_path(self, tmp_path):
        env = _base_env(
            agent_scratch_target={
                "path": "/scratch/u/shared/",
                "permissions": ["upload", "download", "exec"]},
            agent_common_data_target={
                "path": "/scratch/u/shared/",
                "permissions": ["upload", "download", "exec"]},
        )
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "SAME path" in str(exc.value) or "disjoint" in str(exc.value)

    @pytest.mark.integration
    def test_trailing_slash_normalization_does_not_false_positive(self, tmp_path):
        # /scratch/u/a/ and /scratch/u/ab/ MUST NOT be flagged as overlap —
        # the disjoint check uses `+ "/"` boundary, not raw startswith.
        env = _base_env(
            agent_scratch_target={
                "path": "/scratch/u/a/",
                "permissions": ["upload", "download", "exec"]},
            agent_common_data_target={
                "path": "/scratch/u/ab/",
                "permissions": ["upload", "download", "exec"]},
        )
        p = _write(tmp_path, _wrap(env))
        # Should load clean.
        access = compute_access.load_access(p)
        assert compute_access.get_agent_scratch_target(
            compute_access.get_compute_env("cluster", access))["path"] == "/scratch/u/a/"

    @pytest.mark.integration
    def test_cross_env_overlap_is_NOT_checked(self, tmp_path):
        # The security boundary is the env. Two different envs may declare
        # overlapping paths (e.g. dev cluster vs prod cluster mapping to the
        # same NFS mount as far as path strings go) — the disjoint check
        # is intra-env only, not cross-env.
        manifest = {"compute_envs": [
            _base_env(name="env_a", agent_scratch_target={
                "path": "/shared/scratch/",
                "permissions": ["upload", "download", "exec"]}),
            _base_env(name="env_b", agent_scratch_target={
                "path": "/shared/scratch/",   # same path, different env
                "permissions": ["upload", "download", "exec"]}),
        ], "projects": []}
        p = _write(tmp_path, manifest)
        # Should load clean.
        access = compute_access.load_access(p)
        assert len(access["compute_envs"]) == 2


# ===========================================================================
# 5. New permission tokens (`download`, `exec`) accepted in PERMISSIONS
# ===========================================================================

class TestNewPermissionTokens:
    @pytest.mark.integration
    @pytest.mark.parametrize("tok", ["download", "exec"])
    def test_token_in_PERMISSIONS(self, tok):
        assert tok in compute_access.PERMISSIONS

    @pytest.mark.integration
    def test_dir_block_accepts_new_tokens_in_project_directories(self, tmp_path):
        # The reusable dir-access block validator MUST accept `download` and
        # `exec` on a project's directories[] entries too — same vocabulary
        # everywhere.
        manifest = {
            "compute_envs": [_base_env(name="laptop", type="local",
                                       host=None, user=None)],
            "projects": [{
                "name": "p", "description": "x",
                "compute_envs": ["laptop"],
                "directories": [{
                    "path": "/tmp/scratch/", "description": "scratch",
                    "permissions": ["download", "exec"], "env": "laptop"},
                ],
            }],
        }
        manifest["compute_envs"][0].pop("host", None)
        manifest["compute_envs"][0].pop("user", None)
        p = _write(tmp_path, manifest)
        access = compute_access.load_access(p)   # must not raise
        assert access["projects"][0]["name"] == "p"


# ===========================================================================
# 6. data_transfer block — wire protocol selector
# ===========================================================================

_GOOD_LOCAL_UUID = "11111111-1111-1111-1111-111111111111"
_GOOD_REMOTE_UUID = "22222222-2222-2222-2222-222222222222"


class TestDataTransferBlock:

    # --- happy paths --------------------------------------------------------

    @pytest.mark.integration
    def test_absent_block_defaults_to_scp(self, tmp_path):
        """Omitting data_transfer keeps the legacy scp_head_node behavior."""
        env = _base_env()
        p = _write(tmp_path, _wrap(env))
        access = compute_access.load_access(p)
        e = compute_access.get_compute_env("cluster", access)
        assert compute_access.get_data_transfer_kind(e) == "scp_head_node"
        assert compute_access.get_globus_endpoints(e) is None

    @pytest.mark.integration
    def test_scp_head_node_explicit_loads(self, tmp_path):
        env = _base_env(data_transfer={"type": "scp_head_node"})
        p = _write(tmp_path, _wrap(env))
        access = compute_access.load_access(p)
        e = compute_access.get_compute_env("cluster", access)
        assert compute_access.get_data_transfer_kind(e) == "scp_head_node"
        assert compute_access.get_globus_endpoints(e) is None

    @pytest.mark.integration
    def test_globus_with_all_fields_loads(self, tmp_path):
        env = _base_env(data_transfer={
            "type": "globus",
            "globus": {
                "local_endpoint_id":    _GOOD_LOCAL_UUID,
                "local_endpoint_name":  "user1-laptop",
                "remote_endpoint_id":   _GOOD_REMOTE_UUID,
                "remote_endpoint_name": "HPC RC DataMover",
            },
        })
        p = _write(tmp_path, _wrap(env))
        access = compute_access.load_access(p)
        e = compute_access.get_compute_env("cluster", access)
        assert compute_access.get_data_transfer_kind(e) == "globus"
        g = compute_access.get_globus_endpoints(e)
        assert g["local_endpoint_id"] == _GOOD_LOCAL_UUID
        assert g["remote_endpoint_id"] == _GOOD_REMOTE_UUID

    # --- rejection paths ----------------------------------------------------

    @pytest.mark.integration
    def test_unknown_type_refused(self, tmp_path):
        env = _base_env(data_transfer={"type": "rsync"})
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError, match="rsync"):
            compute_access.load_access(p)

    @pytest.mark.integration
    def test_unknown_top_level_key_refused(self, tmp_path):
        env = _base_env(data_transfer={
            "type": "scp_head_node",
            "unknown_field": 1,
        })
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError, match="unknown keys"):
            compute_access.load_access(p)

    @pytest.mark.integration
    def test_missing_type_refused(self, tmp_path):
        env = _base_env(data_transfer={"globus": {
            "local_endpoint_id":    _GOOD_LOCAL_UUID,
            "local_endpoint_name":  "x",
            "remote_endpoint_id":   _GOOD_REMOTE_UUID,
            "remote_endpoint_name": "y",
        }})
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError, match="missing required key 'type'"):
            compute_access.load_access(p)

    @pytest.mark.integration
    def test_globus_type_without_globus_block_refused(self, tmp_path):
        env = _base_env(data_transfer={"type": "globus"})
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError, match="requires a 'globus:' sub-block"):
            compute_access.load_access(p)

    @pytest.mark.integration
    @pytest.mark.parametrize("missing_key", [
        "local_endpoint_id", "local_endpoint_name",
        "remote_endpoint_id", "remote_endpoint_name",
    ])
    def test_globus_block_missing_required_field_refused(self, tmp_path,
                                                        missing_key):
        g = {
            "local_endpoint_id":    _GOOD_LOCAL_UUID,
            "local_endpoint_name":  "x",
            "remote_endpoint_id":   _GOOD_REMOTE_UUID,
            "remote_endpoint_name": "y",
        }
        del g[missing_key]
        env = _base_env(data_transfer={"type": "globus", "globus": g})
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError, match=missing_key):
            compute_access.load_access(p)

    @pytest.mark.integration
    @pytest.mark.parametrize("bad_uuid", [
        "not-a-uuid",
        "EACFD9EF-C4A5-11F0-80AC-0AFFCDD38B9D",   # uppercase rejected
        "11111111-1111-1111-1111",                # too short
        "",
        "11111111_1111_1111_1111_111111111111",   # underscores not dashes
    ])
    def test_globus_block_bad_uuid_refused(self, tmp_path, bad_uuid):
        env = _base_env(data_transfer={"type": "globus", "globus": {
            "local_endpoint_id":    bad_uuid,
            "local_endpoint_name":  "x",
            "remote_endpoint_id":   _GOOD_REMOTE_UUID,
            "remote_endpoint_name": "y",
        }})
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError, match="canonical UUID"):
            compute_access.load_access(p)

    @pytest.mark.integration
    def test_globus_block_empty_name_refused(self, tmp_path):
        env = _base_env(data_transfer={"type": "globus", "globus": {
            "local_endpoint_id":    _GOOD_LOCAL_UUID,
            "local_endpoint_name":  "",                 # empty
            "remote_endpoint_id":   _GOOD_REMOTE_UUID,
            "remote_endpoint_name": "y",
        }})
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError, match="non-empty string"):
            compute_access.load_access(p)

    @pytest.mark.integration
    def test_globus_block_unknown_subkey_refused(self, tmp_path):
        env = _base_env(data_transfer={"type": "globus", "globus": {
            "local_endpoint_id":    _GOOD_LOCAL_UUID,
            "local_endpoint_name":  "x",
            "remote_endpoint_id":   _GOOD_REMOTE_UUID,
            "remote_endpoint_name": "y",
            "fallback":             "scp_head_node",   # not allowed
        }})
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError, match="unknown keys.*fallback"):
            compute_access.load_access(p)

    @pytest.mark.integration
    def test_scp_with_globus_block_refused(self, tmp_path):
        """Mixed signals — type=scp but a globus block is present. We
        refuse rather than silently ignore (the user almost certainly
        meant type=globus and typoed the type)."""
        env = _base_env(data_transfer={
            "type": "scp_head_node",
            "globus": {
                "local_endpoint_id":    _GOOD_LOCAL_UUID,
                "local_endpoint_name":  "x",
                "remote_endpoint_id":   _GOOD_REMOTE_UUID,
                "remote_endpoint_name": "y",
            },
        })
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError,
                           match="globus.*also present|Set type=globus"):
            compute_access.load_access(p)


# ===========================================================================
# 7. Full happy-path: all three Phase 2 blocks together, disjoint paths
# ===========================================================================

@pytest.mark.integration
def test_complete_phase2_env_loads_and_all_lookups_return(tmp_path):
    """A complete Phase 2 compute_env with all three new blocks, paths
    disjoint, loads clean, and every lookup helper returns the right thing."""
    env = _base_env(
        container_upload_target={
            "path": "/scratch/u/containers/",
            "permissions": ["upload"],
            "description": "sif tarballs"},
        agent_scratch_target={
            "path": "/scratch/u/agent_workspace/",
            "permissions": ["upload", "download", "exec"],
            "description": "agent sandbox"},
        agent_common_data_target={
            "path": "/work/u/ref/common/",
            "permissions": ["upload", "download", "exec"],
            "description": "shared reference data (Exomiser, gnomAD, etc.)"},
        slurm={
            "account": "tislab",
            "partition": "general",
            "gpu": {"partition": "a100-gpu", "qos": "gpu_access"},
        },
    )
    p = _write(tmp_path, _wrap(env))
    access = compute_access.load_access(p)
    env_blk = compute_access.get_compute_env("cluster", access)

    # Lookups all return their respective shapes.
    sc = compute_access.get_agent_scratch_target(env_blk)
    assert sc["path"] == "/scratch/u/agent_workspace/"
    assert set(sc["permissions"]) >= {"upload", "download", "exec"}

    common = compute_access.get_agent_common_data_target(env_blk)
    assert common["path"] == "/work/u/ref/common/"
    assert set(common["permissions"]) >= {"upload", "download", "exec"}

    sl = compute_access.get_slurm_config(env_blk)
    assert sl["account"] == "tislab"
    assert sl["gpu"] == {"partition": "a100-gpu", "qos": "gpu_access"}


# ===========================================================================
# 6. job_manager — the batch-scheduler enum (slurm only; see VALID_JOB_MANAGERS)
# ===========================================================================

class TestJobManager:
    """`job_manager` names the scheduler an env runs jobs through. A CONTROLLED
    enum — the config validator refuses anything outside VALID_JOB_MANAGERS, which
    holds only the schedulers actually wired ('slurm').

    Only the REFUSAL is tested, because refusing is all that is wired: nothing reads
    job_manager to dispatch on, so there is no accessor and no default to assert. The
    un-implemented 'bash' value was removed 2026-07-20 (it violated the enum's own
    rule — a value ships WITH its submit/poll wiring), so it now refuses too; a future
    scheduler is added to VALID_JOB_MANAGERS together with its wiring."""

    @pytest.mark.integration
    @pytest.mark.parametrize("bad", ["bash", "pbs", "lsf", "SLURM", "sge", ""])
    def test_refuses_unsupported_scheduler(self, tmp_path, bad):
        env = _base_env(job_manager=bad)
        with pytest.raises(ConfigError, match="job_manager"):
            compute_access.load_access(_write(tmp_path, _wrap(env)))

