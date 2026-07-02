#!/usr/bin/env python3
"""
render_decision_tree.py — render the system's scenario decision tree as a
self-contained, offline HTML visualization (no external libs, no CDN).

The tree is held as structured data below (the hand-maintained "scenario overlay"
described in docs/scenario_decision_tree.md). Rendering is a pure function over
that data, so this could later be fed by an AST skeleton-extractor and stay in
sync with the code.

Output: docs/scenario_decision_tree.html

Node model:
    N(label, outcome=None, kind="branch", *children)
      - kind: "stage" | "q" (decision) | "branch" (a condition) | "root"
      - outcome (leaf tag): proven | refused | broke | vanished | degraded | loop
      - children: nested N(...)

Visual: nested collapsible tree (<details>) with elbow connectors; leaves carry a
colored outcome pill. A little vanilla JS gives expand-all / collapse-all + filter
by outcome (the tree is fully usable without JS — pure <details>).
"""
from __future__ import annotations

import json
from pathlib import Path


# ---------------------------------------------------------------------------
# Node helper
# ---------------------------------------------------------------------------

def N(label, *children, outcome=None, kind="branch"):
    return {"label": label, "outcome": outcome, "kind": kind,
            "children": list(children)}


OUTCOMES = {
    # key: (symbol, name, color, blurb)
    "proven":   ("✅", "PROVEN",   "#3ce086", "honest green — frozen & validated, or sealed with evidence"),
    "refused":  ("⛔", "REFUSED",  "#22e3ee", "loud, honest, recoverable — a gate said no before writing anything"),
    "broke":    ("💥", "BROKE",    "#ff9f43", "loud hard failure, RECORDED — install/build/tool failed, captured"),
    "vanished": ("👻", "VANISHED", "#ff4bd8", "loud to agent, NOT recorded — failed with no durable trace (the thin spot)"),
    "degraded": ("⚠️", "DEGRADED", "#fff200", "proceeded, weaker proof — observed-only / emulated / fallback"),
    "loop":     ("🔁", "LOOP",     "#5b8cff", "recoverable, feeds back — informs a retry"),
}


# ===========================================================================
# THE TREE  (mirrors docs/scenario_decision_tree.md)
# ===========================================================================

TREE = N("🧑‍🔬 Scientist — I have a tool + some data, I want a trustworthy artifact",
  kind="root",
  *[
    # ---- STAGE 0 : RESOLVE ------------------------------------------------
    N("STAGE 0 · resolve_tool — pick a tier", kind="stage",
      *[
        N("Where does this tool live?", kind="q", *[
          N("found on conda (bioconda / conda-forge) → tier=conda (preferred)", outcome="proven"),
          N("found on PyPI only → tier=pip"),
          N("found on CRAN / Bioconductor only → tier=cran/bioc"),
          N("github_repo given → release asset → tier=binary"),
          N("github_repo given → buildable source → tier=source"),
          N("found on BOTH PyPI and CRAN, no language= → pass language='python'|'r'", outcome="refused"),
          N("github_repo given but PyPI/CRAN name doesn't reference that repo "
            "→ tier disqualified (cross-namespace collision, e.g. PyPI 'gab' ≠ baumannlab/Genome_Assembly_Booster)", outcome="refused"),
          N("nothing anywhere → manual / not found — scientist decides", outcome="refused"),
        ]),
      ]),

    # ---- STAGE 1 : INSTALL -----------------------------------------------
    N("STAGE 1 · install into the env (per tier)", kind="stage",
      *[
        N("Did the tier's install succeed AND verify?", kind="q", *[
          N("CONDA", kind="q", *[
            N("solver rc=0 → installed", outcome="proven"),
            N("solver rc≠0 (conflict / mirror down) → loop (relax pins / channel)", outcome="broke"),
          ]),
          N("PIP", kind="q", *[
            N("install rc=0 AND import check passes → installed", outcome="proven"),
            N("install rc=0 BUT import fails → success=false (pip 'installs' broken dep silently)", outcome="broke"),
          ]),
          N("R (cran / bioc / github)", kind="q", *[
            N("unknown source type → error before Rscript runs", outcome="refused"),
            N("install rc=0 AND requireNamespace loads → installed", outcome="proven"),
            N("install rc≠0, stderr names missing deps → missing_packages[] → install deps, retry", outcome="loop"),
            N("install rc=0 BUT namespace won't load → success=false (load-or-die)", outcome="broke"),
          ]),
          N("BINARY (release / vendor URL)", kind="q", *[
            N("sha256 matches AND smoke verify runs → installed", outcome="proven"),
            N("sha256 MISMATCH → HARD FAIL, step not merged (wrong bytes)", outcome="broke"),
            N("sha256 ok BUT won't exec (wrong arch) → verify catches libc/CPU mismatch", outcome="broke"),
          ]),
          N("SOURCE (git clone + build)", kind="q", *[
            N("neither bin_path nor entrypoint (or both) → ambiguous shape error", outcome="refused"),
            N("host_build=false → build DEFERRED to freeze image (cross-arch)"),
            N("build + smoke verify ok → installed (anchored by commit_sha)", outcome="proven"),
          ]),
          N("PERL (cpanm) — perl -M{module} -e1 loads → installed / else broke (cpanm exits 0 with broken XS)", outcome="proven"),
          N("CARGO / GO — build ok → cli_which anchor + toolchain version recorded → installed", outcome="proven"),
          N("JAR — download+extract → heuristic jar pick (name match, shortest) → installed", outcome="proven"),
          N("SYNTHESIS (agent-authored long-tail)", kind="q", *[
            N("fetch fails → early error (no docker build wasted)", outcome="refused"),
            N("anchor drift (commit/sha256 moved) → refuse (provenance broke)", outcome="refused"),
            N("agent-authored URL not found in repo → validate_submission refuses", outcome="refused"),
          ]),
        ]),
      ]),

    # ---- STAGE 2 : FREEZE -------------------------------------------------
    N("STAGE 2 · freeze → Layer-1 env artifact", kind="stage",
      *[
        N("Q0 — can we even start?", kind="q", *[
          N("free disk < threshold → refuse early (A1) — 'docker buildx prune'", outcome="refused"),
          N("gated=true AND licenses[] empty → refuse early (F2/I13) BEFORE build cost", outcome="refused"),
        ]),
        N("Q1 — EnvCache hit? (request_key = tools + versions + platform + accel policy)", kind="q", *[
          N("hit AND image still in daemon → return proven artifact by hash (no re-solve)", outcome="proven"),
          N("hit BUT image evicted from daemon → miss → rebuild (never serve a stale record)"),
        ]),
        N("Q2 — ADOPT or BUILD?", kind="q", *[
          N("pure conda + public biocontainer, not gated, no env-mutating steps → ADOPT path"),
          N("has non-conda installs / pip-via-run_in_env → BUILD path (biocontainer can't represent it)"),
          N("gated=true → BUILD path (I13: gated is NEVER adopted)"),
          N("biocontainer lookup", kind="q", *[
            N("exact version tag found → adoptable"),
            N("version set never pre-built upstream → miss → BUILD"),
            N("quay.io API network error → treated as miss → BUILD (no crash)"),
          ]),
        ]),
        N("Q3a — ADOPT honesty (check_adopt: POLICY_CLEAN only, trusted by digest)", kind="q", *[
          N("I12 accelerator + I13 license pass → adopted by digest (no VALIDATED_IN_IMAGE — bytes trusted, not run)", outcome="proven"),
          N("violation → refuse", outcome="refused"),
        ]),
        N("Q3b — BUILD honesty (build in-container, then check_build)", kind="q", *[
          N("a non-conda install is non-replayable (source w/o bin_path, binary w/o platform asset, unreachable sha256) → error BEFORE docker build", outcome="refused"),
          N("docker build stage fails", kind="q", *[
            N("base image unpullable → BUILT fails (no image at all)", outcome="broke"),
            N("conda solve conflict / package gone → build fails", outcome="broke"),
            N("long-tail cmd rc≠0 (wrong-arch, build err) → build fails", outcome="broke"),
          ]),
          N("image built → check_build", kind="q", *[
            N("BUILT: image + digest resolve in daemon (else BUILT.image_present)"),
            N("VALIDATED_IN_IMAGE: every tool re-runs green in the shipped image", kind="q", *[
              N("no evidence collected → VALIDATED_IN_IMAGE.no_evidence", outcome="refused"),
              N("echo / true / : / [ 1=1 ] cheat shape → evidence_shape violation", outcome="refused"),
              N("tool absent or rc≠0 in image → evidence_passed violation", outcome="refused"),
            ]),
            N("POLICY_CLEAN", kind="q", *[
              N("I12 cuda/rocm w/o toolkit_version → refuse", outcome="refused"),
              N("I12 runtime_verified w/o probe + min_driver_version → refuse", outcome="refused"),
              N("I12 mps not dev_only → refuse", outcome="refused"),
              N("I13 gated but redistributable / no license → refuse", outcome="refused"),
            ]),
            N("PROVENANCE_CLEAN (synthesis): empty cmds or untagged source → refuse", outcome="refused"),
            N("all pass → Layer-1 env ✔", outcome="proven"),
          ]),
        ]),
        N("Q4 — delivery", kind="q", *[
          N("adopt → apptainer pull docker://…@digest", outcome="proven"),
          N("build + registry/push_target, push ok → apptainer pull docker://ref", outcome="proven"),
          N("build + push FAILS → tarball fallback (reported, never silent)", outcome="degraded"),
          N("build + gated → tarball only, NEVER pushed (I13)"),
          N("build + no registry (default) → docker save → apptainer build (registry-free)", outcome="proven"),
          N("→ writes ENV.html (immutable) + attestation.json"),
        ]),
      ]),

    # ---- STAGE 3A : VALIDATE LOCAL ---------------------------------------
    N("STAGE 3A · validate LOCALLY (run_step_in_container)", kind="stage",
      *[
        N("Run the step inside the frozen image", kind="q", *[
          N("no pipeline_id / no frozen env / record has no image → refuse (call freeze first)", outcome="refused"),
          N("Docker daemon is REMOTE (DOCKER_HOST) → refuse (can't bind-mount local data)", outcome="refused"),
          N("adopted image not local AND pull fails → refuse", outcome="refused"),
          N("container runs, rc=0", kind="q", *[
            N("each output type-validated, all pass → validated step (feeds seal)", outcome="proven"),
            N("an output FAILS type validation (bad BAM/VCF/JSON) → recorded passed=False → seal will REFUSE (C1/I3)", outcome="degraded"),
            N("measured under emulation (arm64→amd64) → I7 stamped not-authoritative (timings unreliable)", outcome="degraded"),
          ]),
          N("container runs, rc≠0 (tool crash / missing lib / wrong flag) → step recorded, NO validation → loop (fix cmd) or REBUILD env (Stage 2)", outcome="broke"),
        ]),
      ]),

    # ---- STAGE 3B : VALIDATE CLUSTER -------------------------------------
    N("STAGE 3B · validate ON CLUSTER (run_step_on_cluster + HPC bridge)", kind="stage",
      *[
        N("Q-auth — can the agent touch this cluster at all?", kind="q", *[
          N("projects_access.yaml missing / malformed → ConfigError", outcome="refused"),
          N("compute env not type=ssh → refuse", outcome="refused"),
          N("env has no agent_scratch_target → hard-fail (no sandbox to work in)", outcome="refused"),
          N("project lacks env access / scratch lacks exec → PermissionDenied", outcome="refused"),
          N("ssh session not open (no ControlMaster) → rc255 → hint 'open ssh hpc-agent' (actionable)", outcome="broke"),
          N("transfer zone breach (path not under <scratch>/<project>/…; wrong permission token) → PermissionDenied (upload≠download≠exec)", outcome="refused"),
          N("unsafe token (job_id, path, module has metachar) → refuse BEFORE any ssh", outcome="refused"),
        ]),
        N("Q-input — are declared inputs already on the cluster? (remote_paths_exist, loud precheck)", kind="q", *[
          N("all inputs present → proceed"),
          N("any input missing → refuse BEFORE sbatch (no auto-staging; user's rails own data)", outcome="refused"),
        ]),
        N("Q-stage — get the .sif onto the cluster", kind="q", *[
          N("no container_upload_target (build-archive) → refuse (no fallback zone)", outcome="refused"),
          N("tar upload / apptainer pull|build fails → refuse", outcome="refused"),
          N("already staged → idempotent skip"),
          N("staged → C2 fingerprint (inspect_staged_sif)", kind="q", *[
            N("label source digest == pinned env digest → cryptographically verified", outcome="proven"),
            N("no comparable digest (build_archive) → observed-only (sha256 + inspect-ok)", outcome="degraded"),
            N("inspect fails / SIF_MISSING → cluster_image_verified=False", outcome="degraded"),
          ]),
        ]),
        N("Q-transfer — wire protocol (per env.data_transfer)", kind="q", *[
          N("scp_head_node, file > 5 GiB → refuse (use globus)", outcome="refused"),
          N("scp, sha256 round-trip MISMATCH → file unlinked, refuse (corrupt)", outcome="broke"),
          N("no-overwrite: remote file exists → refuse", outcome="refused"),
          N("globus sync → SUCCEEDED (end-to-end checksum)", outcome="proven"),
          N("globus async → 'submitted' (task_id) → MUST confirm via globus_task_status", kind="q", outcome="loop", *[
            N("later SUCCEEDED → manifest → 'uploaded' (half-baked-transfer defense)", outcome="proven"),
            N("later FAILED → manifest → 'failed'", outcome="broke"),
          ]),
          N("globus consent / accessible-folders misconfig → hard error (never silent scp fallback)", outcome="broke"),
        ]),
        N("Q-run — submit + poll + fetch", kind="q", *[
          N("render main.nf/config/launcher fails → refuse", outcome="refused"),
          N("upload of a rendered file fails → refuse", outcome="refused"),
          N("sbatch fails (bad partition/account, sbatch gone) → refuse — NO step recorded, files leaked in scratch", outcome="vanished"),
          N("poll exceeds cap (default 60 min), no terminal → refuse — job may STILL be running, NO step recorded", outcome="vanished"),
          N("job terminal, rc≠0 (crash / OOM / missing lib) → step recorded rc≠0, NO validation → loop / REBUILD", outcome="broke"),
          N("job terminal, rc=0 BUT sacct query errored → resource_usage all-zeros + sacct_error → seal REFUSES (C3/I7)", outcome="degraded"),
          N("output download fails → download_errors[], partial outputs unvalidated", outcome="degraded"),
          N("job rc=0, outputs downloaded + type-validated → cluster-locus validated step (feeds seal)", outcome="proven"),
        ]),
      ]),

    # ---- STAGE 4 : SEAL ---------------------------------------------------
    N("STAGE 4 · seal → Layer-2 workflow artifact (seal_workflow)", kind="stage",
      *[
        N("Q-pre", kind="q", *[
          N("unknown pipeline_id / no frozen env → refuse", outcome="refused"),
        ]),
        N("Invariant gauntlet — refuse on ANY", kind="q", *[
          N("I0 shape — a list field holds a non-dict → refuse", outcome="refused"),
          N("I3 outputs", kind="q", *[
            N("rc=0 step produced NO outputs (not marked) → silent-empty-success", outcome="refused"),
            N("outputs exist but never validated → refuse", outcome="refused"),
            N("an output's validation is passed=FALSE (C1) → can't seal a proven-bad output", outcome="refused"),
            N("validation used expected_type='any' → lazy (touch foo.bar would pass)", outcome="refused"),
          ]),
          N("I6 paths", kind="q", *[
            N("a relative input/output path → repro landmine", outcome="refused"),
            N("usage template {PLACEHOLDER} not declared → typo/undeclared slot", outcome="refused"),
          ]),
          N("I7 resources", kind="q", *[
            N("rc=0 step has no resource_usage → monitor never saw it run", outcome="refused"),
            N("resource_usage carries sacct_error → (C3) fabricated zeros", outcome="refused"),
            N("resource_usage all-zeros → (C3) no honest cost data", outcome="refused"),
          ]),
          N("I8 provenance", kind="q", *[
            N("a step input traces to no source → orphan (doesn't compose)", outcome="refused"),
            N("authored artifact missing / sha256 drifted → spec claim ≠ disk", outcome="refused"),
            N("same-path bytes changed between steps → lineage mutated", outcome="refused"),
          ]),
          N("I4 usage", kind="q", *[
            N("usage.command_template self-test FAILS (H2) → won't ship a broken runnable form", outcome="refused"),
            N("NO usage block → skip (seals without usage_verified badge)"),
          ]),
          N("self-verify — constructed spec fails its OWN invariants → wouldn't re-verify standalone", outcome="refused"),
        ]),
        N("ALL PASS → write workflow.yaml + RUN.html; set validated_in_shipped_image (digest match) + usage_verified → SEALED", outcome="proven"),
        N("⚠️ KNOWN GAP — a workflow whose ONLY step FAILED (rc≠0) with NO usage block hits zero violations "
          "(failed steps are skipped by I3/I7/I8) → it SEALS. Badges are all False (not a lie), but it's a sealed "
          "artifact for a run that never worked, and RUN.html doesn't yet mark the step FAILED.", outcome="vanished"),
      ]),

    # ---- STAGE 5 : PRODUCTION --------------------------------------------
    N("STAGE 5 · production run (submit_workflow_job)", kind="stage",
      *[
        N("Submit the sealed workflow against the user's real workspace", kind="q", *[
          N("workflow_dir empty → refuse (must be explicit)", outcome="refused"),
          N("workflow_dir not in directories[] with upload+exec → PermissionDenied", outcome="refused"),
          N("render/upload/sbatch fails → refuse (with forensic launcher path)", outcome="refused"),
          N("sbatch ok → submit-and-document: returns job_id + writes submission.json (NO polling — prod jobs run for hours). "
            "Later: cluster_job_status → download outputs", outcome="proven"),
        ]),
      ]),
  ])


# ---------------------------------------------------------------------------
# Machine-readable model — the canonical JSON the visualization renders from.
# `label` is a terse node caption (for the box); `detail` is the full text (for
# the hover tooltip). Anything downstream (a different renderer, a d3 view, an
# analytics pass) layers on top of THIS.
# ---------------------------------------------------------------------------

def _terse(label: str) -> str:
    """A short caption for the node box. Take the condition (the part before the
    first arrow) — that's the DECISION; the consequence after the arrow is the
    outcome, already carried by the pill/color."""
    base = label.split("→")[0].strip() if "→" in label else label.strip()
    if not base:                        # labels that START with an arrow
        base = label.lstrip("→ ").strip()
    return base


def to_model(node: dict, ctr: list) -> dict:
    nid = ctr[0]
    ctr[0] += 1
    return {
        "id":       nid,
        "label":    _terse(node["label"]),
        "detail":   node["label"],
        "kind":     node["kind"],
        "outcome":  node["outcome"],
        "children": [to_model(c, ctr) for c in node["children"]],
    }


def _counts(node: dict, acc: dict) -> None:
    if node["outcome"]:
        acc[node["outcome"]] = acc.get(node["outcome"], 0) + 1
    for c in node["children"]:
        _counts(c, acc)


# ---------------------------------------------------------------------------
# Renderer — a top-down node-link graph (root at top, branches fan down), drawn
# as self-contained SVG. Collapsible nodes, pan/zoom, outcome color + filter.
# Pure vanilla JS over the embedded JSON model; NO external libraries.
# ---------------------------------------------------------------------------

_CSS = """
:root{
  --bg:#0a0c14;--surface:#13151f;--surface-2:#1a1d29;--border:#262a3a;
  --cyan:#22e3ee;--yellow:#fff200;--ink:#e6e9f0;--muted:#8e98ad;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--ink);overflow:hidden;
display:flex;flex-direction:column;
font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
header{flex:0 0 auto;padding:14px 20px 12px;border-bottom:1px solid var(--border);
background:linear-gradient(180deg,rgba(34,227,238,.05),transparent)}
h1{font-size:19px;font-weight:800;color:var(--yellow);margin:0 0 2px;letter-spacing:.01em}
.sub{color:var(--muted);font-size:12px;margin:0}
.bar{display:flex;flex-wrap:wrap;gap:14px;align-items:center;margin-top:10px}
.legend{display:flex;flex-wrap:wrap;gap:7px}
.chip{display:inline-flex;align-items:center;gap:6px;font-size:11px;font-weight:700;
padding:4px 9px;border-radius:2px;border:1px solid var(--border);background:var(--surface);
cursor:pointer;user-select:none;letter-spacing:.03em}
.chip .dot{width:9px;height:9px;border-radius:50%}
.chip.off{opacity:.3}
.chip .cnt{color:var(--muted);font-weight:600;margin-left:1px}
.btns{display:flex;gap:7px;flex-wrap:wrap;margin-left:auto}
.btn{font-size:11px;font-weight:700;padding:5px 11px;border:1px solid var(--cyan);
background:transparent;color:var(--cyan);cursor:pointer;border-radius:2px;letter-spacing:.05em}
.btn:hover{background:rgba(34,227,238,.12)}
#stage{flex:1 1 auto;width:100%;min-height:0;display:block;cursor:grab;touch-action:none}
#stage.drag{cursor:grabbing}
.edge{fill:none;stroke:#2f3446;stroke-width:1.3}
.edge.spine{stroke:#22e3ee;stroke-width:2.2;opacity:.5}
.nd rect{fill:var(--surface);stroke-width:1.6;rx:5}
.nd text.lbl{fill:var(--ink);font:600 11.5px/1 -apple-system,"Segoe UI",Roboto,sans-serif}
.nd.root rect{fill:rgba(255,242,0,.10)} .nd.root text.lbl{fill:var(--yellow);font-weight:800}
.nd.stage rect{fill:rgba(34,227,238,.12)} .nd.stage text.lbl{fill:var(--cyan);font-weight:800}
.nd.q text.lbl{fill:var(--ink)}
.nd{cursor:pointer}
.nd:hover rect{filter:brightness(1.35)}
.tog{fill:var(--surface-2);stroke:var(--cyan);stroke-width:1.4}
.tsym{fill:var(--cyan);font:700 11px var(--mono);pointer-events:none}
.oc-pill{font:800 9px -apple-system,sans-serif;fill:#000}
svg.filtering .nd:not(.match){opacity:.13}
svg.filtering .edge{opacity:.07}
.hint{position:fixed;bottom:12px;left:20px;color:var(--muted);font-size:11px}
"""

_JS = r"""
const DATA = __DATA__;
const OC = __OC__;
// vertical pipeline spine (stages stacked top→bottom); each stage fans its
// decision tree to the RIGHT within its own band.
const NW=214, NH=34, SPINE_X=54, ROW=56, ROWH=42, COL=234, GAP=28;

// parent refs (for edge drawing) + initial collapse to the spine
(function setp(n,p){ n._p=p; n.children.forEach(c=>setp(c,n)); })(DATA,null);
const collapsed=new Set();
function foldToSpine(){ collapsed.clear();
  (function w(n,d){ if(d>=1 && n.children.length) collapsed.add(n.id); n.children.forEach(c=>w(c,d+1)); })(DATA,0); }
foldToSpine();

const svg=document.getElementById('stage'), g=document.getElementById('vp');
let scale=1, tx=0, ty=0, bounds={w:1200,h:800};

function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));}
function trunc(s,n){return s.length>n? s.slice(0,n-1)+'…' : s;}

// left→right layout of one stage's descendants inside a horizontal band at `top`
function layoutBand(stage, top){
  const nodes=[stage]; let leaf=0;
  stage._x=SPINE_X; stage._y=top;
  function walk(n,depth){
    n._x=SPINE_X+depth*COL;
    const kids=collapsed.has(n.id)?[]:n.children;
    if(!kids.length){ n._y=top+leaf*ROWH; leaf++; }
    else{ kids.forEach(c=>walk(c,depth+1)); n._y=(kids[0]._y+kids[kids.length-1]._y)/2; }
    nodes.push(n);
  }
  stage.children.forEach(c=>walk(c,1));
  return {nodes, height:Math.max(leaf*ROWH, ROW)};
}

// vertical spine: root, then each stage; expanded stages consume a taller band
function layout(){
  const all=[]; const spine=[DATA]; let y=24;
  DATA._x=SPINE_X; DATA._y=y; all.push(DATA); y+=ROW;
  for(const st of DATA.children){
    spine.push(st);
    if(!collapsed.has(st.id) && st.children.length){
      const b=layoutBand(st,y); all.push(...b.nodes); y+=b.height+GAP;
    }else{ st._x=SPINE_X; st._y=y; all.push(st); y+=ROW; }
  }
  const xs=all.map(n=>n._x), ys=all.map(n=>n._y);
  bounds={w:Math.max(...xs)+NW+80, h:Math.max(...ys)+NH+50};
  return {all, spine};
}

function render(){
  const {all, spine}=layout();
  let edges='', nodes='';
  // spine chain — the end-to-end pipeline flow, drawn as a vertical line
  const cx=SPINE_X+NW/2;
  for(let i=0;i<spine.length-1;i++)
    edges+=`<path class="edge spine" d="M${cx},${spine[i]._y+NH} L${cx},${spine[i+1]._y}"/>`;
  // detail edges — parent → child inside each stage band (horizontal curves)
  all.forEach(n=>{
    const p=n._p;
    if(p && p!==DATA){
      const x1=p._x+NW, y1=p._y+NH/2, x2=n._x, y2=n._y+NH/2, mx=(x1+x2)/2;
      edges+=`<path class="edge" d="M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}"/>`;
    }
  });
  all.forEach(n=>{
    const oc=n.outcome;
    const stroke=oc?OC[oc].c:(n.kind==='stage'?'#22e3ee':n.kind==='root'?'#fff200':'#3a4056');
    const has=n.children.length>0;
    const label=(oc?OC[oc].s+' ':'')+trunc(n.label, oc?30:33);
    let tog='';
    if(has){ const s=collapsed.has(n.id)?'+':'−';
      tog=`<circle class="tog" cx="${NW}" cy="${NH/2}" r="8.5"/>`+
          `<text class="tsym" x="${NW}" y="${NH/2+3.8}" text-anchor="middle">${s}</text>`; }
    nodes+=`<g class="nd ${n.kind}${oc?' oc-'+oc:''}" data-id="${n.id}" data-outcome="${oc||''}" transform="translate(${n._x},${n._y})">`+
           `<rect width="${NW}" height="${NH}" rx="5" style="stroke:${stroke}"/>`+
           `<text class="lbl" x="11" y="${NH/2+4}">${esc(label)}</text>${tog}`+
           `<title>${esc(n.detail)}</title></g>`;
  });
  g.innerHTML=edges+nodes;
  applyFilter();
}

function apply(){ g.setAttribute('transform',`translate(${tx},${ty}) scale(${scale})`); }
function fit(){
  const r=svg.getBoundingClientRect();
  scale=Math.min(r.width/bounds.w, r.height/bounds.h, 1)*0.96;
  tx=(r.width-bounds.w*scale)/2; ty=20; apply();
}
function toggle(id){
  (function f(n){ if(n.id===id){ if(n.children.length){ collapsed.has(id)?collapsed.delete(id):collapsed.add(id); render(); } return true; }
    return n.children.some(f); })(DATA);
}

// ---- interactions: click toggles, drag pans (threshold separates them) ------
let ptr=null, moved=false;
svg.addEventListener('pointerdown',e=>{ ptr={x:e.clientX,y:e.clientY,tx,ty}; moved=false; svg.classList.add('drag'); });
window.addEventListener('pointermove',e=>{ if(!ptr)return;
  const dx=e.clientX-ptr.x, dy=e.clientY-ptr.y;
  if(Math.abs(dx)+Math.abs(dy)>4) moved=true;
  tx=ptr.tx+dx; ty=ptr.ty+dy; apply(); });
window.addEventListener('pointerup',e=>{
  if(ptr && !moved){
    const t=document.elementFromPoint(e.clientX,e.clientY);
    const nd=t && t.closest && t.closest('.nd');
    if(nd) toggle(+nd.dataset.id);
  }
  ptr=null; svg.classList.remove('drag');
});
svg.addEventListener('wheel',e=>{ e.preventDefault();
  const r=svg.getBoundingClientRect(), mx=e.clientX-r.left, my=e.clientY-r.top;
  const f=e.deltaY<0?1.12:1/1.12, ns=Math.min(2.5,Math.max(0.12,scale*f));
  tx=mx-(mx-tx)*(ns/scale); ty=my-(my-ty)*(ns/scale); scale=ns; apply();
},{passive:false});

document.getElementById('expand').onclick=()=>{ collapsed.clear(); render(); fit(); };
document.getElementById('collapse').onclick=()=>{ foldToSpine(); render(); fit(); };
document.getElementById('fit').onclick=fit;

// ---- outcome filter ---------------------------------------------------------
const active=new Set();
document.querySelectorAll('.chip').forEach(chip=>chip.onclick=()=>{
  const o=chip.dataset.outcome;
  active.has(o)?(active.delete(o),chip.classList.add('off')):(active.add(o),chip.classList.remove('off'));
  if(active.size) collapsed.clear();   // reveal every match
  else foldToSpine();
  render(); fit();
});
function applyFilter(){
  if(!active.size){ svg.classList.remove('filtering'); return; }
  svg.classList.add('filtering');
  g.querySelectorAll('.nd').forEach(nd=>nd.classList.toggle('match', active.has(nd.dataset.outcome)));
}

render(); fit();
window.addEventListener('resize',apply);
"""


def render_html(model: dict) -> str:
    acc: dict = {}
    _counts(model, acc)
    total = sum(acc.values())
    legend = "".join(
        f'<span class="chip" data-outcome="{k}"><span class="dot" style="background:{c}"></span>'
        f'{sym} {name}<span class="cnt">{acc.get(k,0)}</span></span>'
        for k, (sym, name, c, _blurb) in OUTCOMES.items())
    oc_js = "{" + ",".join(
        f'"{k}":{{"s":"{sym}","c":"{c}"}}' for k, (sym, _n, c, _b) in OUTCOMES.items()) + "}"
    js = (_JS.replace("__DATA__", json.dumps(model))
             .replace("__OC__", oc_js))
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>Scenario decision tree</title><style>{_CSS}</style></head><body>"
        "<header><h1>Scenario decision tree</h1>"
        '<p class="sub">Vertical pipeline spine (STAGE 0 → 5, top to bottom); each stage '
        "fans its decision tree to the right. Click a box to expand/collapse · drag to pan · "
        f"scroll to zoom · {total} tagged outcomes · self-contained &amp; offline.</p>"
        '<div class="bar"><div class="legend">' + legend + "</div>"
        '<div class="btns">'
        '<button class="btn" id="expand">Expand all</button>'
        '<button class="btn" id="collapse">Collapse all</button>'
        '<button class="btn" id="fit">Fit</button>'
        "</div></div></header>"
        '<svg id="stage"><g id="vp"></g></svg>'
        '<div class="hint">click a box to fan it out · legend chip to spotlight an outcome</div>'
        f"<script>{js}</script></body></html>"
    )


if __name__ == "__main__":
    docs = Path(__file__).resolve().parents[1] / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    model = to_model(TREE, [0])
    (docs / "scenario_decision_tree.json").write_text(json.dumps(model, indent=2, ensure_ascii=False))
    (docs / "scenario_decision_tree.html").write_text(render_html(model))
    print(f"wrote {docs/'scenario_decision_tree.json'}")
    print(f"wrote {docs/'scenario_decision_tree.html'}")
