"""
run_dashboard_html — the Layer-2 WORKFLOW run dashboard as a self-contained HTML
page, rendered PURELY from the verified WorkflowSpec (+ optionally the frozen env
record it pins).

This is the sibling of env_report_html. The split mirrors the two-layer spine:

  • Layer 1 — the ENVIRONMENT (env_report_html.render_env_report_html): written
    ONCE at freeze, immutable, content-addressed. Asserts build-locus honesty only
    (BUILT / VALIDATED_IN_IMAGE / POLICY_CLEAN). Never claims cluster-worthiness.

  • Layer 2 — the WORKFLOW (this module): written per seal_workflow. Answers the
    question the seal exists to answer — "does this actually run, here, without me
    grinding through it by hand?" — with the validated evidence, grouped by the
    compute locus it ran on (host / local container / cluster). The how-to guide is
    a DISTINCT panel rendered from the same verified usage block (no markdown).

Accretion is per-workflow-per-digest. A workflow's dashboard accretes loci as the
SAME env (pinned by digest) is validated on more compute resources. The moment the
env is REBUILT (new digest), a step whose evidence ran against the old digest is
STALE and rendered as such — it is not shown green beside evidence from the new
env. This is the one honesty rule the accretion imposes: run-evidence accretes onto
one workflow at one digest; it never accretes across a digest change as if the
env never changed. The `validated_in_shipped_image` badge (digest-matched at seal)
already enforces the hard claim; this renderer makes the digest visible.

Honesty guarantees mirror the env report: PURE over the spec, every value ESCAPED,
DETERMINISTIC (no clock read — only the spec's own captured `created_at`). Shares
env_report_html's `_CSS` + header banner so the two pages are one visual family.
One public fn: render_run_dashboard_html.
"""

from __future__ import annotations

from typing import Optional

from agent.models.core_data import usage_commands
from agent.skills.env_report_html import (
    _badge, _close_page, _e, _empty, _header_banner, _kv_table, _open_page,
)


# ---------------------------------------------------------------------------
# Locus classification — where a step's evidence came from.
# ---------------------------------------------------------------------------

def _run_locus(step: dict) -> str:
    if (step.get("validation_locus") == "cluster"
            or (step.get("resource_usage") or {}).get("locus") == "cluster"):
        return "cluster"
    if step.get("ran_in_container"):
        return "local container"
    return "host"


# Ordered so the strongest evidence (native cluster) leads the page.
_LOCUS_ORDER = ["cluster", "local container", "host"]


def _step_digest(step: dict) -> Optional[str]:
    """The env digest THIS step's evidence ran against, if recorded.

    Local-container steps stamp `container_image_digest`. Cluster steps carry
    the on-cluster `.sif` fingerprint (`cluster_sif_sha256`) plus a
    `cluster_image_digest_match` bool asserting it matches the frozen env; the
    digest itself is the frozen env's, so we key cluster staleness off the match
    flag rather than a raw digest compare (see _digest_state)."""
    return step.get("container_image_digest")


def _digest_state(step: dict, primary_digest: Optional[str]) -> tuple[bool, str]:
    """(is_current, human note). `is_current` is False when the step's evidence
    demonstrably ran against a DIFFERENT env than the one this workflow pins —
    the stale case a rebuild introduces. Unknown/unrecorded → treated as current
    (we don't fabricate a mismatch we can't prove)."""
    locus = _run_locus(step)
    if locus == "cluster":
        # The cluster C2 round-trip already compared the .sif's baked source
        # digest to the frozen env. Trust its verdict when present.
        if step.get("cluster_image_digest_match") is False:
            return False, "ran against a .sif whose digest does NOT match the pinned env"
        return True, ""
    d = _step_digest(step)
    if d and primary_digest and d != primary_digest:
        return False, f"ran against a different env image ({_e(d[:19])}…)"
    return True, ""


# ---------------------------------------------------------------------------
# Per-step evidence.
# ---------------------------------------------------------------------------

def _render_cluster_context(step: dict) -> str:
    rows: list[tuple[str, str]] = []
    job, node = step.get("cluster_job_id"), step.get("cluster_node")
    if job:
        rows.append(("SLURM job", f"{_e(job)} on {_e(node or '?')}"))
    sha = step.get("cluster_sif_sha256")
    if sha:
        tag = ""
        if step.get("cluster_image_verified"):
            tag = " — ✓ verified on cluster"
            if step.get("cluster_image_digest_match") is True:
                tag += "; digest matches the frozen env"
        rows.append((".sif sha256", f'<code>{_e(sha)}</code>{_e(tag)}'))
    mods = [m for m in (step.get("cluster_apptainer_module"),
                        step.get("cluster_nextflow_module")) if m]
    if mods:
        rows.append(("Modules", ", ".join(f'<code>{_e(m)}</code>' for m in mods)))
    sc = step.get("cluster_slurm") or {}
    if isinstance(sc, dict) and sc:
        rows.append(("SLURM placement",
                     ", ".join(f"{_e(k)}={_e(v)}" for k, v in sc.items())))
    return _kv_table(rows) if rows else ""


def _render_run_step(step: dict, primary_digest: Optional[str]) -> str:
    P: list[str] = []
    is_current, stale_note = _digest_state(step, primary_digest)
    title = f"Step {_e(step.get('step', '?'))}"
    if step.get("tool"):
        title += f" — {_e(step['tool'])}"
    if not is_current:
        title += (f' <span class="stale">⚠ stale — {_e(stale_note)}; '
                  're-run in the current env before trusting this</span>')
    P.append(f'<p class="note"><b>{title}</b></p>')
    cmd = (step.get("command") or "").strip()
    if cmd:
        P.append(f"<pre>{_e(cmd)}</pre>")
    val = step.get("validation") or {}
    if isinstance(val, dict) and val:
        rows = []
        for fn, v in val.items():
            passed = v.get("passed") if isinstance(v, dict) else None
            method = (v or {}).get("validation_method") or (v or {}).get("method") or ""
            rows.append(f"<tr><td>{_e(fn)}</td><td>{_badge(passed)}</td>"
                        f"<td>{_e(method)}</td></tr>")
        P.append('<div class="tbl-wrap"><table>'
                 '<tr><th>Output</th><th>Validated</th><th>Check</th></tr>'
                 + "".join(rows) + "</table></div>")
    ru = step.get("resource_usage") or {}
    if isinstance(ru, dict) and ru:
        P.append('<p class="note">resources: '
                 f'wall {_e(ru.get("wall_seconds"))}s · '
                 f'peak RSS {_e(ru.get("peak_rss_mb"))} MB · '
                 f'CPU {_e(ru.get("max_cpu_percent"))}% · '
                 f'locus {_e(ru.get("locus", "?"))}</p>')
    if _run_locus(step) == "cluster":
        P.append(_render_cluster_context(step))
    return "".join(P)


def _render_locus_group(locus: str, steps: list[dict], primary_digest: Optional[str]) -> str:
    P = ['<div class="run-card">']
    any_stale = any(not _digest_state(s, primary_digest)[0] for s in steps)
    if any_stale:
        badge = '<span class="pill na">stale — env rebuilt since</span>'
    else:
        badge = '<span class="pill ok">✓ validated here</span>'
    P.append(f'<div class="run-title">{_e(locus)} {badge}'
             f'<span class="note" style="margin-left:auto">{len(steps)} step(s)</span></div>')
    for s in steps:
        P.append(_render_run_step(s, primary_digest))
    P.append("</div>")
    return "".join(P)


def _render_validated_evidence(spec: dict, primary_digest: Optional[str]) -> str:
    steps = [s for s in (spec.get("pipeline_steps") or []) if isinstance(s, dict)]
    by_locus: dict[str, list[dict]] = {}
    for s in steps:
        by_locus.setdefault(_run_locus(s), []).append(s)
    P = ['<section class="bx">']
    P.append('<h2>Does it run? — validated evidence '
             '<span class="note">every command below was executed and its outputs '
             'type-validated; grouped by the compute resource it ran on</span></h2>')
    P.append('<div class="bx-body">')
    if not steps:
        P.append(_empty("(no validated steps recorded)"))
    else:
        for locus in _LOCUS_ORDER:
            if locus in by_locus:
                P.append(f'<h3 class="sub">{_e(locus)}</h3>')
                P.append(_render_locus_group(locus, by_locus[locus], primary_digest))
        # any locus not in the known order (future-proof)
        for locus in sorted(set(by_locus) - set(_LOCUS_ORDER)):
            P.append(f'<h3 class="sub">{_e(locus)}</h3>')
            P.append(_render_locus_group(locus, by_locus[locus], primary_digest))
    P.append("</div></section>")
    return "".join(P)


# ---------------------------------------------------------------------------
# How-to — the DISTINCT panel (the auto-generated user guide, no markdown).
# ---------------------------------------------------------------------------

def _usage_status(spec: dict) -> str:
    """The I4 self-test state — "verified" | "failed" | "not_attempted" | "".

    THREE STATES, not a bool. `usage_verified: False` conflates "tested and it failed"
    with "never tested" — and since seal REFUSES the former, False on disk always meant
    the latter, while the page rendered it as a verdict. `usage_verification` carries
    the truth + the reason; the bool is the fallback for specs sealed before it existed.

    One derivation, read by every panel. The head table used to re-derive it as the raw
    bool, so the same page said "Usage self-tested: False" above the fold and
    "not attempted — <reason>" below it. Two answers to one question is the bug this
    whole audit is about."""
    uv = spec.get("usage_verification") or {}
    return uv.get("status") or ("verified" if spec.get("usage_verified") else "")


#: How each I4 state reads in a one-line summary cell.
_USAGE_LABEL = {
    "verified":      "yes",
    "failed":        "NO — self-test failed",
    "not_attempted": "not attempted",
    "":              "not attempted",
}


def _render_howto(spec: dict) -> str:
    usage = spec.get("usage")
    P = ['<section class="bx">']
    uv = spec.get("usage_verification") or {}
    status = _usage_status(spec)
    verified = status == "verified"
    locus = uv.get("locus") or ""
    if verified:
        where = " in the shipped image" if locus == "image" else ""
        tag = f'<span class="pill ok">✓ self-tested{_e(where)}</span>'
    elif status == "not_attempted":
        tag = '<span class="pill na">not self-tested — not attempted</span>'
    else:
        tag = '<span class="pill na">not self-tested</span>'
    # The subtitle used to assert "self-tested against every declared input shape (I4)"
    # UNCONDITIONALLY — on every dashboard, including one with no usage block at all, and
    # directly above a pill reading "not self-tested". Two contradictory claims in one
    # panel. It now describes what this page actually knows.
    sub = ("the runnable command, self-tested against every declared input shape (I4)"
           if verified else
           "the runnable command as authored — see below for whether it was self-tested")
    P.append(f'<h2>How to run it <span class="note">{_e(sub)}</span></h2>')
    P.append('<div class="bx-body">')
    # ONE reading of command_template (str or list[str]) — core_data.usage_commands.
    cmds = usage_commands(usage) if isinstance(usage, dict) else []
    if not cmds:
        P.append(_empty("(no usage.command_template on this workflow — nothing to run)"))
        P.append("</div></section>")
        return "".join(P)
    label = "Command" if len(cmds) == 1 else f"Commands ({len(cmds)}, run in order)"
    P.append(f'<div class="how"><div class="run-title">{label} {tag}</div>')
    if status == "not_attempted" and uv.get("reason"):
        # WHY it wasn't tested. Without this the reader is left to assume the how-to is
        # suspect, when the real reason is usually about our runner (inputs live on the
        # cluster; no local image), not about the command.
        P.append('<p class="note"><b>Not self-tested here:</b> '
                 f'{_e(uv["reason"])} — this is missing evidence about the command, '
                 'not evidence against it.</p>')
    if usage.get("description"):
        P.append(f'<p class="note">{_e(usage["description"])}</p>')
    # Numbered when there's more than one, because the ORDER is part of the contract:
    # the self-test runs them in sequence sharing a working dir, so a reader who runs
    # them out of order is not running what was verified.
    if len(cmds) == 1:
        P.append(f'<pre>{_e(cmds[0])}</pre>')
    else:
        P.append("<pre>" + "\n".join(f"{i}. {_e(c)}" for i, c in enumerate(cmds, 1)) + "</pre>")
    ins = [i for i in (usage.get("inputs") or []) if isinstance(i, dict)]
    if ins:
        P.append('<p class="note"><b>Inputs</b></p>')
        rows = "".join(
            f'<tr><td>{_e(i.get("name"))}</td><td>{_e(i.get("format",""))}</td>'
            f'<td>{_e(i.get("description",""))}</td></tr>' for i in ins)
        P.append('<div class="tbl-wrap"><table>'
                 '<tr><th>Placeholder</th><th>Format</th><th>Description</th></tr>'
                 + rows + '</table></div>')
    outs = [o for o in (usage.get("outputs") or []) if isinstance(o, dict)]
    if outs:
        P.append('<p class="note"><b>Outputs</b></p>')
        rows = "".join(
            f'<tr><td>{_e(o.get("name"))}</td>'
            f'<td>{_e(", ".join(o.get("files", []) or []))}</td>'
            f'<td>{_e(o.get("type",""))}</td></tr>' for o in outs)
        P.append('<div class="tbl-wrap"><table>'
                 '<tr><th>Name</th><th>Files</th><th>Type</th></tr>'
                 + rows + '</table></div>')
    trials = [t for t in (usage.get("trials") or []) if isinstance(t, dict)]
    if trials:
        # Guarded by `verified`. This line used to render unconditionally, so a dashboard
        # could say "not self-tested" and "Self-tested against 1 declared input shape(s)"
        # in the same panel — cluster_refdata_validation did exactly that.
        lead = (f'Self-tested against {len(trials)} declared input shape(s):' if verified
                else f'{len(trials)} declared input shape(s) (NOT self-tested — see above):')
        P.append(f'<p class="note">{lead}</p><ul class="foot">')
        for t in trials:
            P.append(f'<li><b>{_e(t.get("name","trial"))}</b>'
                     + (f' — {_e(t.get("description"))}' if t.get("description") else "")
                     + '</li>')
        P.append("</ul>")
    P.append("</div></div></section>")
    return "".join(P)


# ---------------------------------------------------------------------------
# Environment panel — what this workflow is pinned to, + a link to its ENV.html.
# ---------------------------------------------------------------------------

def _render_env_panel(spec: dict, env_record: Optional[dict]) -> str:
    P = ['<section class="bx">']
    P.append('<h2>Environment '
             '<span class="note">pinned by digest — the Layer-1 solved component '
             'this workflow consumes</span></h2>')
    P.append('<div class="bx-body">')
    env_name = (env_record or {}).get("name") or ""
    rows: list[tuple[str, str]] = [
        ("Image", f'<code>{_e(spec.get("env_image","—"))}</code>' if spec.get("env_image") else "—"),
        ("Content digest",
         f'<code>{_e(spec.get("env_content_digest","—"))}</code>'
         if spec.get("env_content_digest") else "—"),
        ("Request key",
         f'<code>{_e(spec.get("env_request_key","—"))}</code>'
         if spec.get("env_request_key") else "—"),
    ]
    if env_name:
        rows.append(("Env report",
                     f'<a href="{_e(env_name)}.ENV.html"><code>{_e(env_name)}.ENV.html</code></a>'
                     '<span class="note"> — the immutable Layer-1 build honesty report</span>'))
    hpc = spec.get("env_hpc_delivery") or {}
    if hpc.get("get_image"):
        rows.append(("Get the image (HPC)", f"<pre>{_e(hpc['get_image'])}</pre>"))
    P.append(_kv_table(rows))
    # Multi-env chaining: a workflow may chain steps across several frozen envs.
    envs = [e for e in (spec.get("envs") or []) if isinstance(e, dict)]
    if len(envs) > 1:
        P.append('<h3 class="sub">Chained envs (multi-env workflow)</h3>')
        er = "".join(
            f'<tr><td>{_e(e.get("request_key",""))}</td>'
            f'<td><code>{_e(e.get("image_digest",""))}</code></td></tr>' for e in envs)
        P.append('<div class="tbl-wrap"><table>'
                 '<tr><th>Request key</th><th>Image digest</th></tr>' + er + '</table></div>')
    P.append("</div></section>")
    return "".join(P)


# ---------------------------------------------------------------------------
# External inputs — reproducibility sources the run consumed.
# ---------------------------------------------------------------------------

def _render_inputs(spec: dict) -> str:
    rdbs = [d for d in (spec.get("reference_databases") or []) if isinstance(d, dict)]
    arts = [a for a in (spec.get("authored_artifacts") or []) if isinstance(a, dict)]
    td = spec.get("test_data") if isinstance(spec.get("test_data"), dict) else None
    if not (rdbs or arts or td):
        return ""
    P = ['<section class="bx">']
    P.append('<h2>Inputs &amp; external sources '
             '<span class="note">what the validated run consumed (I8 provenance)</span></h2>')
    P.append('<div class="bx-body">')
    if td:
        keys = [k for k in ("r1", "r2", "vcf", "bam", "reference_fasta", "file", "core_data_dir")
                if td.get(k)]
        if keys:
            P.append('<h3 class="sub">Test data</h3>')
            P.append(_kv_table([(k, f'<code>{_e(td[k])}</code>') for k in keys]))
    if rdbs:
        P.append('<h3 class="sub">Reference databases</h3>')
        rows = "".join(
            f'<tr><td>{_e(d.get("name",""))}</td>'
            f'<td><code>{_e((d.get("sha256") or "")[:19])}{"…" if d.get("sha256") else "—"}</code></td>'
            f'<td>{_e(d.get("size_bytes",""))}</td></tr>' for d in rdbs)
        P.append('<div class="tbl-wrap"><table>'
                 '<tr><th>Name</th><th>sha256</th><th>Bytes</th></tr>' + rows + '</table></div>')
    if arts:
        P.append('<h3 class="sub">Authored artifacts</h3>')
        rows = "".join(
            f'<tr><td>{_e(a.get("role",""))}</td><td><code>{_e(a.get("path",""))}</code></td>'
            f'<td><code>{_e((a.get("sha256") or "")[:19])}…</code></td></tr>' for a in arts)
        P.append('<div class="tbl-wrap"><table>'
                 '<tr><th>Role</th><th>Path</th><th>sha256</th></tr>' + rows + '</table></div>')
    P.append("</div></section>")
    return "".join(P)


# ---------------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------------

def render_run_dashboard_html(spec: dict, env_record: Optional[dict] = None) -> str:
    """Render a sealed WorkflowSpec as a self-contained Layer-2 run dashboard.

    `spec` is the machine-verified WorkflowSpec (the source of every value here).
    `env_record` is the frozen env record it pins (optional) — used only to link
    the ENV.html and to know the primary env digest for stale-detection. Same CSS
    + header banner as the env report, so the two artifacts are one family."""
    s = spec or {}
    name = s.get("workflow_name") or "workflow"
    steps = [st for st in (s.get("pipeline_steps") or []) if isinstance(st, dict)]
    validated = [st for st in steps
                 if st.get("validation") or st.get("validation_status") == "passed"]
    loci = [locus for locus in _LOCUS_ORDER
            if any(_run_locus(st) == locus for st in steps)]
    # Primary digest for stale-detection MUST be a bare image digest (sha256:…),
    # the same shape a step stamps as container_image_digest. env_image is a
    # repo@sha256:… REF (different shape) — never use it here or every local step
    # would false-flag stale. Absent a real digest we compare nothing (unknown →
    # current; we don't fabricate a mismatch we can't prove).
    primary_digest = (env_record or {}).get("image_digest") or None

    shipped = bool(s.get("validated_in_shipped_image"))
    if shipped:
        pill = '<span class="pill ok">✓ validated in shipped image</span>'
    elif steps:
        pill = '<span class="pill na">not shipped-image verified</span>'
    else:
        pill = '<span class="pill na">no runs recorded</span>'

    head_rows = [
        ("Sealed", _e(s.get("created_at", "—"))),
        ("Validated on", ", ".join(_e(x) for x in loci) if loci else "—"),
        ("Steps validated", f"{len(validated)}/{len(steps)}"),
        ("Env image", f'<code>{_e(s.get("env_image","—"))}</code>' if s.get("env_image") else "—"),
        ("Env content digest",
         f'<code>{_e(s.get("env_content_digest","—"))}</code>' if s.get("env_content_digest") else "—"),
        ("Usage self-tested", _e(_USAGE_LABEL.get(_usage_status(s), _usage_status(s)))),
    ]
    if s.get("description"):
        head_rows.insert(0, ("Workflow", _e(s["description"])))

    P: list[str] = []
    P.append(_open_page(f"Workflow run report — {name}"))
    P.append(_header_banner(f"Workflow run report — {_e(name)}", pill, head_rows))
    P.append(_render_validated_evidence(s, primary_digest))
    P.append(_render_howto(s))
    P.append(_render_env_panel(s, env_record))
    inputs = _render_inputs(s)
    if inputs:
        P.append(inputs)
    # HONEST PROVENANCE. This used to claim "no field on this page was authored by the
    # agent", which is false and was false when written: the description, the usage
    # description, and the command_template are all rendered here and all sit in
    # patch_pipeline's agent-authored allowlist (CLAUDE.md). A page that overstates its own
    # purity is the same defect class it exists to prevent — so it now says which parts are
    # machine-observed and which are authored, and lets the reader weigh them differently.
    P.append('<p class="gen">Generated deterministically from the sealed WorkflowSpec. '
             'The <b>evidence</b> — commands run, exit codes, outputs, validations, '
             'digests, resource usage — is machine-observed and cannot be authored by the '
             'agent. The <b>prose</b> — this workflow\'s description, the how-to '
             'description, and the command template itself — IS agent-authored (it is in '
             'patch_pipeline\'s allowlist); the how-to\'s self-test status above says '
             'whether that command was actually executed. The env report (Layer 1) is a '
             'separate, immutable page.</p>')
    P.append(_close_page())
    return "\n".join(P)
