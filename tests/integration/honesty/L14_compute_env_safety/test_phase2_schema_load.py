"""
L14 cheat-guards — Phase 2 schema extensions on compute_envs[].

Phase 2 adds three optional env-level blocks: `agent_scratch_target`,
`reference_data_targets`, and `slurm`. The validator MUST refuse every
mis-shaped declaration BEFORE any primitive that would consume it gets
wired. Otherwise a typo in the user's yaml ("max_corees_per_job: 9999")
silently passes the cap, or a `reference_data_targets` with traversal
in its `name` lets a primitive resolve `../../../etc/passwd`.

These tests pin the rejection paths:

  - agent_scratch_target: requires upload + fetch + exec in permissions;
    otherwise the slot doesn't mean what it says (the sandbox must be
    writable, fetchable, AND job-executable)
  - reference_data_targets: a LIST of named dir-access blocks; names
    safe-token (alnum + _- only); requires upload + exec; unique names
  - slurm: closed key set, positive-int caps, queue_default ∈
    allowed_queues, all non-empty
  - disjoint-subtree check: no declared path on the same env may be a
    prefix of (or equal to) another (container_upload / scratch / each
    refdata)
  - new permission tokens (`fetch`, `exec`) accepted in PERMISSIONS;
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
# 1. agent_scratch_target — single dir-access block
# ===========================================================================

class TestAgentScratchTarget:
    @pytest.mark.integration
    def test_happy_path_loads(self, tmp_path):
        env = _base_env(agent_scratch_target={
            "path": "/scratch/u/agent_workspace/",
            "permissions": ["upload", "fetch", "exec"],
            "description": "agent sandbox",
        })
        p = _write(tmp_path, _wrap(env))
        access = compute_access.load_access(p)
        loaded = compute_access.get_agent_scratch_target(
            compute_access.get_compute_env("cluster", access))
        assert loaded["path"] == "/scratch/u/agent_workspace/"
        assert set(loaded["permissions"]) == {"upload", "fetch", "exec"}

    @pytest.mark.integration
    def test_lookup_returns_none_when_undeclared(self, tmp_path):
        env = _base_env()  # no agent_scratch_target
        p = _write(tmp_path, _wrap(env))
        access = compute_access.load_access(p)
        assert compute_access.get_agent_scratch_target(
            compute_access.get_compute_env("cluster", access)) is None

    @pytest.mark.integration
    @pytest.mark.parametrize("missing", ["upload", "fetch", "exec"])
    def test_refuses_missing_required_permission(self, tmp_path, missing):
        # The sandbox is for upload + fetch + exec; missing any of the three
        # means the slot doesn't mean what its presence implies.
        perms = [p for p in ["upload", "fetch", "exec"] if p != missing]
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
            "permissions": ["upload", "fetch", "exec"],
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
# 2. reference_data_targets — list of named dir-access blocks
# ===========================================================================

class TestReferenceDataTargets:
    @pytest.mark.integration
    def test_happy_path_loads(self, tmp_path):
        env = _base_env(reference_data_targets=[
            {"name": "exomiser_data",
             "path": "/work/u/ref/exomiser/",
             "permissions": ["upload", "exec"],
             "description": "exomiser DB"},
            {"name": "gnomad_v4",
             "path": "/work/u/ref/gnomad_v4/",
             "permissions": ["upload", "exec"],
             "description": "gnomAD v4"},
        ])
        p = _write(tmp_path, _wrap(env))
        access = compute_access.load_access(p)
        env_blk = compute_access.get_compute_env("cluster", access)
        exo = compute_access.get_reference_data_target(env_blk, "exomiser_data")
        assert exo["path"] == "/work/u/ref/exomiser/"
        gnd = compute_access.get_reference_data_target(env_blk, "gnomad_v4")
        assert gnd["path"] == "/work/u/ref/gnomad_v4/"

    @pytest.mark.integration
    def test_lookup_unknown_name_raises_keyerror(self, tmp_path):
        env = _base_env(reference_data_targets=[
            {"name": "exomiser_data",
             "path": "/work/u/ref/exomiser/",
             "permissions": ["upload", "exec"]},
        ])
        p = _write(tmp_path, _wrap(env))
        access = compute_access.load_access(p)
        env_blk = compute_access.get_compute_env("cluster", access)
        with pytest.raises(KeyError) as exc:
            compute_access.get_reference_data_target(env_blk, "not_a_target")
        assert "not_a_target" in str(exc.value)
        assert "exomiser_data" in str(exc.value)  # surfaces available names

    @pytest.mark.integration
    def test_refuses_not_a_list(self, tmp_path):
        # A common typo: nesting one block directly rather than under a list.
        env = _base_env(reference_data_targets={
            "name": "x", "path": "/work/u/x/",
            "permissions": ["upload", "exec"],
        })
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "must be a LIST" in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("bad_name", [
        "",                       # empty
        "name with space",        # space
        "name/with/slash",        # path-like
        "..",                     # traversal token
        "name.with.dot",          # dot — disallowed because of traversal-adjacency
        "x" * 65,                 # too long
    ])
    def test_refuses_unsafe_name_token(self, tmp_path, bad_name):
        env = _base_env(reference_data_targets=[
            {"name": bad_name,
             "path": "/work/u/x/",
             "permissions": ["upload", "exec"]},
        ])
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        msg = str(exc.value)
        # Empty strings hit the "must be a non-empty string" branch first.
        assert "name" in msg and ("safe token" in msg or "non-empty" in msg)

    @pytest.mark.integration
    def test_refuses_duplicate_names(self, tmp_path):
        env = _base_env(reference_data_targets=[
            {"name": "dup", "path": "/work/u/a/",
             "permissions": ["upload", "exec"]},
            {"name": "dup", "path": "/work/u/b/",
             "permissions": ["upload", "exec"]},
        ])
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "duplicated" in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("missing", ["upload", "exec"])
    def test_refuses_missing_required_permission(self, tmp_path, missing):
        perms = [p for p in ["upload", "exec"] if p != missing]
        env = _base_env(reference_data_targets=[
            {"name": "x", "path": "/work/u/x/", "permissions": perms},
        ])
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "must include" in str(exc.value)
        assert missing in str(exc.value)


# ===========================================================================
# 3. slurm — closed-key config block with positive-int caps
# ===========================================================================

class TestSlurmBlock:
    def _good_slurm(self) -> dict:
        return {
            "queue_default": "general",
            "allowed_queues": ["general", "interact"],
            "account": "tislab",
            "max_cores_per_job": 16,
            "max_mem_gb_per_job": 64,
            "max_time_hours_per_job": 24,
        }

    @pytest.mark.integration
    def test_happy_path_loads(self, tmp_path):
        env = _base_env(slurm=self._good_slurm())
        p = _write(tmp_path, _wrap(env))
        access = compute_access.load_access(p)
        cfg = compute_access.get_slurm_config(
            compute_access.get_compute_env("cluster", access))
        assert cfg["queue_default"] == "general"
        assert cfg["max_cores_per_job"] == 16

    @pytest.mark.integration
    def test_lookup_returns_none_when_undeclared(self, tmp_path):
        env = _base_env()
        p = _write(tmp_path, _wrap(env))
        access = compute_access.load_access(p)
        assert compute_access.get_slurm_config(
            compute_access.get_compute_env("cluster", access)) is None

    @pytest.mark.integration
    def test_refuses_unknown_key(self, tmp_path):
        # A typo in a max_* key is the classic "the cap isn't real" trap.
        bad = self._good_slurm()
        bad["max_corees_per_job"] = 9999   # typo
        env = _base_env(slurm=bad)
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "unknown keys" in str(exc.value)
        assert "max_corees_per_job" in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("missing", [
        "queue_default", "allowed_queues", "account",
        "max_cores_per_job", "max_mem_gb_per_job", "max_time_hours_per_job",
    ])
    def test_refuses_missing_required_key(self, tmp_path, missing):
        bad = self._good_slurm()
        bad.pop(missing)
        env = _base_env(slurm=bad)
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "missing required keys" in str(exc.value)
        assert missing in str(exc.value)

    @pytest.mark.integration
    def test_refuses_queue_default_not_in_allowed_queues(self, tmp_path):
        bad = self._good_slurm()
        bad["queue_default"] = "ghost_queue"   # not in allowed_queues
        env = _base_env(slurm=bad)
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "queue_default" in str(exc.value)
        assert "allowed_queues" in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("field,bad_value", [
        ("max_cores_per_job", 0),         # zero rejected
        ("max_cores_per_job", -1),        # negative rejected
        ("max_cores_per_job", 1.5),       # float rejected
        ("max_cores_per_job", "16"),      # string-of-int rejected
        ("max_cores_per_job", True),      # bool rejected (subclass of int)
        ("max_mem_gb_per_job", 0),
        ("max_time_hours_per_job", -24),
    ])
    def test_refuses_non_positive_int_caps(self, tmp_path, field, bad_value):
        bad = self._good_slurm()
        bad[field] = bad_value
        env = _base_env(slurm=bad)
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "positive integer" in str(exc.value)
        assert field in str(exc.value)

    @pytest.mark.integration
    @pytest.mark.parametrize("field", ["queue_default", "account"])
    def test_refuses_empty_string_fields(self, tmp_path, field):
        bad = self._good_slurm()
        bad[field] = ""
        env = _base_env(slurm=bad)
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "non-empty string" in str(exc.value)
        assert field in str(exc.value)

    @pytest.mark.integration
    def test_refuses_allowed_queues_empty_list(self, tmp_path):
        bad = self._good_slurm()
        bad["allowed_queues"] = []
        env = _base_env(slurm=bad)
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "allowed_queues" in str(exc.value)
        assert "non-empty list" in str(exc.value)


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
                "permissions": ["upload", "fetch", "exec"],
                "description": "y"},
        )
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "PARENT" in str(exc.value) or "disjoint" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_refdata_overlaps_scratch(self, tmp_path):
        env = _base_env(
            agent_scratch_target={
                "path": "/scratch/u/agent_workspace/",
                "permissions": ["upload", "fetch", "exec"]},
            reference_data_targets=[
                {"name": "ref_inside_scratch",
                 "path": "/scratch/u/agent_workspace/ref/",
                 "permissions": ["upload", "exec"]},
            ],
        )
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "disjoint" in str(exc.value) or "PARENT" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_refdata_entries_overlap_each_other(self, tmp_path):
        env = _base_env(reference_data_targets=[
            {"name": "outer", "path": "/work/u/ref/",
             "permissions": ["upload", "exec"]},
            {"name": "inner", "path": "/work/u/ref/exomiser/",
             "permissions": ["upload", "exec"]},
        ])
        p = _write(tmp_path, _wrap(env))
        with pytest.raises(ConfigError) as exc:
            compute_access.load_access(p)
        assert "disjoint" in str(exc.value) or "PARENT" in str(exc.value)

    @pytest.mark.integration
    def test_refuses_two_targets_with_identical_path(self, tmp_path):
        env = _base_env(
            agent_scratch_target={
                "path": "/scratch/u/shared/",
                "permissions": ["upload", "fetch", "exec"]},
            reference_data_targets=[
                {"name": "same", "path": "/scratch/u/shared/",
                 "permissions": ["upload", "exec"]},
            ],
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
                "permissions": ["upload", "fetch", "exec"]},
            reference_data_targets=[
                {"name": "sibling", "path": "/scratch/u/ab/",
                 "permissions": ["upload", "exec"]},
            ],
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
                "permissions": ["upload", "fetch", "exec"]}),
            _base_env(name="env_b", agent_scratch_target={
                "path": "/shared/scratch/",   # same path, different env
                "permissions": ["upload", "fetch", "exec"]}),
        ], "projects": []}
        p = _write(tmp_path, manifest)
        # Should load clean.
        access = compute_access.load_access(p)
        assert len(access["compute_envs"]) == 2


# ===========================================================================
# 5. New permission tokens (`fetch`, `exec`) accepted in PERMISSIONS
# ===========================================================================

class TestNewPermissionTokens:
    @pytest.mark.integration
    @pytest.mark.parametrize("tok", ["fetch", "exec"])
    def test_token_in_PERMISSIONS(self, tok):
        assert tok in compute_access.PERMISSIONS

    @pytest.mark.integration
    def test_dir_block_accepts_new_tokens_in_project_directories(self, tmp_path):
        # The reusable dir-access block validator MUST accept `fetch` and
        # `exec` on a project's directories[] entries too — same vocabulary
        # everywhere. (No project-level grant logic Phase-2-side yet; this
        # just pins the token surface.)
        manifest = {
            "compute_envs": [_base_env(name="laptop", type="local",
                                       host=None, user=None)],
            "projects": [{
                "name": "p", "description": "x",
                "compute_env_access": [{
                    "compute_env": "laptop",
                    "directories": [{
                        "path": "/tmp/scratch/", "description": "scratch",
                        "permissions": ["fetch", "exec"]},
                    ],
                }],
            }],
        }
        # Clean a few keys that don't apply to local.
        manifest["compute_envs"][0].pop("host", None)
        manifest["compute_envs"][0].pop("user", None)
        p = _write(tmp_path, manifest)
        access = compute_access.load_access(p)   # must not raise
        assert access["projects"][0]["name"] == "p"


# ===========================================================================
# 6. Full happy-path: all three Phase 2 blocks together, disjoint paths
# ===========================================================================

@pytest.mark.integration
def test_complete_phase2_env_loads_and_all_lookups_return(tmp_path):
    """A complete Phase 2 compute_env with all three new blocks, paths
    disjoint, loads clean, and every lookup helper returns the right thing.
    The integration assertion — not just per-block."""
    env = _base_env(
        container_upload_target={
            "path": "/scratch/u/containers/",
            "permissions": ["upload"],
            "description": "sif tarballs"},
        agent_scratch_target={
            "path": "/scratch/u/agent_workspace/",
            "permissions": ["upload", "fetch", "exec"],
            "description": "agent sandbox"},
        reference_data_targets=[
            {"name": "exomiser_data",
             "path": "/work/u/ref/exomiser/",
             "permissions": ["upload", "exec"],
             "description": "Exomiser DB (~15 GB)"},
            {"name": "gnomad_v4",
             "path": "/work/u/ref/gnomad_v4/",
             "permissions": ["upload", "exec"],
             "description": "gnomAD v4 VCFs (~50 GB)"},
        ],
        slurm={
            "queue_default": "general",
            "allowed_queues": ["general", "interact"],
            "account": "tislab",
            "max_cores_per_job": 16,
            "max_mem_gb_per_job": 64,
            "max_time_hours_per_job": 24,
        },
    )
    p = _write(tmp_path, _wrap(env))
    access = compute_access.load_access(p)
    env_blk = compute_access.get_compute_env("cluster", access)

    # Lookups all return their respective shapes.
    sc = compute_access.get_agent_scratch_target(env_blk)
    assert sc["path"] == "/scratch/u/agent_workspace/"
    assert set(sc["permissions"]) >= {"upload", "fetch", "exec"}

    exo = compute_access.get_reference_data_target(env_blk, "exomiser_data")
    assert exo["path"] == "/work/u/ref/exomiser/"

    sl = compute_access.get_slurm_config(env_blk)
    assert sl["max_cores_per_job"] == 16
    assert sl["queue_default"] in sl["allowed_queues"]
