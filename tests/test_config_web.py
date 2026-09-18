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
    # base_url matters: the app answers loopback Hosts only (anti-rebinding).
    return (TestClient(webmod.create_app(tmp_path / "pa.yaml", cfgmod),
                       base_url="http://127.0.0.1"),
            tmp_path / "pa.yaml")


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
    request) and the D10 lock text is present for the no-env state (revised in
    menu review: ANY compute env unlocks — a project computes on local too)."""
    c, _ = client
    html = c.get("/").text
    assert "agent_scratch_target" in html
    assert "working directory" in html        # CS58: exec's distinguishing gloss
    assert "Locked — declare a compute env first" in html
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
    for route in ("/save", "/validate", "/preview", "/shutdown"):
        assert c.post(route, json=_doc()).status_code == 403, route
    assert not path.exists()


def test_a_non_loopback_host_is_refused_even_with_the_header(cfgmod, webmod, tmp_path):
    """DNS rebinding makes an attacker's page same-origin with this server, so
    it CAN attach the header — but its Host still names the attacker's domain,
    and every route refuses it, reads included (GET /config carries real
    hostnames and usernames)."""
    from starlette.testclient import TestClient
    path = tmp_path / "pa.yaml"
    c = TestClient(webmod.create_app(path, cfgmod), base_url="http://evil.example")
    assert c.get("/config").status_code == 403
    assert c.post("/save", json=_doc(), headers=HDRS).status_code == 403
    assert not path.exists()


def test_a_malformed_body_is_a_400_never_the_empty_document(client):
    """`null` (and any non-object body) used to coerce to the EMPTY config,
    which the loader accepts — so a machine caller's garbage was written over
    the file with a green verdict. Garbage must be refused, not rounded up."""
    c, path = client
    assert c.post("/save", json=_doc(), headers=HDRS).json()["ok"] is True
    before = path.read_text()
    for body in ("null", "[1, 2]", '"a string"', "{not json"):
        r = c.post("/save", content=body,
                   headers={**HDRS, "Content-Type": "application/json"})
        assert r.status_code == 400, (body, r.status_code)
    assert path.read_text() == before, "a malformed body reached the file"


def test_the_pages_close_buttons_stop_the_serving_process(cfgmod, webmod, tmp_path):
    """SAVE & CLOSE / CANCEL CHANGES & CLOSE end the terminal process from the
    page — POST /shutdown flips the server's exit flag via the on_close hook, so
    the user never has to Ctrl-C. Header-guarded like every other POST: stopping
    someone's menu is a deliberate act."""
    from starlette.testclient import TestClient
    calls = []
    c = TestClient(webmod.create_app(tmp_path / "pa.yaml", cfgmod,
                                     on_close=lambda: calls.append(1)),
                   base_url="http://127.0.0.1")
    assert c.post("/shutdown").status_code == 403 and calls == []
    r = c.post("/shutdown", headers=HDRS).json()
    assert r["ok"] is True and r["closing"] is True and calls == [1]


def test_the_footer_and_env_buttons_say_what_they_do(webmod):
    """The user-reviewed control surface: three explicit footer actions (revert
    one change at a time; save-and-exit; discard-and-exit) and add-env buttons
    that name what they add. The 'declared' checkbox is gone — every zone is
    always on screen, and a blank optional path IS 'not declared'."""
    for marker in ("revert<br>last<br>change", "save<br>&amp;<br>close",
                   "cancel<br>changes<br>&amp; close",
                   "+ add new local env (this machine)",
                   "+ add new ssh env (cluster/external compute resource)"):
        assert marker in webmod.PAGE, marker
    assert "toggleZone" not in webmod.PAGE


def test_the_intro_legends_and_permission_resets_render_off_the_spec(webmod):
    """Menu review: the envs intro carries TWO legends (directories, then
    permissions) and the projects intro reuses the same permissions legend —
    all rendered from META, one function for the permission rows. Every
    permissions field also offers a 'defaults ↺' reset to the spec's
    recommended set."""
    assert webmod.PAGE.count("permLegendRows()") >= 3   # def-site + envs + projects
    assert "the compute env directories" in webmod.PAGE
    assert "resetPerms(" in webmod.PAGE and "resetDirPerms(" in webmod.PAGE
    assert webmod.PAGE.count("defaults ↺") == 2         # zone + directory chip rows


def test_required_and_optional_are_badged_consistently(webmod):
    """User-reviewed convention: every name/path/description field carries the
    same badge style — `required` (warn color) or `optional` (dim) — matching
    the zone headers, so the whole form reads with one visual grammar."""
    for marker in ('project name <span class="req">required</span>',
                   'env name <span class="req">required</span>',
                   'absolute path <span class="req">required</span>'):
        assert marker in webmod.PAGE, marker
    assert webmod.PAGE.count('<span class="opt">optional</span>') >= 2  # both descriptions


def test_every_write_posts_the_pruned_document_and_the_verdict_names_the_field(webmod):
    """Two page-side seams. (1) validate/save/preview all post outDoc() — the
    pruning that turns a blank optional-zone path into 'undeclared' — so the
    preview is byte-identical to what a save writes. (2) The loader speaks yaml
    keys (`globus.local_endpoint_name`); the verdict pipes its message through
    the ONE label map the inputs render from, so an error names the field the
    user sees ('local display name'), not just the key."""
    assert webmod.PAGE.count("body: JSON.stringify(outDoc())") == 3
    assert "body: JSON.stringify(doc)" not in webmod.PAGE
    assert "GLOBUS_LABELS.local_endpoint_name" in webmod.PAGE   # inputs read the map
    assert "nameTheField(lastVerdict.message)" in webmod.PAGE   # the verdict glosses with it


def test_no_user_string_is_ever_interpolated_into_a_js_string_literal(webmod):
    """esc() cannot protect the JS-string-in-attribute context: the browser
    HTML-decodes attribute values BEFORE compiling the handler, so an env name
    like `x',alert(1),'` executes. Handler arguments must be indices. This pins
    the pattern that made it exploitable."""
    assert "'${esc(" not in webmod.PAGE


def test_the_save_that_drops_unmanaged_keys_says_so(cfgmod, webmod, tmp_path):
    """Config.reload announces top-level keys a save will DROP; the terminal
    reprints that every loop. The web save is the moment the drop happens, so
    its response must carry the warning too."""
    from starlette.testclient import TestClient
    path = tmp_path / "pa.yaml"
    path.write_text(yaml.safe_dump({**_doc(), "future_section": {"a": 1}}))
    c = TestClient(webmod.create_app(path, cfgmod), base_url="http://127.0.0.1")
    r = c.post("/save", json=_doc(), headers=HDRS).json()
    assert r["ok"] is True
    assert any("future_section" in m["text"] and "DROP" in m["text"]
               for m in r["messages"])


def test_validate_answers_instead_of_500ing_on_a_non_string_path(client):
    """A hand-edited YAML can carry `path: 123`; the notes pass must skip it,
    not take the whole verdict down with a TypeError."""
    c, _ = client
    doc = {"compute_envs": [{"name": "laptop", "type": "local",
                             "agent_scratch_target": {"path": 123,
                                                      "permissions": ["upload"]}}],
           "projects": []}
    r = c.post("/validate", json=doc, headers=HDRS)
    assert r.status_code == 200
    assert r.json()["state"] == "invalid"     # the loader refuses it, with words


def test_preview_returns_the_exact_bytes_a_save_would_write(client, cfgmod):
    c, path = client
    doc = _doc()
    text = c.post("/preview", json=doc, headers=HDRS).json()["yaml"]
    assert text.startswith(cfgmod.HEADER)
    assert c.post("/save", json=doc, headers=HDRS).json()["ok"] is True
    assert path.read_text() == text


# --- reading a real file -----------------------------------------------------

def test_the_page_says_where_its_file_path_came_from(cfgmod, client):
    """'Is this the right file?' must be answerable from the page: the
    bootstrap carries path_source — the workspace + which of the three
    resolution answers chose it, or a plain statement that --file overrode
    the agent's default. The fixture path IS an override, so that is what
    it must say (not a workspace story for a path the agent won't read)."""
    c, _ = client
    j = c.get("/config").json()
    assert "--file override" in j["path_source"]

    from agent.skills.compute_access import default_access_path
    src = cfgmod.path_source(default_access_path())
    assert "workspace" in src and "the agent" in src


def test_config_get_reports_state_and_content_of_the_disk_file(cfgmod, webmod, tmp_path):
    from starlette.testclient import TestClient
    path = tmp_path / "pa.yaml"
    path.write_text(yaml.safe_dump(_doc()))
    c = TestClient(webmod.create_app(path, cfgmod), base_url="http://127.0.0.1")
    j = c.get("/config").json()
    assert j["exists"] is True and j["state"] == "valid"
    assert j["config"]["compute_envs"][0]["name"] == "cluster"

    missing = TestClient(webmod.create_app(tmp_path / "gone.yaml", cfgmod),
                         base_url="http://127.0.0.1")
    assert missing.get("/config").json()["state"] == "absent"
