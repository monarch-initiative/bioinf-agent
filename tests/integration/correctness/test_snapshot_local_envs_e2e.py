"""
End-to-end drive of `snapshot_project` against this repo's own `envs/`
directory — the "point a local compute_env at our envs/" scenario.

Why integration/correctness (not honesty/L14):

  L14 pins the agent's COMMAND SURFACE (literal find argv, shell-injection
  neutralization, permission-gate ordering, one-level visibility). It
  proves the agent CAN'T cheat.

  This file proves the primitive WORKS against real on-disk data — a real
  conda env tree, with the symlink mess every conda env carries. Synthetic
  tmp_path fixtures hide that texture; real envs surface it.

The visibility contract these tests exercise: `file_name_only` is ONE
LEVEL only. Pointing snapshot at `envs/` yields just `envs/` + its
immediate child env dirs. To find `samtools` inside one of those envs,
the user (or a follow-up tool) declares `envs/<env>/bin/` as its own
directory entry. The agent never recurses past what the user explicitly
granted.

The schema (post-2026-05-31 redesign): list-of-named-blocks at every
level; per-dir access lives at projects[].compute_env_access[].directories[].
See agent/skills/projects_access.yaml.example for the full annotated shape.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from agent.skills import snapshot
from agent.skills.compute_access import PermissionDenied


# Resolve repo root from the test file. tests/integration/correctness/<file>
REPO_ROOT = Path(__file__).resolve().parents[3]
ENVS_DIR = REPO_ROOT / "envs"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _write_access(tmp_path: Path, payload: dict) -> Path:
    """Materialize a projects_access.yaml under tmp_path and return its path."""
    p = tmp_path / "projects_access.yaml"
    p.write_text(yaml.safe_dump(payload))
    return p


def _local_env_block(*, name: str = "this_repo") -> dict:
    """A single local compute_env block in the new schema shape."""
    return {"name": name, "type": "local", "container_upload_target": None}


def _project_block(*, name: str, env_name: str, dirs: list[dict],
                   description: str = "test") -> dict:
    """Build a project block in the new schema shape from a list of
    {path, permissions, description?} dicts."""
    return {
        "name": name,
        "description": description,
        "compute_envs": [env_name],
        "directories": [{**d, "env": env_name} for d in dirs],
    }


@pytest.fixture(scope="module")
def envs_root() -> Path:
    """Skip the whole module if the bootstrapped envs/ tree isn't here.
    This isn't a CI-machine test — it requires a real local conda env to
    have been built (e.g. via setup_core_test_data.sh)."""
    if not ENVS_DIR.is_dir() or not any(ENVS_DIR.iterdir()):
        pytest.skip("envs/ empty — bootstrap via scripts/setup_core_test_data.sh")
    return ENVS_DIR


@pytest.fixture
def access_envs_catalog(tmp_path, envs_root):
    """The baseline config: local compute_env pointed at envs/, single
    project with one file_name_only directory entry."""
    payload = {
        "compute_envs": [_local_env_block()],
        "projects": [_project_block(
            name="envs_inventory", env_name="this_repo",
            description="catalog view of this repo's envs/",
            dirs=[
                {"path": str(envs_root), "permissions": ["file_name_only"],
                 "description": "this repo's conda envs (local e2e test)"},
            ],
        )],
    }
    return _write_access(tmp_path, payload)


# ---------------------------------------------------------------------------
# 1. Happy path — drive against real envs/, prove the record shape
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_snapshot_envs_root_shows_only_top_level(access_envs_catalog, envs_root):
    """Pointing snapshot at envs/ yields envs/ ITSELF + its immediate child
    dirs (the env directories). Nothing deeper — that's the one-level
    visibility contract. The user has to declare a deeper path to see
    inside an env.

    This is intentionally a much smaller surface than the recursive walk:
    one declaration = one directory's contents, not a subtree."""
    rec = snapshot.snapshot_project(
        "envs_inventory", access_path=str(access_envs_catalog))

    assert "error" not in rec, rec
    assert rec["project"] == "envs_inventory"
    assert rec["compute_envs"] == ["this_repo"]

    entries = rec["entries"]
    assert rec["entry_count"] == len(entries)

    # Per-entry shape contract (now includes compute_env tag).
    assert all(
        set(e.keys()) == {"compute_env", "path", "size", "mtime", "type"}
        for e in entries)
    assert all(e["compute_env"] == "this_repo" for e in entries)
    assert all(e["path"].startswith("/") for e in entries)
    assert all(isinstance(e["size"], int) and e["size"] >= 0 for e in entries)
    assert all(isinstance(e["mtime"], float) for e in entries)

    # The root itself is included.
    paths = {e["path"] for e in entries}
    assert str(envs_root) in paths

    # Every other entry is an IMMEDIATE child of envs/. None are deeper.
    for e in entries:
        if e["path"] == str(envs_root):
            continue
        assert Path(e["path"]).parent == envs_root, (
            f"snapshot returned entry deeper than 1 level: {e['path']!r}")

    # The env dirs we have on disk should appear as dir entries.
    on_disk_envs = {d.name for d in envs_root.iterdir() if d.is_dir()}
    snapshot_dirs = {
        Path(e["path"]).name for e in entries
        if e["type"] == "dir" and e["path"] != str(envs_root)}
    assert on_disk_envs <= snapshot_dirs, (
        f"missing env dirs in snapshot: on_disk={on_disk_envs!r} "
        f"snapshot={snapshot_dirs!r}")


# ---------------------------------------------------------------------------
# 2. The pipeline-building handshake — programmatic tool discovery
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_declaring_an_env_bin_dir_makes_tools_discoverable(tmp_path, envs_root):
    """The user's bar: "as long as you can understand programmatically the
    file names, it's enough to build pipelines". With one-level visibility
    the user DECLARES `envs/<env>/bin/` as its own directory entry in the
    project's compute_env_access; then the binaries ARE discoverable by
    path. Pin the full handshake end-to-end."""
    bin_dir = next(
        (e / "bin" for e in envs_root.iterdir()
         if e.is_dir() and (e / "bin").is_dir()),
        None)
    if bin_dir is None:
        pytest.skip("no bootstrapped env has a bin/ dir")

    access = _write_access(tmp_path, {
        "compute_envs": [_local_env_block()],
        "projects": [_project_block(
            name="env_bin", env_name="this_repo",
            dirs=[
                {"path": str(bin_dir), "permissions": ["file_name_only"],
                 "description": "one env's bin dir"},
            ],
        )],
    })
    rec = snapshot.snapshot_project("env_bin", access_path=str(access))
    assert "error" not in rec, rec
    paths = [e["path"] for e in rec["entries"]]

    # Conda envs ship python in bin/.
    has_py = any(
        Path(p).name in ("python", "python3") or
        (Path(p).name.startswith("python3.") and Path(p).name[8:].isdigit())
        for p in paths)
    assert has_py, (
        f"no python binary in snapshot of {bin_dir} — conda layout broken? "
        f"first 10 paths: {paths[:10]!r}")


@pytest.mark.integration
def test_envs_root_snapshot_does_NOT_reveal_bin_contents(access_envs_catalog, envs_root):
    """Inverse: snapshotting envs/ (the parent) MUST NOT reveal
    `envs/<env>/bin/python`. The one-level contract under stress."""
    rec = snapshot.snapshot_project(
        "envs_inventory", access_path=str(access_envs_catalog))
    paths = {e["path"] for e in rec["entries"]}
    assert not any("/bin/python" in p for p in paths), (
        f"snapshot of envs/ leaked envs/<env>/bin/python — the one-level "
        f"contract was bypassed. Entries: {sorted(paths)!r}")
    assert not any("/lib/" in p for p in paths), (
        f"snapshot of envs/ leaked an env's lib/ contents (depth>=2).")


# ---------------------------------------------------------------------------
# 3. Permission matrix — `upload`-only and missing-from-dirs both refuse
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_upload_only_dir_is_invisible_to_snapshot(tmp_path, envs_root):
    """A directory declared `permissions: [upload]` is authorized for
    uploads but NOT for snapshot. The snapshot's snapshot_paths derivation
    must skip it; the resulting snapshot is a no-op (clean error)."""
    access = _write_access(tmp_path, {
        "compute_envs": [_local_env_block()],
        "projects": [_project_block(
            name="upload_only", env_name="this_repo",
            dirs=[
                {"path": str(envs_root), "permissions": ["upload"],
                 "description": "upload only, no listing"},
            ],
        )],
    })
    rec = snapshot.snapshot_project("upload_only", access_path=str(access))
    assert "error" in rec, f"expected no-op error, got {rec!r}"
    assert "file_name_only" in rec["error"]


@pytest.mark.integration
def test_path_outside_any_authorized_dir_denied(tmp_path, envs_root):
    """The gate refuses paths not declared in the project's directories[].
    The validator accepts the config (each dir is well-formed); the gate
    fires when a downstream caller passes a path outside the allowlist."""
    from agent.skills import compute_access

    access_path = _write_access(tmp_path, {
        "compute_envs": [_local_env_block()],
        "projects": [_project_block(
            name="declared", env_name="this_repo",
            dirs=[
                {"path": str(envs_root), "permissions": ["file_name_only"]},
            ],
        )],
    })
    access = compute_access.load_access(access_path)
    project = compute_access.get_project("declared", access)
    with pytest.raises(PermissionDenied) as exc:
        compute_access.check_permission(
            project, "this_repo", "/tmp/totally-unrelated-9d8e7c", "snapshot")
    assert "not authorized" in str(exc.value)


@pytest.mark.integration
def test_multiple_permissions_visible_to_snapshot(tmp_path, envs_root):
    """A dir declared `permissions: [file_name_only, upload]` SHOULD be
    walked by the snapshot — file_name_only is in the list, that's all
    that matters. Pin the inclusive semantics."""
    access = _write_access(tmp_path, {
        "compute_envs": [_local_env_block()],
        "projects": [_project_block(
            name="dual_perm", env_name="this_repo",
            dirs=[
                {"path": str(envs_root),
                 "permissions": ["file_name_only", "upload"],
                 "description": "list and upload"},
            ],
        )],
    })
    rec = snapshot.snapshot_project("dual_perm", access_path=str(access))
    assert "error" not in rec, rec
    assert rec["entry_count"] >= 1


# ---------------------------------------------------------------------------
# 4. Multi-path + multi-env aggregation
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_snapshot_aggregates_two_dirs_in_one_env(tmp_path, envs_root):
    """A project may declare multiple directories on the same env. The
    result aggregates one-level walks of each, all tagged with the same
    compute_env."""
    subdirs = [d for d in envs_root.iterdir() if d.is_dir()]
    if len(subdirs) < 2:
        pytest.skip(f"need >=2 bootstrapped envs (have {len(subdirs)})")
    e1, e2 = subdirs[:2]
    access = _write_access(tmp_path, {
        "compute_envs": [_local_env_block()],
        "projects": [_project_block(
            name="two_dirs", env_name="this_repo",
            dirs=[
                {"path": str(e1), "permissions": ["file_name_only"]},
                {"path": str(e2), "permissions": ["file_name_only"]},
            ],
        )],
    })
    rec = snapshot.snapshot_project("two_dirs", access_path=str(access))
    assert "error" not in rec, rec
    paths = {e["path"] for e in rec["entries"]}
    assert str(e1) in paths and str(e2) in paths
    # Every entry tagged with the one env we walked.
    assert all(e["compute_env"] == "this_repo" for e in rec["entries"])
    # Nothing deeper than depth 1 within either env.
    for p in paths:
        if p in (str(e1), str(e2)):
            continue
        parent = Path(p).parent
        assert parent in (e1, e2), (
            f"entry not at depth 1 under e1/e2: {p!r} parent={parent!r}")


@pytest.mark.integration
def test_multi_env_project_aggregates_across_envs(tmp_path, envs_root):
    """A project with TWO compute_env_access blocks — one per env. Each
    env contributes its own walk; the result is tagged so a downstream
    consumer can partition.

    For this test, both blocks point at the SAME on-disk envs_root (the
    only local env we have); we differentiate by env NAME, declared twice
    via two different compute_envs entries."""
    access = _write_access(tmp_path, {
        "compute_envs": [
            {"name": "env_a", "type": "local", "container_upload_target": None},
            {"name": "env_b", "type": "local", "container_upload_target": None},
        ],
        "projects": [{
            "name": "multi_env",
            "description": "two envs",
            "compute_envs": ["env_a", "env_b"],
            "directories": [
                {"path": str(envs_root),
                 "permissions": ["file_name_only"], "env": "env_a"},
                {"path": str(envs_root),
                 "permissions": ["file_name_only"], "env": "env_b"},
            ],
        }],
    })
    rec = snapshot.snapshot_project("multi_env", access_path=str(access))
    assert "error" not in rec, rec
    assert rec["compute_envs"] == ["env_a", "env_b"]
    env_tags = {e["compute_env"] for e in rec["entries"]}
    assert env_tags == {"env_a", "env_b"}, f"got {env_tags}"
    # Each env contributed an equal share (same dir walked twice).
    assert rec["per_env_counts"]["env_a"] == rec["per_env_counts"]["env_b"]


# ---------------------------------------------------------------------------
# 5. No-op / empty-project clean-error contract
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_empty_compute_env_access_returns_clean_error(tmp_path):
    """Project with no compute_env_access blocks at all → no-op error,
    not exception."""
    access = _write_access(tmp_path, {
        "compute_envs": [_local_env_block()],
        "projects": [{
            "name": "no_access",
            "description": "deliberately empty",
            "compute_envs": [],
        }],
    })
    rec = snapshot.snapshot_project("no_access", access_path=str(access))
    assert "error" in rec
    assert "compute_env_access" in rec["error"]


# ---------------------------------------------------------------------------
# 6. Symlink reality — conda envs are full of them
# ---------------------------------------------------------------------------

@pytest.mark.integration
def test_symlinks_in_conda_envs_dont_crash_and_resolve_to_target_type(
        tmp_path, envs_root):
    """conda envs put MANY symlinks in bin/ (python → python3 → python3.11;
    pip → pip3.x; etc). With one-level visibility we need to point the
    snapshot AT bin/ directly to actually exercise this.

    Pin the observed contract:
      - Path.is_file()/is_dir() follow symlinks, so a symlink-to-file is
        recorded as type='file' with the TARGET's size.
      - Broken symlinks raise on stat() and are SILENTLY DROPPED.
      - `type` is one of {file, dir, link, ?} — never anything else."""
    bin_dir = next(
        (e / "bin" for e in envs_root.iterdir()
         if e.is_dir() and (e / "bin").is_dir()),
        None)
    if bin_dir is None:
        pytest.skip("no bootstrapped env has a bin/ dir")

    access = _write_access(tmp_path, {
        "compute_envs": [_local_env_block()],
        "projects": [_project_block(
            name="env_bin", env_name="this_repo",
            dirs=[{"path": str(bin_dir), "permissions": ["file_name_only"]}],
        )],
    })
    rec = snapshot.snapshot_project("env_bin", access_path=str(access))
    types = {e["type"] for e in rec["entries"]}
    assert types <= {"file", "dir", "link", "?"}
    assert "link" not in types, (
        "current local walk follows symlinks via Path.is_file()/is_dir() — "
        f"any 'link' type entry indicates a behavior change. types={types!r}")
    files = [e for e in rec["entries"] if e["type"] == "file"]
    assert len(files) > 0, "bin/ produced zero file entries"
