"""Regression tests for acquire-via-recipe: run a TOOL'S OWN data-acquisition
script on a compute node instead of re-implementing its URL list.

Anchored to the Talos Phase-2 finding: transcribing gather_files.sh's targets
silently dropped decompression (phenio.db.gz, ref.fa.gz), a chrMT->chrM rename,
and ClinVar. The rail runs the tool's recipe verbatim, wrapped with (1) a forced
non-interactive path (mask tmux — the recipe re-execs into a detached tmux server
SLURM would kill) and (2) a recipe-agnostic sha256 provenance sweep.

The functional tests EXECUTE the rendered runner in a temp dir with a fake recipe
so the tmux-mask + provenance sweep are proven, not just asserted structurally.
"""
import os
import subprocess
from pathlib import Path

import pytest

from agent.skills import acquire_data as ad
from agent.skills import workflow_render as wr


def _slurm_v():
    return wr._check_slurm({"time": "1-00:00:00", "mem": "4g"})


# ---- structural -------------------------------------------------------------

def test_runner_is_valid_bash_and_runs_the_recipe():
    s = ad.render_recipe_runner_script(
        name="ref_bundle", recipe_filename="gather_files.sh",
        mask_tools=("tmux",), slurm_v=_slurm_v(), email="")
    # runs the tool's OWN script (not a transcription)
    assert "bash gather_files.sh" in s
    # forces non-interactive path
    assert "masked from recipe PATH: tmux" in s
    # provenance sweep, skipping our own scaffolding + partials
    assert "sha256sum" in s and "shasum -a 256" in s
    assert "*.source.sha256|*.part|launcher.sh|gather_files.sh" in s
    r = subprocess.run(["bash", "-n", "/dev/stdin"], input=s,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_runner_without_mask_omits_shim():
    s = ad.render_recipe_runner_script(
        name="x", recipe_filename="get.sh", mask_tools=(),
        slurm_v=_slurm_v(), email="")
    assert "mktemp -d" not in s          # no shim built
    assert "bash get.sh" in s
    assert 'PATH="$RECIPE_PATH"' not in s


# ---- functional: the mask + sweep actually behave ---------------------------

@pytest.mark.skipif(os.name != "posix", reason="posix shell semantics")
def test_masked_tool_is_hidden_and_files_get_hashed(tmp_path):
    """Execute the rendered runner against a fake recipe. Prove: (a) a masked
    tool (fake `tmux`) is NOT visible to the recipe, (b) every produced file gets
    a .source.sha256 sidecar, (c) the recipe + launcher themselves are skipped."""
    # A fake tmux on PATH — the mask must hide it from the recipe.
    fakebin = tmp_path / "fakebin"
    fakebin.mkdir()
    (fakebin / "tmux").write_text("#!/usr/bin/env bash\necho REAL_TMUX\n")
    (fakebin / "tmux").chmod(0o755)

    # The tool's "recipe": records whether it can see tmux, then produces a file.
    workdir = tmp_path / "acq"
    workdir.mkdir()
    recipe = workdir / "gather_files.sh"
    recipe.write_text(
        "#!/usr/bin/env bash\n"
        "if command -v tmux >/dev/null 2>&1; then echo yes > tmux_visible; "
        "else echo no > tmux_visible; fi\n"
        "printf 'GENOME' > ref.fa\n"
        "printf 'FREQ'   > gnomad.bin\n"
    )

    # Strip the #SBATCH header (we run it as a plain script, not via sbatch).
    full = ad.render_recipe_runner_script(
        name="acq", recipe_filename="gather_files.sh",
        mask_tools=("tmux",), slurm_v=_slurm_v(), email="")
    body = "\n".join(l for l in full.splitlines() if not l.startswith("#SBATCH"))
    launcher = workdir / "launcher.sh"
    launcher.write_text(body)

    env = dict(os.environ)
    env["PATH"] = f"{fakebin}:{env['PATH']}"          # tmux IS on PATH...
    env["SLURM_SUBMIT_DIR"] = str(workdir)
    r = subprocess.run(["bash", str(launcher)], capture_output=True, text=True,
                       env=env, cwd=str(workdir))
    assert r.returncode == 0, r.stderr

    # (a) the recipe could NOT see tmux (it was masked out of PATH)
    assert (workdir / "tmux_visible").read_text().strip() == "no"
    # (b) every produced data file got a content sidecar
    assert (workdir / "ref.fa.source.sha256").is_file()
    assert (workdir / "gnomad.bin.source.sha256").is_file()
    # the sidecar holds a real 64-hex sha256
    h = (workdir / "ref.fa.source.sha256").read_text().strip()
    assert len(h) == 64 and all(c in "0123456789abcdef" for c in h)
    # (c) the recipe + launcher themselves are NOT hashed (scaffolding, skipped)
    assert not (workdir / "gather_files.sh.source.sha256").exists()
    assert not (workdir / "launcher.sh.source.sha256").exists()


# ---- orchestrator guardrails (no ssh) ---------------------------------------

def test_acquire_via_recipe_rejects_missing_recipe_file(tmp_path):
    out = ad.acquire_via_recipe(
        name="talos_large_files", recipe_local_path=str(tmp_path / "nope.sh"),
        compute_env="whatever", _pipeline_state=object())
    assert out.get("outcome") == "refused"
    assert out.get("code") == "acquire.recipe_missing"


def test_acquire_via_recipe_rejects_bad_name(tmp_path):
    recipe = tmp_path / "gather.sh"
    recipe.write_text("echo hi\n")
    out = ad.acquire_via_recipe(
        name="bad name!", recipe_local_path=str(recipe),
        compute_env="whatever", _pipeline_state=object())
    assert out.get("outcome") == "refused"
    assert out.get("code") == "acquire.bad_name"
