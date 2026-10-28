"""The I4 self-test runs each how-to command in the image its sealed step ran in, and the
record says where each command ran.

A two-image how-to sealed `verified` with every command run in the one image
`freeze_request_key` named; a command whose sealed step ran elsewhere passed there only
because the rehearsal needed nothing from the other image. Now the seal maps each command
to its step (the pipeline record's own matcher), hands the runner the step's image, and a
step image that does not resolve locally leaves the how-to `not_attempted` with the reason.
"""
from __future__ import annotations

from pathlib import Path

from agent.skills import spec_writer as SW
from agent.skills.pipeline_record import match_steps_to_templates


def _spec(commands, subs, outputs):
    return {"usage": {"description": "d", "command_template": commands,
                      "inputs": [{"name": k} for k in subs],
                      "outputs": [{"name": "o", "files": outputs}],
                      "trials": [{"name": "t1", "substitutions": subs}]},
            "conda_env": "host_env"}


class _ImageRunner:
    is_image_runner = True
    mounts: list = []

    def __init__(self):
        self.calls = []

    def run_in_env(self, env_name, command, timeout=None, watch_dir=None, **kw):
        self.calls.append(kw.get("command_index"))
        (Path(watch_dir) / f"c{kw.get('command_index')}.txt").write_text("x")
        return {"returncode": 0, "stdout": "", "stderr": "", "image": f"img{kw.get('command_index')}"}


class _HostRunner:
    is_image_runner = False

    def __init__(self):
        self.kwargs = []

    def run_in_env(self, env_name, command, timeout=None, watch_dir=None, **kw):
        self.kwargs.append(dict(kw))
        (Path(watch_dir) / "c1.txt").write_text("x")
        (Path(watch_dir) / "c2.txt").write_text("x")
        return {"returncode": 0, "stdout": "", "stderr": ""}


def test_an_image_runner_is_told_which_command_it_runs_and_the_trial_records_each_image(tmp_path):
    src = tmp_path / "in.txt"; src.write_text("x")
    spec = _spec(["tool1 {IN} > {OUTPUT_DIR}/c1.txt", "tool2 {IN} > {OUTPUT_DIR}/c2.txt"],
                 {"IN": str(src)}, ["c1.txt", "c2.txt"])
    runner = _ImageRunner()
    r = SW.self_test_usage(spec, runner)
    assert r["ok"] is True, r
    assert runner.calls == [1, 2]
    assert r["trials"][0]["images_run"] == ["img1", "img2"]


def test_a_host_runner_is_not_told_an_index_and_the_record_names_the_host_env(tmp_path):
    src = tmp_path / "in.txt"; src.write_text("x")
    spec = _spec(["tool1 {IN} > {OUTPUT_DIR}/c1.txt", "tool2 {IN} > {OUTPUT_DIR}/c2.txt"],
                 {"IN": str(src)}, ["c1.txt", "c2.txt"])
    runner = _HostRunner()
    r = SW.self_test_usage(spec, runner)
    assert r["ok"] is True, r
    assert runner.kwargs == [{}, {}]
    assert r["trials"][0]["images_run"] == ["host_env", "host_env"]


def test_the_matcher_pairs_each_command_with_the_step_that_ran_it():
    templates = ["sort {IN} > {OUTPUT_DIR}/a.txt", "uniq {OUTPUT_DIR}/a.txt > {OUTPUT_DIR}/b.txt"]
    rows = [{"substitutions": {"IN": "/d/in.txt"}}]
    steps = [{"step": 1, "command": "sort /d/in.txt > /s/x/a.txt", "returncode": 0},
             {"step": 2, "command": "uniq /s/x/a.txt > /s/x/b.txt", "returncode": 0}]
    matched, wrote_into = match_steps_to_templates(templates, rows, steps, {"OUTPUT_DIR"})
    assert [[s["step"] for _ri, s in hits] for hits in matched.values()] == [[1], [2]]
    assert wrote_into == {1: ["/s/x"], 2: ["/s/x"]}


def _two_image_draft(tmp_path):
    src = tmp_path / "in.txt"; src.write_text("x")
    d_a, d_b = "sha256:" + "aa" * 32, "sha256:" + "bb" * 32
    draft = {
        "usage": {"description": "d",
                  "command_template": ["sort {IN} > {OUTPUT_DIR}/a.txt",
                                       "uniq {OUTPUT_DIR}/a.txt > {OUTPUT_DIR}/b.txt"],
                  "inputs": [{"name": "IN"}], "outputs": [{"name": "o", "files": ["b.txt"]}],
                  "trials": [{"name": "t1", "substitutions": {"IN": str(src)}}]},
        "pipeline_steps": [
            {"step": 1, "command": f"sort {src} > /s/x/a.txt", "returncode": 0, "container_image_digest": d_a},
            {"step": 2, "command": "uniq /s/x/a.txt > /s/x/b.txt", "returncode": 0, "container_image_digest": d_b},
        ],
    }
    fr = {"image": "tools_a:1", "image_digest": d_a, "platform": "linux-64"}
    by_digest = {d_a: ("ka", {"image": "tools_a:1", "image_digest": d_a, "platform": "linux-64"}),
                 d_b: ("kb", {"image": "tools_b:1", "image_digest": d_b, "platform": "linux-aarch64"})}
    return draft, fr, by_digest, d_b


def test_the_seal_maps_a_command_to_its_own_steps_image(tmp_path, monkeypatch):
    from agent import mcp_server as _ms
    from agent.mcp_tools import workflow_tools as WT
    draft, fr, by_digest, d_b = _two_image_draft(tmp_path)
    monkeypatch.setattr(_ms._docker, "image_digest", lambda ref: ref)
    mapping, why = WT._command_images(fr, draft, by_digest)
    assert why == ""
    assert mapping == {2: (d_b, "linux/arm64")}, mapping   # command 1 ran in the pinned image


def test_a_step_image_that_does_not_resolve_here_names_the_command_and_blocks_the_self_test(tmp_path, monkeypatch):
    from agent import mcp_server as _ms
    from agent.mcp_tools import workflow_tools as WT
    draft, fr, by_digest, d_b = _two_image_draft(tmp_path)
    monkeypatch.setattr(_ms._docker, "image_digest", lambda ref: None if ref == d_b else ref)
    mapping, why = WT._command_images(fr, draft, by_digest)
    assert mapping == {} and "command 2" in why and d_b in why and "does not resolve" in why


def test_the_image_runner_runs_a_mapped_command_in_its_own_image_and_says_so(monkeypatch):
    from agent import mcp_server as _ms
    from agent.mcp_tools import workflow_tools as WT
    seen = []

    def run_in_container(image, command, mounts=None, workdir=None, platform=None, timeout=None):
        seen.append((image, platform))
        return {"returncode": 0, "stdout": "", "stderr": ""}
    monkeypatch.setattr(_ms._docker, "run_in_container", run_in_container)
    runner = WT._ImageUsageRunner("tools_a:1", "linux/amd64", [], images_by_command={2: ("tools_b:1", "linux/arm64")})
    r1 = runner.run_in_env("e", "sort", command_index=1)
    r2 = runner.run_in_env("e", "uniq", command_index=2)
    assert seen == [("tools_a:1", "linux/amd64"), ("tools_b:1", "linux/arm64")]
    assert (r1["image"], r2["image"]) == ("tools_a:1", "tools_b:1")
