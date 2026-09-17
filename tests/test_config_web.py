"""The browser menu is the SECOND renderer of one field spec, and it cannot
write a file the agent refuses to load.

`scripts/config_web.py` renders the same configuration menu configure.py drives
in the terminal, and both renderers read the ONE declared field spec (ZONES,
PERMISSION_GLOSSES, TRANSFER_TYPES, PROJECT_NAME_RE). That property — not the
page's looks — is what these tests pin: the meta payload the page renders from
IS the spec, every save goes through the same `Config` (validate → .bak →
write), and the write surface demands a deliberate header so a drive-by page
from another origin cannot rewrite the user's permission grants.

The page's JS is not executed here (no browser); what the page WOULD render is
pinned at the data seam it renders FROM, which is where a drift would start.
"""
from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
import yaml

from agent.skills import compute_access

ROOT = Path(__file__).resolve().parent.parent

HDRS = {"X-Bioinf-Config": "1"}


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def cfgmod():
    return _load("_configure_for_web", "configure.py")


@pytest.fixture(scope="module")
def webmod():
    return _load("_config_web", "config_web.py")


@pytest.fixture()
def client(cfgmod, webmod, tmp_path):
    from starlette.testclient import TestClient
    return TestClient(webmod.create_app(tmp_path / "pa.yaml", cfgmod)), tmp_path / "pa.yaml"


def _env(name="cluster"):
    return {
        "name": name, "type": "ssh", "host": "h.example.edu", "user": "a",
        "agent_scratch_target": {"path": "/scratch/a/S/",
                                 "permissions": ["upload", "download", "exec"]},
        "agent_common_data_target": {"path": "/scratch/a/G/",
                                     "permissions": ["upload", "download", "exec"]},
    }


def _doc(**over):
    d = {"compute_envs": [_env()], "projects": []}
    d.update(over)
    return d


# --- one spec, two renderers -------------------------------------------------

def test_the_meta_payload_is_the_field_spec_not_a_copy(cfgmod, client):
    """Every zone, gloss and enum the page renders comes off configure.py's own
    constants. A mismatch here means the page grew its own schema — the exact
    drift the localhost-server design (vs a static file:// page) exists to
    refuse."""
    c, _ = client
    meta = c.get("/config").json()["meta"]
    assert [z["key"] for z in meta["zones"]] == [k for k, _, _, _ in cfgmod.ZONES]
    assert [z["required"] for z in meta["zones"]] == [r for *_, r in cfgmod.ZONES]
    assert meta["permission_glosses"] == cfgmod.PERMISSION_GLOSSES
    assert meta["transfer_types"] == cfgmod.TRANSFER_TYPES
    assert meta["project_name_pattern"] == compute_access.PROJECT_NAME_RE.pattern
    # ssh templates carry the {user} slot the page substitutes; local defaults
    # are real resolved paths on this machine.
    assert all("{user}" in p for p in meta["ssh_zone_templates"].values())
    assert all(p.startswith("/") for p in meta["local_zone_defaults"].values())


def test_the_page_carries_the_spec_and_the_lock(client):
    """The page bootstrap embeds the spec (so the first paint needs no second
    request) and the D10 lock text is present for the no-remote-env state."""
    c, _ = client
    html = c.get("/").text
    assert "agent_scratch_target" in html
    assert "working directory" in html        # CS58: exec's distinguishing gloss
    assert "Locked — needs a remote env" in html
    assert "cdn" not in html.lower() and "https://" not in html.split("</head>")[0], \
        "the page must be self-contained — no external assets"


# --- verdicts ----------------------------------------------------------------

def test_validate_answers_with_the_loaders_own_verdict(client):
    c, _ = client
    ok = c.post("/validate", json=_doc(), headers=HDRS).json()
    assert ok["state"] == "valid" and ok["message"] == ""

    bad = _doc()
    bad["projects"] = [{"name": "p", "compute_envs": ["ghost"], "directories": []}]
    r = c.post("/validate", json=bad, headers=HDRS).json()
    assert r["state"] == "invalid" and "ghost" in r["message"]


def test_validate_notes_a_missing_local_path_but_does_not_refuse(client, tmp_path):
    """CS60 on the web surface: free to check, stated as a note, never a
    refusal and never a mkdir."""
    c, _ = client
    doc = {"compute_envs": [{
        "name": "laptop", "type": "local",
        "agent_scratch_target": {"path": str(tmp_path / "gone"),
                                 "permissions": ["upload", "download", "exec"]}}],
        "projects": []}
    r = c.post("/validate", json=doc, headers=HDRS).json()
    assert r["state"] == "valid"
    assert any("does not exist yet" in n for n in r["notes"])

    doc["compute_envs"][0]["agent_scratch_target"]["path"] = str(tmp_path)
    r = c.post("/validate", json=doc, headers=HDRS).json()
    assert r["notes"] == []


# --- the write surface -------------------------------------------------------

def test_save_goes_through_the_same_config_save_path(client):
    """Valid → written and loadable by the agent's own loader; a second save
    leaves the previous version as .bak — the exact contract the terminal menu
    already keeps, because it IS the terminal menu's save."""
    c, path = client
    r = c.post("/save", json=_doc(), headers=HDRS).json()
    assert r["ok"] is True
    assert compute_access.load_access(path)

    doc2 = _doc()
    doc2["compute_envs"][0]["email"] = "x@example.org"
    assert c.post("/save", json=doc2, headers=HDRS).json()["ok"] is True
    assert Path(str(path) + ".bak").exists()
    assert "email" not in Path(str(path) + ".bak").read_text()


def test_save_refuses_an_invalid_document_and_writes_nothing(client):
    c, path = client
    bad = _doc()
    bad["projects"] = [{"name": "my project!", "compute_envs": ["cluster"],
                        "directories": []}]
    r = c.post("/save", json=bad, headers=HDRS).json()
    assert r["ok"] is False
    assert any("NOT saved" in m["text"] for m in r["messages"])
    assert not path.exists(), "an invalid configuration reached disk"


def test_posts_without_the_deliberate_write_header_are_refused(client):
    """A cross-origin browser page cannot attach a custom header without a CORS
    preflight this app never answers — so the 403 is what stands between a
    drive-by page and the user's permission grants."""
    c, path = client
    for route in ("/save", "/validate", "/preview"):
        assert c.post(route, json=_doc()).status_code == 403, route
    assert not path.exists()


def test_preview_returns_the_exact_bytes_a_save_would_write(client, cfgmod):
    c, path = client
    doc = _doc()
    text = c.post("/preview", json=doc, headers=HDRS).json()["yaml"]
    assert text.startswith(cfgmod.HEADER)
    assert c.post("/save", json=doc, headers=HDRS).json()["ok"] is True
    assert path.read_text() == text


# --- reading a real file -----------------------------------------------------

def test_config_get_reports_state_and_content_of_the_disk_file(cfgmod, webmod, tmp_path):
    from starlette.testclient import TestClient
    path = tmp_path / "pa.yaml"
    path.write_text(yaml.safe_dump(_doc()))
    c = TestClient(webmod.create_app(path, cfgmod))
    j = c.get("/config").json()
    assert j["exists"] is True and j["state"] == "valid"
    assert j["config"]["compute_envs"][0]["name"] == "cluster"

    missing = TestClient(webmod.create_app(tmp_path / "gone.yaml", cfgmod))
    assert missing.get("/config").json()["state"] == "absent"
