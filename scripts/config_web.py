#!/usr/bin/env python3
"""config_web.py — the browser rendering of the configuration menu.

`./scripts/config.sh --web` serves this on 127.0.0.1 (an ephemeral port unless
--port says otherwise) and opens the browser; Ctrl-C stops it. It is the SECOND
renderer of the one field spec `scripts/configure.py` declares (ZONES,
PERMISSION_GLOSSES, TRANSFER_TYPES, ENV_KEYS, PROJECT_NAME_RE) — this module
holds no schema knowledge of its own, which is the property that keeps the two
menus from drifting. Validation and saving go through the SAME `Config` object
the terminal menu uses, so every save is checked by the agent's own loader and
the previous file is kept as `.bak`.

The page is self-contained (inline CSS/JS, no CDN) and the server speaks plain
JSON, which makes it a machine surface too:

    GET  /config    → {path, exists, state, message, config, meta}
    POST /validate  → {state, message, notes[]}     (verdict only; writes nothing)
    POST /save      → {ok, messages[], state}       (validate → .bak → write)
    POST /preview   → {yaml}                        (the exact bytes a save writes)
    GET  /globus/local-id and /globus/search?q=     (the endpoint picker's data)

Two rings guard the write surface. POST routes require the header
`X-Bioinf-Config: 1` — a browser page from another origin cannot attach a
custom header without a CORS preflight (which this app never answers). And
every request must arrive with a loopback Host (`127.0.0.1`/`localhost`),
which closes the DNS-rebinding hole the header check alone leaves open: a page
whose hostname is rebound to 127.0.0.1 becomes same-origin with this server,
but its Host header still names the attacker's domain. An agent authoring this
file non-interactively does not need this server at all — write the YAML
directly (schema: agent/skills/projects_access.yaml.example) and check it with
`configure.py --validate`.

Deliberately NOT a static file:// page: that would need its own validator in
JS, a second copy of the schema, which is the drift this repo's whole immune
system exists to refuse.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# app factory — `cfgmod` is the live scripts/configure.py module, passed in by
# the caller so there is exactly one loaded copy of the field spec (importing
# it by path here would execute a second copy with a second Config class).
# ---------------------------------------------------------------------------

def _meta(cfgmod) -> dict[str, Any]:
    """The field spec, serialized for the page. Every entry is READ off
    configure.py / compute_access — nothing here is a second spelling."""
    from agent.skills.compute_access import PROJECT_NAME_RE
    return {
        "zones": [{"key": k, "label": cfgmod.ZONE_LABELS[k], "gloss": gloss,
                   "default_perms": perms, "required": required}
                  for k, gloss, perms, required in cfgmod.ZONES],
        "permission_order": [t for t in cfgmod.PERMISSION_ORDER if t != "none"],
        "permission_glosses": cfgmod.PERMISSION_GLOSSES,
        "transfer_types": cfgmod.TRANSFER_TYPES,
        "job_managers": list(cfgmod.VALID_JOB_MANAGERS),
        "project_name_pattern": PROJECT_NAME_RE.pattern,
        # Zone-path defaults: ssh as a template over the typed user, local as
        # real resolved paths under this machine's workspace.
        "ssh_zone_templates": cfgmod.ssh_defaults("{user}"),
        "local_zone_defaults": cfgmod.local_defaults(),
        "module_placeholders": cfgmod.MODULE_PLACEHOLDERS,
        "dir_default_perms": cfgmod.DIR_DEFAULT_PERMS,
        "projects_locked_note": cfgmod.PROJECTS_LOCKED_NOTE,
        "example_path": "agent/skills/projects_access.yaml.example",
    }


async def _read_doc(request):
    """The posted document, or an error response. A malformed body must never
    round up to the EMPTY document — the empty document is loader-valid, so a
    machine caller's garbage would be written over the file with a green
    verdict."""
    from starlette.responses import JSONResponse
    try:
        body = await request.json()
    except Exception:
        return None, JSONResponse({"error": "body must be JSON"}, status_code=400)
    if not isinstance(body, dict):
        return None, JSONResponse(
            {"error": "body must be a JSON object with compute_envs/projects"},
            status_code=400)
    return {"compute_envs": body.get("compute_envs") or [],
            "projects": body.get("projects") or []}, None


def _existence_notes(cfgmod, doc: dict) -> list[str]:
    """CS60, web edition: on a `type: local` env every declared path is on THIS
    filesystem, so checking costs nothing. Notes, never refusals."""
    notes: list[str] = []
    local_envs = {e.get("name") for e in doc["compute_envs"]
                  if isinstance(e, dict) and e.get("type") == "local"}
    for env in doc["compute_envs"]:
        if not isinstance(env, dict) or env.get("type") != "local":
            continue
        for key, _, _, _ in cfgmod.ZONES:
            p = (env.get(key) or {}).get("path") if isinstance(env.get(key), dict) else None
            if isinstance(p, str) and p and not Path(p).is_dir():
                notes.append(f"{env.get('name')}: {key} path {p} does not exist yet "
                             f"(accepted — nothing creates it for you)")
    for proj in doc["projects"]:
        if not isinstance(proj, dict):
            continue
        for d in proj.get("directories") or []:
            if (isinstance(d, dict) and d.get("env") in local_envs
                    and isinstance(d.get("path"), str) and d["path"]
                    and not Path(d["path"]).is_dir()):
                notes.append(f"{proj.get('name')}: directory {d['path']} does not "
                             f"exist yet (accepted — nothing creates it for you)")
    return notes


def create_app(cfg_path: Path, cfgmod):
    from starlette.applications import Starlette
    from starlette.middleware import Middleware
    from starlette.middleware.base import BaseHTTPMiddleware
    from starlette.responses import HTMLResponse, JSONResponse
    from starlette.routing import Route

    class LoopbackHostOnly(BaseHTTPMiddleware):
        """Refuse any request whose Host is not loopback. A DNS-rebound page is
        same-origin with this server and can attach any header it likes — but
        its Host still names the attacker's domain, and this is where that
        lie is caught."""
        async def dispatch(self, request, call_next):
            host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]")
            if host not in ("127.0.0.1", "localhost", "::1"):
                return JSONResponse(
                    {"error": f"this menu answers loopback requests only "
                              f"(got Host: {host or '?'})"},
                    status_code=403)
            return await call_next(request)

    def _guarded(request) -> JSONResponse | None:
        if request.headers.get("x-bioinf-config") != "1":
            return JSONResponse(
                {"error": "missing X-Bioinf-Config: 1 header — POSTs to this menu "
                          "are deliberate writes, not ambient ones"},
                status_code=403)
        return None

    async def page(request):
        cfg = cfgmod.Config(cfg_path, notify=lambda k, t: None)
        state, message = cfg.state()
        bootstrap = {
            "path": str(cfg_path),
            "exists": cfg_path.exists(),
            "state": state, "message": message,
            "load_error": cfg.load_error,
            "config": cfg.data,
            "meta": _meta(cfgmod),
        }
        # "</" would end the <script> block from inside a JSON string; "<!--"
        # can push the HTML parser into escaped-script-data state. Both become
        # JS-string escapes that decode back to the same characters.
        blob = (json.dumps(bootstrap)
                .replace("</", "<\\/")
                .replace("<!--", "<\\u0021--"))
        return HTMLResponse(PAGE.replace("__BOOTSTRAP__", blob))

    async def config_get(request):
        cfg = cfgmod.Config(cfg_path, notify=lambda k, t: None)
        state, message = cfg.state()
        return JSONResponse({"path": str(cfg_path), "exists": cfg_path.exists(),
                             "state": state, "message": message,
                             "load_error": cfg.load_error, "config": cfg.data,
                             "meta": _meta(cfgmod)})

    async def validate(request):
        if (deny := _guarded(request)) is not None:
            return deny
        doc, err_resp = await _read_doc(request)
        if err_resp is not None:
            return err_resp
        cfg = cfgmod.Config(cfg_path, notify=lambda k, t: None)
        cfg.data = doc
        err = cfg.validate()
        return JSONResponse({"state": "valid" if not err else "invalid",
                             "message": err,
                             "notes": _existence_notes(cfgmod, doc)})

    async def save(request):
        if (deny := _guarded(request)) is not None:
            return deny
        doc, err_resp = await _read_doc(request)
        if err_resp is not None:
            return err_resp
        messages: list[dict[str, str]] = []
        cfg = cfgmod.Config(cfg_path,
                            notify=lambda k, t: messages.append({"kind": k, "text": t}))
        if cfg.load_error:
            # The terminal reprints this every loop; the save that actually
            # drops the unmanaged keys must say so too, not just the page load.
            messages.append({"kind": "note", "text": cfg.load_error})
        cfg.data = doc
        cfg.dirty = True
        ok = cfg.save()
        return JSONResponse({"ok": ok, "messages": messages,
                             "state": "valid" if ok else "invalid"})

    async def preview(request):
        if (deny := _guarded(request)) is not None:
            return deny
        doc, err_resp = await _read_doc(request)
        if err_resp is not None:
            return err_resp
        return JSONResponse({"yaml": cfgmod.HEADER + "\n" + cfgmod.dump(doc)})

    async def globus_local_id(request):
        return JSONResponse({"id": cfgmod._globus_local_id()})

    async def globus_search(request):
        q = request.query_params.get("q", "").strip()
        if not q:
            return JSONResponse({"rows": [], "why": "empty search"})
        rows, why = cfgmod._globus_search(q)
        return JSONResponse({"rows": [{"id": r.get("id"),
                                       "display_name": r.get("display_name"),
                                       "owner": r.get("owner_string")}
                                      for r in rows],
                             "why": why})

    return Starlette(routes=[
        Route("/", page),
        Route("/config", config_get),
        Route("/validate", validate, methods=["POST"]),
        Route("/save", save, methods=["POST"]),
        Route("/preview", preview, methods=["POST"]),
        Route("/globus/local-id", globus_local_id),
        Route("/globus/search", globus_search),
    ], middleware=[Middleware(LoopbackHostOnly)])


def serve(cfg_path: Path, cfgmod, port: int = 0) -> int:
    import socket
    import webbrowser

    import uvicorn

    if not port:
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
    url = f"http://127.0.0.1:{port}/"
    print(f"  config menu: {url}")
    print("  nothing is written until you SAVE in the page; Ctrl-C here stops it")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    uvicorn.run(create_app(cfg_path, cfgmod), host="127.0.0.1", port=port,
                log_level="warning")
    return 0


# ---------------------------------------------------------------------------
# the page — one file, no CDN, dark game-settings-menu rendering
# ---------------------------------------------------------------------------

PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>bioinf-agent — configuration</title>
<style>
:root {
  --bg: #0a0e12; --bg2: #0e141a; --panel: #101820cc; --line: #1e3038;
  --tx: #cfd8dc; --dim: #6b7d85; --acc: #57d7c3; --acc2: #2b6f66;
  --ok: #6fdc8c; --bad: #ff6b6b; --warn: #f0c674;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html, body { height: 100%; }
body {
  background:
    radial-gradient(1200px 700px at 70% -10%, #12303377 0%, transparent 60%),
    radial-gradient(900px 600px at -10% 110%, #0f262b66 0%, transparent 55%),
    var(--bg);
  color: var(--tx);
  font: 15px/1.5 system-ui, -apple-system, "Segoe UI", sans-serif;
  display: flex; flex-direction: column;
}
code, .mono, input, select, pre { font-family: ui-monospace, "SF Mono", Menlo, monospace; }

header {
  padding: 18px 28px 14px; border-bottom: 1px solid var(--line);
  display: flex; align-items: baseline; gap: 18px; flex-wrap: wrap;
}
header h1 { font-size: 15px; letter-spacing: .35em; font-weight: 600;
            text-transform: uppercase; color: var(--acc); }
header .path { color: var(--dim); font-size: 12px; }
.pill { display: inline-block; padding: 1px 10px; border-radius: 2px;
        font-size: 11px; letter-spacing: .12em; text-transform: uppercase;
        border: 1px solid var(--line); }
.pill.valid   { color: var(--ok);  border-color: var(--ok); }
.pill.invalid { color: var(--bad); border-color: var(--bad); }
.pill.absent  { color: var(--warn); border-color: var(--warn); }
.pill.dirty   { color: var(--warn); border-color: var(--warn); }

.layout { flex: 1; display: flex; min-height: 0; }
nav {
  width: 230px; padding: 26px 0; border-right: 1px solid var(--line);
  display: flex; flex-direction: column; gap: 2px; flex-shrink: 0;
}
nav .item {
  padding: 11px 26px; cursor: pointer; color: var(--dim);
  letter-spacing: .22em; text-transform: uppercase; font-size: 12px;
  border-left: 2px solid transparent; user-select: none;
}
nav .item:hover { color: var(--tx); }
nav .item.active {
  color: var(--acc); border-left-color: var(--acc);
  background: linear-gradient(90deg, #57d7c31a, transparent 70%);
}
nav .item.locked { color: #44545c; }
nav .item.locked::after { content: " ⬦"; }

main { flex: 1; overflow-y: auto; padding: 26px 34px 120px; }
h2 { font-size: 13px; letter-spacing: .3em; text-transform: uppercase;
     color: var(--acc); margin: 4px 0 14px; }
.hint { color: var(--dim); font-size: 13px; margin-bottom: 18px; max-width: 72ch; }

.card {
  background: var(--panel); border: 1px solid var(--line); border-radius: 3px;
  padding: 16px 20px; margin-bottom: 16px;
  box-shadow: 0 0 0 1px #00000055, 0 6px 24px #00000066;
}
.card h3 { font-size: 14px; color: var(--tx); display: flex; align-items: center;
           gap: 10px; margin-bottom: 8px; }
.tag { font-size: 10px; letter-spacing: .15em; text-transform: uppercase;
       color: var(--acc); border: 1px solid var(--acc2); padding: 0 6px;
       border-radius: 2px; }
.row { display: flex; gap: 14px; flex-wrap: wrap; margin: 8px 0; }
.field { display: flex; flex-direction: column; gap: 3px; }
.field label { font-size: 11px; letter-spacing: .12em; text-transform: uppercase;
               color: var(--dim); }
input[type=text] {
  background: var(--bg2); color: var(--tx); border: 1px solid var(--line);
  padding: 6px 9px; border-radius: 2px; font-size: 13px; min-width: 260px;
}
input[type=text]:focus { outline: none; border-color: var(--acc2); }
input.badname { border-color: var(--bad); }
select {
  background: var(--bg2); color: var(--tx); border: 1px solid var(--line);
  padding: 6px 9px; border-radius: 2px; font-size: 13px;
}
.zone { border-top: 1px dashed var(--line); padding: 10px 0 4px; margin-top: 10px; }
.zone .zhead { display: flex; align-items: baseline; gap: 10px; }
.zone .zname { font-size: 12px; letter-spacing: .1em; color: var(--tx);
               text-transform: uppercase; }
.zone .zgloss { color: var(--dim); font-size: 12px; }
.req { color: var(--warn); font-size: 10px; letter-spacing: .15em; }
.chips { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 4px; }
.chip {
  border: 1px solid var(--line); color: var(--dim); font-size: 11px;
  padding: 2px 10px; border-radius: 999px; cursor: pointer; user-select: none;
}
.chip.on { border-color: var(--acc); color: #06211d; background: var(--acc); }
.chip:hover { border-color: var(--acc2); }
button {
  background: transparent; border: 1px solid var(--acc2); color: var(--acc);
  padding: 7px 18px; font-size: 12px; letter-spacing: .18em;
  text-transform: uppercase; cursor: pointer; border-radius: 2px;
}
button:hover { background: #57d7c31a; border-color: var(--acc); }
button.subtle { border-color: var(--line); color: var(--dim); }
button.subtle:hover { color: var(--tx); }
button.danger { border-color: #6b2f2f; color: var(--bad); }
button.danger:hover { background: #ff6b6b14; }
.note { color: var(--warn); font-size: 12px; margin-top: 4px; }
.errline { color: var(--bad); font-size: 13px; white-space: pre-wrap; }
.okline { color: var(--ok); font-size: 13px; }
.lockbox {
  border: 1px dashed var(--line); border-radius: 3px; padding: 34px;
  color: var(--dim); max-width: 66ch;
}
.lockbox .glyph { font-size: 26px; color: #44545c; margin-bottom: 10px; }
pre.filedump {
  background: var(--bg2); border: 1px solid var(--line); border-radius: 3px;
  padding: 16px; font-size: 12px; overflow-x: auto; color: #a9c1c9;
}
table.legend { border-collapse: collapse; font-size: 13px; }
table.legend td { padding: 5px 16px 5px 0; vertical-align: top; }
table.legend td:first-child { color: var(--acc); white-space: nowrap; }

footer {
  position: fixed; left: 0; right: 0; bottom: 0;
  background: #0a0e12ee; border-top: 1px solid var(--line);
  padding: 12px 34px; display: flex; align-items: center; gap: 18px;
  backdrop-filter: blur(4px);
}
footer .verdict { flex: 1; font-size: 13px; min-width: 0; }
.searchrows { margin-top: 6px; }
.searchrows .srow { padding: 4px 8px; border: 1px solid var(--line);
  border-radius: 2px; margin-bottom: 4px; cursor: pointer; font-size: 12px; }
.searchrows .srow:hover { border-color: var(--acc2); }
.searchrows .sid { color: var(--dim); }
</style>
</head>
<body>
<header>
  <h1>bioinf-agent // configuration</h1>
  <span class="pill" id="statepill">…</span>
  <span class="pill dirty" id="dirtypill" style="display:none">unsaved</span>
  <span class="path mono" id="cfgpath"></span>
</header>
<div class="layout">
  <nav id="nav"></nav>
  <main id="main"></main>
</div>
<footer>
  <div class="verdict" id="verdict"></div>
  <button class="subtle" onclick="revert()">Revert</button>
  <button onclick="saveCfg()">Save</button>
</footer>
<script>
const BOOT = __BOOTSTRAP__;
const META = BOOT.meta;
let doc = BOOT.config;
let dirty = false;
let section = 'envs';
let lastVerdict = {state: BOOT.state, message: BOOT.message, notes: []};

const HDRS = {'Content-Type': 'application/json', 'X-Bioinf-Config': '1'};
const esc = s => String(s ?? '').replace(/[&<>"']/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));

function projectsUnlocked() {
  return (doc.projects || []).length > 0 ||
         (doc.compute_envs || []).some(e => e.type === 'ssh');
}

// ---------- nav + shell ----------
function renderNav() {
  const unlocked = projectsUnlocked();
  const items = [
    ['envs', 'Compute Envs'],
    ['projects', 'Projects' + (unlocked ? '' : '')],
    ['file', 'File'],
    ['reference', 'Reference'],
  ];
  document.getElementById('nav').innerHTML = items.map(([k, label]) => {
    const locked = (k === 'projects' && !unlocked) ? ' locked' : '';
    const active = (k === section) ? ' active' : '';
    return `<div class="item${active}${locked}" onclick="go('${k}')">${label}</div>`;
  }).join('');
}
function go(k) { section = k; render(); }

function markDirty() {
  dirty = true;
  document.getElementById('dirtypill').style.display = '';
  scheduleValidate();
}

function render() {
  renderNav();
  document.getElementById('cfgpath').textContent = BOOT.path;
  const m = document.getElementById('main');
  if (section === 'envs') m.innerHTML = renderEnvs();
  else if (section === 'projects') m.innerHTML = renderProjects();
  else if (section === 'file') { m.innerHTML = renderFile(); loadPreview(); }
  else m.innerHTML = renderReference();
  renderVerdict();
}

// ---------- compute envs ----------
function renderZone(ei, z) {
  const env = doc.compute_envs[ei];
  const blk = env[z.key];
  const declared = !!blk;
  const zoneToggle = z.required ? `<span class="req">required</span>` :
    `<label style="font-size:11px;color:var(--dim);cursor:pointer">
       <input type="checkbox" ${declared ? 'checked' : ''}
              onchange="toggleZone(${ei}, '${z.key}', this.checked)"> declared</label>`;
  let body = '';
  if (declared) {
    const perms = blk.permissions || [];
    body = `
      <div class="row">
        <div class="field"><label>path</label>
          <input type="text" value="${esc(blk.path || '')}" size="46"
                 onchange="setZone(${ei}, '${z.key}', 'path', this.value)"></div>
        <div class="field"><label>description</label>
          <input type="text" value="${esc(blk.description || '')}" size="30"
                 onchange="setZone(${ei}, '${z.key}', 'description', this.value)"></div>
      </div>
      <div class="chips">` +
      META.permission_order.map(t =>
        `<span class="chip ${perms.includes(t) ? 'on' : ''}"
               title="${esc(META.permission_glosses[t])}"
               onclick="togglePerm(${ei}, '${z.key}', '${t}')">${t}</span>`).join('') +
      `</div>`;
  }
  return `<div class="zone">
    <div class="zhead"><span class="zname">${z.label}</span> ${zoneToggle}
      <span class="zgloss">${esc(z.gloss)}</span></div>${body}</div>`;
}

function renderEnv(env, ei) {
  const isSSH = env.type === 'ssh';
  const sshFields = !isSSH ? '' : `
    <div class="row">
      <div class="field"><label>ssh host / alias</label>
        <input type="text" value="${esc(env.host || '')}"
               onchange="setEnv(${ei}, 'host', this.value)"></div>
      <div class="field"><label>ssh user (blank = from ssh_config)</label>
        <input type="text" value="${esc(env.user || '')}"
               onchange="setEnvUser(${ei}, this.value)"></div>
      <div class="field"><label>job manager</label>
        <select onchange="setEnv(${ei}, 'job_manager', this.value)">
          ${META.job_managers.map(j => `<option ${env.job_manager === j ? 'selected' : ''}>${j}</option>`).join('')}
          <option value="" ${!env.job_manager ? 'selected' : ''}>(none)</option>
        </select></div>
      <div class="field"><label>notification email</label>
        <input type="text" value="${esc(env.email || '')}"
               onchange="setEnv(${ei}, 'email', this.value)"></div>
    </div>
    <div class="row">
      <div class="field"><label>apptainer module (Lmod)</label>
        <input type="text" value="${esc(env.apptainer_module || '')}"
               placeholder="${esc(META.module_placeholders.apptainer_module)}"
               onchange="setEnv(${ei}, 'apptainer_module', this.value)"></div>
      <div class="field"><label>nextflow module (Lmod)</label>
        <input type="text" value="${esc(env.nextflow_module || '')}"
               placeholder="${esc(META.module_placeholders.nextflow_module)}"
               onchange="setEnv(${ei}, 'nextflow_module', this.value)"></div>
    </div>`;
  const zones = META.zones.map(z => renderZone(ei, z)).join('');
  return `<div class="card">
    <h3><span class="tag">${env.type}</span>
      <input type="text" value="${esc(env.name || '')}" size="18"
             onchange="renameEnv(${ei}, this.value)">
      <span style="flex:1"></span>
      <button class="danger" onclick="removeEnv(${ei})">remove</button></h3>
    ${sshFields}${zones}${isSSH ? renderTransfer(ei) : ''}${isSSH ? renderSlurm(ei) : ''}
  </div>`;
}

function renderTransfer(ei) {
  const env = doc.compute_envs[ei];
  const dt = env.data_transfer || null;
  const t = dt ? dt.type : '';
  const g = (dt && dt.globus) || {};
  const globusFields = t !== 'globus' ? '' : `
    <div class="row">
      <div class="field"><label>local endpoint UUID (this machine)</label>
        <input type="text" id="lep${ei}" value="${esc(g.local_endpoint_id || '')}" size="40"
               onchange="setGlobus(${ei}, 'local_endpoint_id', this.value)"></div>
      <div class="field"><label>local display name</label>
        <input type="text" value="${esc(g.local_endpoint_name || '')}"
               onchange="setGlobus(${ei}, 'local_endpoint_name', this.value)"></div>
      <div class="field" style="justify-content:flex-end">
        <button class="subtle" onclick="detectLocal(${ei})">detect</button></div>
    </div>
    <div class="row">
      <div class="field"><label>remote endpoint UUID</label>
        <input type="text" id="rep${ei}" value="${esc(g.remote_endpoint_id || '')}" size="40"
               onchange="setGlobus(${ei}, 'remote_endpoint_id', this.value)"></div>
      <div class="field"><label>remote display name</label>
        <input type="text" id="ren${ei}" value="${esc(g.remote_endpoint_name || '')}"
               onchange="setGlobus(${ei}, 'remote_endpoint_name', this.value)"></div>
      <div class="field" style="justify-content:flex-end">
        <button class="subtle" onclick="searchRemote(${ei})">search</button></div>
    </div>
    <div class="field"><input type="text" id="gq${ei}"
         placeholder="search text — the endpoint's display name" size="40"></div>
    <div class="searchrows" id="gres${ei}"></div>
    <div class="note">a read-only <code>globus ls</code> succeeding does NOT prove
      transfers will work — verify with one small test transfer.</div>`;
  return `<div class="zone">
    <div class="zhead"><span class="zname">data transfer</span>
      <span class="zgloss">how bytes cross the network — scp via the head node
        (default, no setup), or Globus (off-head-node, checksummed end-to-end)</span></div>
    <div class="chips">
      <span class="chip ${!dt ? 'on' : ''}" onclick="setTransfer(${ei}, '')">default (scp)</span>
      ${META.transfer_types.map(x =>
        `<span class="chip ${t === x ? 'on' : ''}" onclick="setTransfer(${ei}, '${x}')">${x}</span>`).join('')}
    </div>${globusFields}</div>`;
}

function renderSlurm(ei) {
  const env = doc.compute_envs[ei];
  const s = env.slurm || {};
  const gpu = s.gpu || null;
  return `<div class="zone">
    <div class="zhead"><span class="zname">slurm policy</span>
      <span class="zgloss">this cluster's constants — per-job time/mem/cpus are
        passed at run time, never declared here; leave blank to omit</span></div>
    <div class="row">
      <div class="field"><label>account</label>
        <input type="text" value="${esc(s.account || '')}"
               onchange="setSlurm(${ei}, 'account', this.value)"></div>
      <div class="field"><label>default CPU partition</label>
        <input type="text" value="${esc(s.partition || '')}"
               onchange="setSlurm(${ei}, 'partition', this.value)"></div>
      <div class="field"><label>gpu partition (convention)</label>
        <input type="text" value="${esc(gpu ? gpu.partition : '')}"
               onchange="setSlurmGpu(${ei}, 'partition', this.value)"></div>
      <div class="field"><label>gpu qos (convention)</label>
        <input type="text" value="${esc(gpu ? gpu.qos : '')}"
               onchange="setSlurmGpu(${ei}, 'qos', this.value)"></div>
    </div>
    <div class="note" style="color:var(--dim)">the GPU pair is fillable, never
      required — discover real values with <code>cluster_partitions</code>; a job's
      own slurm settings win over these.</div></div>`;
}

function renderEnvs() {
  const cards = (doc.compute_envs || []).map((e, i) => renderEnv(e, i)).join('');
  return `<h2>Compute Environments</h2>
    <p class="hint">Where the agent may run jobs. <b>local</b> is this machine —
    a first-class environment that unlocks production runs on your own hardware;
    <b>ssh</b> is a remote machine (an HPC cluster). Each env declares the same
    four zones, which is what makes a production run the same kind of thing on
    either.</p>
    ${cards || '<p class="hint">none declared yet</p>'}
    <div class="row">
      <button onclick="addEnv('local')">+ local env (this machine)</button>
      <button onclick="addEnv('ssh')">+ ssh env (a cluster)</button>
    </div>`;
}

// ---------- projects ----------
function renderProjects() {
  if (!projectsUnlocked()) {
    return `<h2>Projects</h2>
      <div class="lockbox"><div class="glyph">⬦</div>
        <p><b>Locked — needs a remote env.</b></p>
        <p style="margin-top:8px">${esc(META.projects_locked_note)}</p></div>`;
  }
  const namePat = new RegExp(META.project_name_pattern);
  const cards = (doc.projects || []).map((p, pi) => {
    const badName = !namePat.test(p.name || '');
    const envChecks = (doc.compute_envs || []).map((e, eidx) => `
      <label style="font-size:12px;cursor:pointer;margin-right:14px">
        <input type="checkbox" ${(p.compute_envs || []).includes(e.name) ? 'checked' : ''}
               onchange="toggleProjEnv(${pi}, ${eidx}, this.checked)">
        ${esc(e.name)} <span style="color:var(--dim)">(${e.type})</span></label>`).join('');
    const dirs = (p.directories || []).map((d, di) => `
      <div class="zone">
        <div class="row">
          <div class="field"><label>env</label>
            <select onchange="setDir(${pi}, ${di}, 'env', this.value)">
              ${(p.compute_envs || []).map(n =>
                `<option ${d.env === n ? 'selected' : ''}>${esc(n)}</option>`).join('')}
            </select></div>
          <div class="field"><label>absolute path</label>
            <input type="text" value="${esc(d.path || '')}" size="44"
                   placeholder="/work/mylab/rnaseq_2026"
                   onchange="setDir(${pi}, ${di}, 'path', this.value)"></div>
          <div class="field"><label>description</label>
            <input type="text" value="${esc(d.description || '')}"
                   onchange="setDir(${pi}, ${di}, 'description', this.value)"></div>
          <div class="field" style="justify-content:flex-end">
            <button class="danger" onclick="removeDir(${pi}, ${di})">remove</button></div>
        </div>
        <div class="chips">` +
        META.permission_order.map(t =>
          `<span class="chip ${(d.permissions || []).includes(t) ? 'on' : ''}"
                 title="${esc(META.permission_glosses[t])}"
                 onclick="toggleDirPerm(${pi}, ${di}, '${t}')">${t}</span>`).join('') +
        `</div></div>`).join('');
    return `<div class="card">
      <h3><input type="text" class="${badName ? 'badname' : ''}"
                 value="${esc(p.name || '')}" size="24"
                 onchange="setProj(${pi}, 'name', this.value)">
        <span style="flex:1"></span>
        <button class="danger" onclick="removeProj(${pi})">remove</button></h3>
      ${badName ? '<div class="errline">name must be letters, digits, . _ and - only (no leading . or -) — it becomes a path component (the scratch prefix)</div>' : ''}
      <div class="row"><div class="field"><label>description</label>
        <input type="text" value="${esc(p.description || '')}" size="50"
               onchange="setProj(${pi}, 'description', this.value)"></div></div>
      <div class="field"><label>compute envs this project may use</label>
        <div>${envChecks}</div></div>
      <div class="field" style="margin-top:10px"><label>directories — the explicit
        grants into YOUR territory (the env's scratch/common_data/container/report
        zones come with the env; do not re-declare them here)</label></div>
      ${dirs}
      <div style="margin-top:8px"><button class="subtle" onclick="addDir(${pi})">+ directory</button></div>
    </div>`;
  }).join('');
  return `<h2>Projects</h2>
    <p class="hint">A project is a label for a piece of work plus the list of
    YOUR directories the agent may touch for it. Permissions are independent
    grants, not a ladder — <code>upload</code> does not imply <code>download</code>.</p>
    ${cards || '<p class="hint">none declared yet</p>'}
    <button onclick="addProj()">+ project</button>`;
}

// ---------- file + reference ----------
function renderFile() {
  return `<h2>File</h2>
    <p class="hint">The exact bytes a save writes to
    <code>${esc(BOOT.path)}</code>. A save keeps the previous version as
    <code>.bak</code>. Hand-editing the file is fine — this menu re-reads and
    validates it. An agent can author it directly (schema:
    <code>${esc(META.example_path)}</code>) and check with <code>--validate</code>.</p>
    <pre class="filedump" id="filedump">…</pre>`;
}

function renderReference() {
  const permRows = META.permission_order.map(t =>
    `<tr><td>${t}</td><td>${esc(META.permission_glosses[t])}</td></tr>`).join('');
  const zoneRows = META.zones.map(z =>
    `<tr><td>${z.label}${z.required ? ' *' : ''}</td><td>${esc(z.gloss)}</td></tr>`).join('');
  return `<h2>Reference</h2>
    <h3 style="margin:14px 0 6px;color:var(--tx)">Permission tokens</h3>
    <p class="hint">Independent grants, not a ladder — granting one never implies another.</p>
    <table class="legend">${permRows}</table>
    <h3 style="margin:18px 0 6px;color:var(--tx)">Environment zones</h3>
    <p class="hint">Every env declares the same zones (* = required — the bridge's
    run and stage primitives refuse without them).</p>
    <table class="legend">${zoneRows}</table>
    <h3 style="margin:18px 0 6px;color:var(--tx)">Driving this without a browser</h3>
    <p class="hint">The terminal menu is <code>./scripts/config.sh</code>. An agent
    (or script) should write the YAML directly — annotated schema at
    <code>${esc(META.example_path)}</code> — and check it with
    <code>./scripts/config.sh --validate</code>. This page's own surface is plain
    JSON: GET /config, POST /validate, POST /save (header
    <code>X-Bioinf-Config: 1</code>).</p>`;
}

// ---------- mutations ----------
function addEnv(type) {
  const names = (doc.compute_envs || []).map(e => e.name);
  let base = type === 'ssh' ? 'cluster' : 'laptop', name = base, n = 2;
  while (names.includes(name)) name = base + '_' + (n++);
  const env = {name, type};
  const defaults = type === 'ssh' ? META.ssh_zone_templates : META.local_zone_defaults;
  for (const z of META.zones) {
    const path = (defaults[z.key] || '').replace('{user}', 'USER');
    env[z.key] = {path, permissions: [...z.default_perms], description: z.gloss};
  }
  if (type === 'ssh') { env.host = ''; env.job_manager = META.job_managers[0]; }
  doc.compute_envs.push(env);
  markDirty(); render();
}
function removeEnv(ei) {
  const name = doc.compute_envs[ei].name;
  const users = (doc.projects || []).filter(p => (p.compute_envs || []).includes(name))
                                    .map(p => p.name);
  if (users.length) {
    alert(`${name} is used by project(s): ${users.join(', ')} — remove it from those projects first.`);
    return;
  }
  doc.compute_envs.splice(ei, 1); markDirty(); render();
}
function renameEnv(ei, name) {
  const old = doc.compute_envs[ei].name;
  doc.compute_envs[ei].name = name;
  for (const p of doc.projects || []) {
    p.compute_envs = (p.compute_envs || []).map(e => e === old ? name : e);
    for (const d of p.directories || []) if (d.env === old) d.env = name;
  }
  markDirty(); render();
}
function setEnv(ei, key, val) {
  if (val) doc.compute_envs[ei][key] = val; else delete doc.compute_envs[ei][key];
  markDirty(); render();
}
function setEnvUser(ei, user) {
  // Re-derive still-templated ssh zone paths from the typed user, but never
  // touch a path the user has edited away from the template.
  const env = doc.compute_envs[ei];
  const prev = env.user || 'USER';
  for (const z of META.zones) {
    const tmpl = (META.ssh_zone_templates[z.key] || '');
    if (env[z.key] && env[z.key].path === tmpl.replace('{user}', prev))
      env[z.key].path = tmpl.replace('{user}', user || 'USER');
  }
  setEnv(ei, 'user', user);
}
function toggleZone(ei, key, on) {
  const env = doc.compute_envs[ei];
  if (on) {
    const z = META.zones.find(x => x.key === key);
    const defaults = env.type === 'ssh' ? META.ssh_zone_templates : META.local_zone_defaults;
    env[key] = {path: (defaults[key] || '').replace('{user}', env.user || 'USER'),
                permissions: [...z.default_perms], description: z.gloss};
  } else delete env[key];
  markDirty(); render();
}
function setZone(ei, key, field, val) {
  const blk = doc.compute_envs[ei][key];
  if (val) blk[field] = val; else delete blk[field];
  markDirty(); render();
}
function togglePerm(ei, key, tok) {
  const blk = doc.compute_envs[ei][key];
  const i = (blk.permissions || []).indexOf(tok);
  if (i >= 0) blk.permissions.splice(i, 1); else (blk.permissions ||= []).push(tok);
  markDirty(); render();
}
function setTransfer(ei, t) {
  const env = doc.compute_envs[ei];
  if (!t) delete env.data_transfer;
  else if (t === 'globus')
    env.data_transfer = {type: 'globus',
      globus: (env.data_transfer && env.data_transfer.globus) ||
              {local_endpoint_id: '', local_endpoint_name: '',
               remote_endpoint_id: '', remote_endpoint_name: ''}};
  else env.data_transfer = {type: t};
  markDirty(); render();
}
function setGlobus(ei, key, val) {
  doc.compute_envs[ei].data_transfer.globus[key] = val; markDirty(); render();
}
function setSlurm(ei, key, val) {
  const env = doc.compute_envs[ei];
  const s = env.slurm || {};
  if (val) s[key] = val; else delete s[key];
  if (Object.keys(s).length) env.slurm = s; else delete env.slurm;
  markDirty(); render();
}
function setSlurmGpu(ei, key, val) {
  const env = doc.compute_envs[ei];
  const s = env.slurm || {};
  const gpu = s.gpu || {partition: '', qos: ''};
  gpu[key] = val;
  if (!gpu.partition && !gpu.qos) delete s.gpu; else s.gpu = gpu;
  if (Object.keys(s).length) env.slurm = s; else delete env.slurm;
  markDirty(); render();
}

function addProj() {
  doc.projects.push({name: 'project_' + ((doc.projects || []).length + 1),
                     compute_envs: [], directories: []});
  markDirty(); render();
}
function removeProj(pi) { doc.projects.splice(pi, 1); markDirty(); render(); }
function setProj(pi, key, val) {
  if (val) doc.projects[pi][key] = val; else delete doc.projects[pi][key];
  markDirty(); render();
}
// Handler args are INDICES, never user strings: a name interpolated into a JS
// string literal inside an on* attribute survives esc() (the browser decodes
// entities before compiling the handler), which is an XSS door.
function toggleProjEnv(pi, eidx, on) {
  const name = doc.compute_envs[eidx].name;
  const p = doc.projects[pi];
  p.compute_envs = (p.compute_envs || []).filter(e => e !== name);
  if (on) p.compute_envs.push(name);
  const before = (p.directories || []).length;
  p.directories = (p.directories || []).filter(d => p.compute_envs.includes(d.env));
  if (p.directories.length < before)
    alert('dropped directory grant(s) on env(s) no longer listed');
  markDirty(); render();
}
function addDir(pi) {
  const p = doc.projects[pi];
  if (!(p.compute_envs || []).length) { alert('pick a compute env for this project first'); return; }
  (p.directories ||= []).push({env: p.compute_envs[0], path: '',
                               permissions: [...META.dir_default_perms]});
  markDirty(); render();
}
function removeDir(pi, di) { doc.projects[pi].directories.splice(di, 1); markDirty(); render(); }
function setDir(pi, di, key, val) {
  if (val) doc.projects[pi].directories[di][key] = val;
  else delete doc.projects[pi].directories[di][key];
  markDirty(); render();
}
function toggleDirPerm(pi, di, tok) {
  const d = doc.projects[pi].directories[di];
  const i = (d.permissions || []).indexOf(tok);
  if (i >= 0) d.permissions.splice(i, 1); else (d.permissions ||= []).push(tok);
  markDirty(); render();
}

// ---------- globus picker ----------
async function detectLocal(ei) {
  const r = await fetch('/globus/local-id');
  const j = await r.json();
  if (j.id) { setGlobus(ei, 'local_endpoint_id', j.id); }
  else alert('no local Globus Connect Personal endpoint detected — is the globus CLI installed and GCP running?');
}
async function searchRemote(ei) {
  const q = document.getElementById('gq' + ei).value.trim() ||
            (doc.compute_envs[ei].data_transfer.globus.remote_endpoint_name || '');
  if (!q) { alert('type a search text (the endpoint display name) first'); return; }
  const box = document.getElementById('gres' + ei);
  box.innerHTML = '<span style="color:var(--dim);font-size:12px">searching…</span>';
  const r = await fetch('/globus/search?q=' + encodeURIComponent(q));
  const j = await r.json();
  if (j.why) { box.innerHTML = `<div class="note">${esc(j.why)}</div>`; return; }
  if (!j.rows.length) { box.innerHTML = '<div class="note">no endpoints matched</div>'; return; }
  box.innerHTML = j.rows.map((row, i) =>
    `<div class="srow" onclick="pickRemote(${ei}, ${i})">
       <b>${esc(row.display_name || '?')}</b>
       <span class="sid">${esc(row.id)} — ${esc(row.owner || '')}</span></div>`).join('');
  window._globusRows = j.rows;
}
function pickRemote(ei, i) {
  const row = window._globusRows[i];
  const g = doc.compute_envs[ei].data_transfer.globus;
  g.remote_endpoint_id = row.id;
  g.remote_endpoint_name = row.display_name || '';
  markDirty(); render();
}

// ---------- validate / save / preview ----------
let vtimer = null;
function scheduleValidate() {
  clearTimeout(vtimer);
  vtimer = setTimeout(doValidate, 400);
}
async function doValidate() {
  const r = await fetch('/validate', {method: 'POST', headers: HDRS,
                                      body: JSON.stringify(doc)});
  lastVerdict = await r.json();
  renderVerdict();
}
function renderVerdict() {
  const state = lastVerdict.state || BOOT.state;
  const pill = document.getElementById('statepill');
  pill.textContent = state;
  pill.className = 'pill ' + state;
  const v = document.getElementById('verdict');
  const notes = (lastVerdict.notes || []).map(n => `<div class="note">note: ${esc(n)}</div>`).join('');
  // The unmanaged-keys warning must survive edits: the save is the moment the
  // keys are actually dropped, so hiding it once dirty would hide it exactly
  // when it matters.
  const loadErr = BOOT.load_error ? `<div class="note">${esc(BOOT.load_error)}</div>` : '';
  if (state === 'absent')
    v.innerHTML = `<div class="note">no configuration file yet — nothing validated; SAVE writes the first one</div>` + loadErr + notes;
  else if (state === 'invalid')
    v.innerHTML = `<span class="errline">${esc(lastVerdict.message)}</span>` + loadErr + notes;
  else
    v.innerHTML = `<span class="okline">the agent's loader accepts this configuration</span>` + loadErr + notes;
}
async function saveCfg() {
  const r = await fetch('/save', {method: 'POST', headers: HDRS,
                                  body: JSON.stringify(doc)});
  const j = await r.json();
  const v = document.getElementById('verdict');
  v.innerHTML = j.messages.map(m =>
    `<div class="${m.kind === 'error' ? 'errline' : m.kind === 'ok' ? 'okline' : 'note'}">${esc(m.text)}</div>`).join('');
  if (j.ok) { dirty = false; document.getElementById('dirtypill').style.display = 'none'; }
}
async function revert() {
  const r = await fetch('/config');
  const j = await r.json();
  doc = j.config; dirty = false;
  lastVerdict = {state: j.state, message: j.message, notes: []};
  document.getElementById('dirtypill').style.display = 'none';
  render();
}
async function loadPreview() {
  const r = await fetch('/preview', {method: 'POST', headers: HDRS,
                                     body: JSON.stringify(doc)});
  const j = await r.json();
  const el = document.getElementById('filedump');
  if (el) el.textContent = j.yaml;
}

render();
if (BOOT.state !== 'absent') scheduleValidate();
</script>
</body>
</html>
"""


if __name__ == "__main__":
    print("run via ./scripts/config.sh --web (this module is the renderer, "
          "not the entry point)", file=sys.stderr)
    sys.exit(2)
