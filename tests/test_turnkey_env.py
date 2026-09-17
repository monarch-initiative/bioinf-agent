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
    assert "--nv" not in nf, (
        "a CPU job must not carry --nv — on a node with no NVIDIA driver it is an "
        "error, not a no-op")


# ---- #10 apptainer exec --nv (a GPU job must be able to SEE the GPU) -----------

def _render_gpu(gpus: int) -> str:
    from agent.skills.workflow_render import render_workflow
    return render_workflow(
        tool_name="basecaller",
        command="dorado basecaller ${model} ${reads} > ${calls}",
        inputs={"model": "/ref/model", "reads": "/data/reads.pod5"},
        outputs={"calls": "calls.bam"},
        apptainer_sif="/work/u/CLAUDE_CONTAINERS/dorado_abc.sif",
        apptainer_module="apptainer/1.5.0",
        nextflow_module="nextflow/24.04.2",
        slurm={"time": "01:00:00", "mem": "16G", "cpus": 4, "gpus": gpus,
               "partition": "gpu", "qos": "gpu_access"},
        workflow_name="dorado_basecall",
    )["main.nf"]


def test_a_gpu_job_binds_the_device_into_the_container():
    """The renderer used to allocate a GPU and then hide it.

    The launcher emits `#SBATCH --gres=gpu:N`, so the job LANDS on a GPU node — and
    `apptainer exec` ran without --nv, which is what binds the driver userspace into
    the container. Measured on a real GPU node (GTX 1080, driver 580.126.20):

        apptainer exec --cleanenv       nvidia-smi  →  FATAL: not found in $PATH, rc=255
        apptainer exec --nv --cleanenv  nvidia-smi  →  NVIDIA GeForce GTX 1080, rc=0

    The quiet part: /dev/nvidia0 is visible inside the container in BOTH cases. The
    device node is bound either way; --nv adds libcuda and the utilities. So a tool
    that checks for the device finds it, proceeds, and falls back to CPU (or dies in
    dlopen) — having consumed the scarcest allocation the cluster has.
    """
    nf = _render_gpu(1)
    assert "apptainer exec --nv --cleanenv" in nf, (
        f"a job that asked for a GPU must run the container with --nv: {nf}")


def test_the_env_gpu_convention_reaches_the_header_and_the_exec_line():
    """The layer ABOVE the renderer, which is where a production GPU job actually
    enters: submit_workflow_job → render_workflow_files → _resolve_slurm_and_email →
    render_workflow. The merge is what turns `gpus: 1` into the cluster's own
    partition/qos convention, and until now nothing tested its GPU branch at all —
    the live GPU proof went through `render_workflow` with partition/qos hand-passed,
    i.e. it reproduced this function's OUTPUT without running it.
    """
    from agent.skills import submit_workflow as SW
    env = {"name": "hpc", "slurm": {"account": "acct", "partition": "general",
                                    "gpu": {"partition": "gpu", "qos": "gpu_access"}}}
    merged, _, _ = SW._resolve_slurm_and_email({"time": "01:00:00", "mem": "8G", "gpus": 2}, env)
    assert merged["partition"] == "gpu"        # NOT the CPU default "general"
    assert merged["qos"] == "gpu_access"
    assert merged["account"] == "acct"

    out = SW.render_workflow_files(
        tool_name="t", command="tool ${x} > ${y}",
        inputs={"x": "/d/in"}, outputs={"y": "out.txt"},
        apptainer_sif="/w/t.sif", apptainer_module="apptainer/1.5.0",
        nextflow_module="nextflow/24.04.2",
        slurm={"time": "01:00:00", "mem": "8G", "gpus": 2},
        workflow_name="w", env=env)
    assert "#SBATCH --gres=gpu:2" in out["launcher.sh"]
    assert "#SBATCH --partition=gpu" in out["launcher.sh"]
    assert "apptainer exec --nv --cleanenv" in out["main.nf"]


def test_a_gpu_job_on_an_env_with_no_gpu_convention_renders_and_states_it():
    """This test used to assert the OPPOSITE — that the same call raises — and
    argued the refusal was "the honest state, not a bug to route around."

    The premise was that an env declaring no gpu convention cannot express which
    partition a GPU job needs, so guessing is how a job sits in the wrong queue
    forever. What that missed: naming a partition is not always the better guess.
    On a cluster whose scheduler places gres requests itself, handing it a
    partition constrains the search and SLOWS placement, so `undeclared` is the
    correct submission there, not a broken one. A cluster-shaped judgement cannot
    be a hard requirement, and a config key that is unfillable-but-required is
    just an unreachable feature.

    Refusing was also never the only honest option — it was the option that
    reported nothing. Stating which of the four placement states a job is in
    keeps the fact and drops the veto.
    """
    from agent.skills import submit_workflow as SW
    env = {"name": "hpc", "slurm": {"account": "acct"}}
    merged, _, placement = SW._resolve_slurm_and_email(
        {"time": "01:00:00", "mem": "8G", "gpus": 1}, env)
    assert placement["state"] == "undeclared"
    assert merged["account"] == "acct"          # the rest of the policy still merges

    out = SW.render_workflow_files(
        tool_name="t", command="tool ${x} > ${y}",
        inputs={"x": "/d/in"}, outputs={"y": "out.txt"},
        apptainer_sif="/w/t.sif", apptainer_module="apptainer/1.5.0",
        nextflow_module="nextflow/24.04.2",
        slurm={"time": "01:00:00", "mem": "8G", "gpus": 1},
        workflow_name="w", env=env)
    launcher = out["launcher.sh"]
    assert "#SBATCH --gres=gpu:1" in launcher
    assert "#SBATCH --partition" not in launcher
    assert "#SBATCH --qos" not in launcher
    # The device still gets bound — --nv is gated on gpus, not on placement.
    assert "apptainer exec --nv --cleanenv" in out["main.nf"]
    # And the state travels with the render, so the caller's record can carry it.
    assert out["gpu_placement"]["state"] == "undeclared"


def test_a_discovered_partition_reaches_the_header_without_touching_the_config():
    """`cluster_partitions` reads a real partition/qos off the live cluster; this
    is the path that lets the caller USE one. Nothing is declared on the env."""
    from agent.skills import submit_workflow as SW
    out = SW.render_workflow_files(
        tool_name="t", command="tool ${x} > ${y}",
        inputs={"x": "/d/in"}, outputs={"y": "out.txt"},
        apptainer_sif="/w/t.sif", apptainer_module="apptainer/1.5.0",
        nextflow_module="nextflow/24.04.2",
        slurm={"time": "01:00:00", "mem": "8G", "gpus": 1,
               "partition": "l40-gpu", "qos": "gpu_access"},
        workflow_name="w", env={"name": "hpc", "type": "ssh"})
    assert "#SBATCH --partition=l40-gpu" in out["launcher.sh"]
    assert "#SBATCH --qos=gpu_access" in out["launcher.sh"]
    assert out["gpu_placement"] == {
        "gpus": 1, "state": "declared",
        "partition": "l40-gpu", "partition_source": "job",
        "qos": "gpu_access", "qos_source": "job"}


def test_gres_and_nv_agree_about_whether_this_is_a_gpu_job():
    """One decision, read twice — the shape this codebase keeps paying for. If the
    header allocates a device the exec line must bind it, and vice versa."""
    from agent.skills.workflow_render import render_workflow
    for gpus in (0, 1, 4):
        out = render_workflow(
            tool_name="t", command="tool ${x} > ${y}",
            inputs={"x": "/d/in"}, outputs={"y": "out.txt"},
            apptainer_sif="/w/t.sif", apptainer_module="apptainer/1.5.0",
            nextflow_module="nextflow/24.04.2",
            slurm={"time": "00:10:00", "mem": "1G", "cpus": 1, "gpus": gpus,
                   **({"partition": "gpu", "qos": "gpu_access"} if gpus else {})},
            workflow_name="w")
        allocates = "--gres=gpu:" in out["launcher.sh"]
        binds = "--nv" in out["main.nf"]
        assert allocates == binds == bool(gpus), (
            f"gpus={gpus}: header allocates={allocates}, exec binds={binds}")
