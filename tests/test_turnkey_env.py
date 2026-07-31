"""Regression tests for the TURNKEY cluster env fixes (bugs #7 + #9).

Anchored to the real Talos cluster seal: the frozen image ran 173 pytest green
inside apptainer on a cluster with a BARE command (no manual `export JAVA_HOME`).
Getting there took two fixes, both general (any JVM/GDAL/PROJ/R conda tool):

  #7  freeze bakes conda's activate.d env deltas (JAVA_HOME, GDAL_DATA, …) as ENV
      in the runtime image — the self-activating image otherwise skips activate.d,
      so `apptainer exec` sees no JAVA_HOME and the JVM/Spark gateway can't start.
  #9  the workflow renderer runs `apptainer exec --cleanenv` — apptainer passes
      HOST env vars into the container by default and they OVERRIDE the image ENV,
      so a login-node JAVA_HOME (/nas/.../java/17) clobbered our baked one and the
      gateway died "java: No such file or directory". --cleanenv makes the sealed
      environment the one that actually runs.
"""


# ---- #7 bake conda activate.d env into the runtime image ----------------------

def test_emit_dockerfile_bakes_activation_env_vars():
    from agent.skills.container_build import emit_dockerfile, PixiEngine
    df = emit_dockerfile(
        "debian:bookworm-slim", engine=PixiEngine("linux/amd64"),
        has_env_layer=True, longtail_steps=[],
        activation_env={
            "JAVA_HOME": "/work/.pixi/envs/default/lib/jvm",
            "GDAL_DATA": "/work/.pixi/envs/default/share/gdal",
        })
    assert 'ENV JAVA_HOME="/work/.pixi/envs/default/lib/jvm"' in df
    assert 'ENV GDAL_DATA="/work/.pixi/envs/default/share/gdal"' in df
    # baked in the RUNTIME stage (after the second FROM), not the builder
    runtime = df.split("FROM ")[-1]
    assert "ENV JAVA_HOME=" in runtime


def test_emit_dockerfile_no_activation_env_is_a_noop():
    from agent.skills.container_build import emit_dockerfile, PixiEngine
    df = emit_dockerfile(
        "debian:bookworm-slim", engine=PixiEngine("linux/amd64"),
        has_env_layer=True, longtail_steps=[], activation_env=None)
    assert "activate.d env deltas" not in df


def test_capture_activation_env_filters_to_prefix_and_path():
    """The capture keeps (a) vars pointing INTO the env prefix and (b) PATH; it
    drops volatile/unrelated host vars. Exercised via a fake exec so no container
    is needed."""
    from agent.skills.container_build import ContainerBuild, PixiEngine
    cb = ContainerBuild.__new__(ContainerBuild)
    cb.engine = PixiEngine("linux/amd64")
    cb.has_env_layer = True
    cb.cid = "fake"
    ep = cb.engine.env_prefix()
    fake_env = "\n".join([
        f"JAVA_HOME={ep}/lib/jvm",          # keep (points into prefix)
        f"GDAL_DATA={ep}/share/gdal",       # keep
        f"PATH={ep}/bin:/usr/bin",          # keep (PATH)
        "HOME=/root",                        # drop (volatile, not in prefix)
        "CONDA_PREFIX=" + ep,               # drop (handled by runtime_lines)
        "PIXI_PROJECT_ROOT=/work",          # drop (PIXI_)
        "LANG=C.UTF-8",                      # drop (not in prefix)
    ])
    cb.exec = lambda *a, **k: {"returncode": 0, "stdout": fake_env, "stderr": ""}
    got = cb.capture_activation_env()
    assert got.get("JAVA_HOME") == f"{ep}/lib/jvm"
    assert got.get("GDAL_DATA") == f"{ep}/share/gdal"
    assert "PATH" in got
    assert "HOME" not in got and "CONDA_PREFIX" not in got
    assert "PIXI_PROJECT_ROOT" not in got and "LANG" not in got


# ---- #9 apptainer exec --cleanenv (host env must not clobber the sealed env) ---

def test_render_workflow_apptainer_exec_uses_cleanenv():
    from agent.skills.workflow_render import render_workflow
    out = render_workflow(
        tool_name="talos",
        command="python -m pytest /opt/tools/talos/test/ > ${report}",
        inputs={},
        outputs={"report": "pytest_report.txt"},
        apptainer_sif="/work/u/CLAUDE_CONTAINERS/talos_v11_abc.sif",
        apptainer_module="apptainer/1.5.0",
        nextflow_module="nextflow/24.04.2",
        slurm={"time": "00:30:00", "mem": "8G", "cpus": 2},
        workflow_name="talos_pytest",
    )
    nf = out["main.nf"]
    assert "apptainer exec --cleanenv" in nf, (
        "apptainer exec must use --cleanenv so the sealed image env (baked "
        "JAVA_HOME) is not clobbered by host env vars")
