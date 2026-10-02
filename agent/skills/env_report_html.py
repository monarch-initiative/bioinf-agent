"""
env_report_html — the Layer-1 env report as a self-contained HTML page, rendered
PURELY from the verified freeze record.

Clean tables, no decorative tiles. SAME sections + SAME columns for every install
(adopt and build), so two reports compare cell-for-cell. Cyber dark
palette (black, cyan, yellow) — meant to give each section visual weight without
adding any unverifiable content.

Honesty guarantees, made structural:

  • PURE over the record — render_env_report_html(record) reads ONLY the freeze
    record. No field is agent-authored.
  • ESCAPED — every value is HTML-escaped; a package name / command / digest can
    never inject markup.
  • DETERMINISTIC — no clock is read here (the only time shown is the record's own
    captured `created_at`); stable ordering. Same record → same bytes.
  • MODE-HONEST — a container-native BUILD shows per-tool in-image evidence
    (validated == shipped). An ADOPTED biocontainer keeps the same columns but its
    "Validated in image" cell is a "trusted by digest" badge — the page never
    claims a validation it did not run.
  • VERIFIED vs DECLARED — runtime-verified facts and submitter-DECLARED policy
    (license-gating, accelerator) live in separate, labelled sections.

Self-contained: inline CSS, zero external resources, no JS. Companion-artifact
links resolve relative to env_reports/ where the file lives. One public fn:
render_env_report_html.
"""

from __future__ import annotations

from html import escape
from typing import Any, Optional

from agent.models.core_data import record_is_gated as _record_is_gated

from agent.models.core_data import shipped_binaries as _shipped_binaries
from agent.models.core_data import tool_identities as _tool_identities
from agent.skills.env_honesty import (
    ESTABLISHED as _ESTABLISHED,
    FAILED as _FAILED,
    GUARANTEE_NOT_APPLICABLE as _G_NA,
    NOT_ESTABLISHED as _NOT_ESTABLISHED,
    PARTLY_ESTABLISHED as _PARTLY,
)
from agent.skills.env_report_helpers import (
    _install_anchor, _install_method, _is_sha, _locus_line, _pkg_index,
    _resolved_version, _verif_index, requested_versions as _shared_req_versions,
    version_divergences as _version_divergences,
)

#: How each Layer-1 guarantee verdict is shown. Module-level and EXHAUSTIVE over
#: env_honesty.GUARANTEE_VERDICTS — a test asserts the two sets are equal, so a
#: sixth verdict state cannot be added upstream and silently render as raw text
#: (or, worse, fall back to a badge meaning something else) on the page a human
#: reads. The five states exist because "not established" and "not applicable"
#: mean opposite things to that reader; a map that quietly merged them would undo
#: the distinction this whole section was rebuilt to preserve.
_VERDICT_BADGE = {
    _ESTABLISHED: '<span class="ok">established</span>',
    _PARTLY: '<span class="warn">partly established</span>',
    _NOT_ESTABLISHED: '<span class="warn">not established</span>',
    _G_NA: '<span class="muted">not applicable</span>',
    _FAILED: '<span class="pill bad">FAILED</span>',
}

_CSS = """
/* PALETTE — two themes, one structure. EVERY colour flows through these variables;
   all geometry/layout below is theme-agnostic. Cyber is the default AND the
   no-JS fallback (:root); the in-page toggle sets :root[data-theme] and the light/
   professional palette overrides the same names. Rule for edits: keep NO raw hex
   below the :root blocks — a stray literal is a colour that silently won't switch. */
:root, :root[data-theme="cyber"]{
  --bg:#0a0c14;--surface:#13151f;--surface-2:#1a1d29;--border:#262a3a;
  --cyan:#22e3ee;--cyan-soft:rgba(34,227,238,.16);--accent-wash:rgba(34,227,238,.05);
  --yellow:#fff200;--yellow-soft:rgba(255,242,0,.18);
  --title:#fff200;--link:#fff200;--warn:#fff200;
  --ink:#e6e9f0;--muted:#8e98ad;
  --ok:#3ce086;--ok-bg:rgba(60,224,134,.14);
  --bad:#ff4b6e;--bad-bg:rgba(255,75,110,.14);
  --code-bg:#0e1019;--pre-bg:#0c0e16;--on-accent:#000;--on-bad:#fff;
  --head-bg:linear-gradient(180deg,var(--accent-wash),transparent 80%);--head-shadow:none;
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
:root[data-theme="light"]{
  /* professional / print-friendly — one blue primary, deep navy for the
     secondary-accent role (list markers, the adopt pill, picture highlights),
     ink-coloured titles, standard blue links. Amber is reserved for the WARN
     role so a caveat still reads as a caveat. Same structure, quiet skin. */
  --bg:#f4f6f9;--surface:#ffffff;--surface-2:#eef2f7;--border:#d5dce6;
  --cyan:#0b5fb4;--cyan-soft:rgba(11,95,180,.10);--accent-wash:rgba(11,95,180,.05);
  --yellow:#1f3a5f;--yellow-soft:rgba(31,58,95,.10);
  --title:#14213d;--link:#0b5fb4;--warn:#9a6700;
  --ink:#1a2233;--muted:#5b6675;
  --ok:#1a7f4b;--ok-bg:rgba(26,127,75,.12);
  --bad:#c0304a;--bad-bg:rgba(192,48,74,.10);
  --code-bg:#eef2f7;--pre-bg:#f4f7fb;--on-accent:#ffffff;--on-bad:#ffffff;
  --head-bg:var(--surface);--head-shadow:0 1px 2px rgba(20,33,61,.06),0 8px 24px -12px rgba(20,33,61,.18);
}
/* THEME TOGGLE — presentation only (authors no content); hidden in print. */
.theme-toggle{position:fixed;top:14px;right:16px;z-index:10;background:var(--surface-2);
color:var(--muted);border:1px solid var(--border);font:600 11px/1 var(--mono);
letter-spacing:.10em;text-transform:uppercase;padding:8px 12px;cursor:pointer;border-radius:2px}
.theme-toggle:hover{color:var(--cyan);border-color:var(--cyan)}
@media print{.theme-toggle{display:none}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
font:14.5px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:30px 22px 64px;background:transparent}
/* HEADER BANNER. The cyan frame is a 2px ring drawn by ::before, and the two
   gaps in it are part of the ring's own clip-path — a notch cut down from the
   top edge under the TL yellow diagonal and up from the bottom edge under the
   BR one, each 30px wide at the edge with sides sloped to the yellow's own
   slope (dx/dy = 30/14). The notch is cut 8px deep into a 2px ring: the extra
   depth only removes transparent interior, so no device-pixel rounding at any
   zoom can leave a hairline across the gap (a page-coloured mask laid over the
   border could, and did). At the TL and BR corners (only), a solid yellow
   L-block sits OUTSIDE the cyan with a 6px gap, extending half the panel along
   both edges and tapering off on a diagonal at the far end of each arm. */
.head{position:relative;padding:24px 28px 8px;margin:24px 24px 44px;
background:var(--head-bg);box-shadow:var(--head-shadow)}
.head::before{content:"";position:absolute;inset:0;border:2px solid var(--cyan);pointer-events:none;
clip-path:polygon(0 0,calc(50% - 30px) 0,calc(50% - 47px) 8px,calc(50% - 17px) 8px,50% 0,100% 0,
100% 100%,calc(50% + 30px) 100%,calc(50% + 47px) calc(100% - 8px),calc(50% + 17px) calc(100% - 8px),50% 100%,0 100%)}
.head .cr{position:absolute;background:var(--yellow);pointer-events:none;z-index:2}
/* TL: block at top:-20 left:-20 → L's outer edge is 20px outside the cyan's
   outer edge; arm thickness 14px, so a 6px gap between the L and the cyan.
   50% extent along each edge. */
.head .cr-tl{top:-20px;left:-20px;
width:calc(50% + 20px);height:calc(50% + 20px);
clip-path:polygon(0 0,100% 0,calc(100% - 30px) 14px,14px 14px,14px calc(100% - 30px),0 100%)}
/* BR: mirror of TL (rotate 180°). */
.head .cr-br{bottom:-20px;right:-20px;
width:calc(50% + 20px);height:calc(50% + 20px);
clip-path:polygon(100% 0,100% 100%,0 100%,30px calc(100% - 14px),calc(100% - 14px) calc(100% - 14px),calc(100% - 14px) 30px)}
/* LIGHT: a plain card. No corner blocks, no notches — a 1px border with a
   3px primary bar along the top, on a white surface with a soft shadow. */
:root[data-theme="light"] .head{margin:12px 0 36px}
:root[data-theme="light"] .head::before{clip-path:none;border:1px solid var(--border);
border-top:3px solid var(--cyan)}
:root[data-theme="light"] .head .cr{display:none}
/* SECTION PANELS — each remaining section is a bordered card (no yellow accents) */
section.bx{border:1px solid var(--border);margin:22px 0;background:transparent}
section.bx h2{margin:0;padding:14px 22px 11px;border-bottom:none}
section.bx .bx-body{padding:14px 22px 18px}
section.bx .bx-body > *:first-child{margin-top:0}
section.bx .bx-body > *:last-child{margin-bottom:0}
/* FOLDING SECTION — the heading is the disclosure; one arrow, on the heading. */
section.bx > details.fold{margin:0;border:none;background:transparent;padding:0}
section.bx > details.fold > summary{display:block;padding:0;font:inherit;color:inherit;
list-style:none}
section.bx > details.fold > summary::before{content:none}
section.bx > details.fold > summary > h2::before{content:"▸";display:inline-block;width:16px;
color:var(--cyan);font-size:12px}
section.bx > details.fold[open] > summary > h2::before{content:"▾"}
section.bx > details.fold > summary:hover > h2{color:var(--ink)}
/* sub-heading inside a section (e.g. "Install commands" under Along for the ride) */
h3.sub{font-size:11.5px;text-transform:uppercase;letter-spacing:.12em;color:var(--muted);
margin:22px 0 8px;font-weight:600}
.head h1{font-size:24px;font-weight:800;color:var(--title);margin:0 0 14px;letter-spacing:.01em}
table.head-kv{width:100%;border:none;background:transparent;font-size:12.5px;
border-collapse:collapse}
table.head-kv td{padding:6px 0;border-bottom:1px solid var(--border);vertical-align:top;line-height:1.5}
table.head-kv tr:last-child td{border-bottom:none}
table.head-kv td.k{background:transparent;border:none;width:185px;color:var(--muted);
font-weight:500;padding-right:18px}
/* SECTION HEADINGS — cyan, uppercase, just an underline (no yellow left bar) */
h2{font-size:12.5px;font-weight:700;letter-spacing:.16em;text-transform:uppercase;
color:var(--cyan);margin:34px 0 12px;padding:0 0 9px 0;border-bottom:1px solid var(--cyan)}
h2 .note{color:var(--muted);font-weight:400;letter-spacing:0;text-transform:none;font-size:12px;margin-left:8px}
a{color:var(--link);text-decoration:none;border-bottom:1px dashed transparent}
a:hover{border-bottom-color:var(--link)}
code{background:var(--code-bg);color:var(--cyan);padding:1px 6px;border:1px solid var(--border);
border-radius:2px;font:12.5px/1.4 var(--mono);word-break:break-all}
pre{background:var(--pre-bg);color:var(--ink);padding:10px 12px;margin:4px 0;border:1px solid var(--border);
border-left:3px solid var(--cyan);border-radius:0;overflow-x:auto;
font:12px/1.5 var(--mono);white-space:pre-wrap;word-break:break-word}
details{margin:6px 0;border:1px solid var(--border);border-left:3px solid var(--cyan);
background:var(--surface)}
details>summary{cursor:pointer;padding:8px 12px;font:12.5px/1.4 var(--mono);
color:var(--cyan);list-style:none}
details>pre{margin:0;border:none;border-top:1px solid var(--border)}
.tbl-wrap{overflow-x:auto;margin:4px 0}
table{width:100%;border-collapse:collapse;background:var(--surface);
border:1px solid var(--border);border-top:2px solid var(--cyan);font-size:13.5px}
th,td{text-align:left;padding:10px 14px;border-bottom:1px solid var(--border);vertical-align:top;line-height:1.5}
tr:last-child td{border-bottom:none}
tr.id-row td{padding:2px 14px 10px 32px;font-size:12px;border-bottom:1px solid var(--border)}
tr.id-row td q{font-style:italic}
/* EVIDENCE CELL — the words first, the literal command one click away. */
.ev{display:block;margin:6px 0 0;border:none;background:transparent;padding:0}
.ev>summary{display:inline-block;padding:0;font:11.5px/1.4 var(--mono);color:var(--muted);
letter-spacing:.04em}
.ev>summary:hover{color:var(--cyan)}
.ev>summary::before{color:var(--muted)}
.ev>pre{margin:6px 0 0;font-size:11.5px}
th{background:var(--surface-2);color:var(--cyan);font-size:10.5px;font-weight:700;
letter-spacing:.12em;text-transform:uppercase;border-bottom:1px solid var(--border)}
/* LEFTMOST COLUMN — same muted color as the build-details key column, in EVERY table */
table:not(.head-kv) td:first-child{color:var(--muted);font-weight:500}
td.k{color:var(--muted);width:225px;background:var(--surface-2);font-weight:500;
border-right:1px solid var(--border)}
.pill{display:inline-block;font-size:11px;font-weight:800;padding:3px 12px;
vertical-align:middle;margin-left:10px;letter-spacing:.14em;text-transform:uppercase;border-radius:0}
.pill.ok{background:var(--cyan);color:var(--on-accent)}
.pill.adopt{background:var(--yellow);color:var(--on-accent)}
.pill.bad{background:var(--bad);color:var(--on-bad)}
.pill.na{background:var(--surface-2);color:var(--muted);border:1px solid var(--border)}
.badge{display:inline-block;font-size:11.5px;font-weight:700;padding:1px 9px;border-radius:0;
margin-right:4px}
.badge.ok{background:var(--ok-bg);color:var(--ok);border:1px solid var(--ok)}
.badge.bad{background:var(--bad-bg);color:var(--bad);border:1px solid var(--bad)}
.badge.na{background:var(--surface-2);color:var(--muted);border:1px solid var(--border)}
/* The third badge state, for a fact that is neither a pass nor a failure —
   "declared none, but the image carries cuda 12.8". Without its own compound rule it
   would inherit `.badge` box styling with `.warn`'s text colour and no border, which
   reads as a styling slip rather than a deliberate third state. Same lesson as the
   coverage-state note below: a state that renders like another state IS that state,
   to the only reader who matters. */
.badge.warn{background:var(--surface-2);color:var(--warn);border:1px solid var(--warn)}
.note{color:var(--muted);font-size:12.5px;margin:6px 0}
/* A caveat that must not read as small print. The RUN dashboard uses it for
   "these resource numbers were measured under emulation; do not size #SBATCH --mem
   from them" — a correction that has to be at least as visible as the numbers it
   corrects, or it is the same defect in a lighter shade of grey. */
.warn-note{color:var(--warn);font-size:12.5px;margin:6px 0;padding:8px 12px;
background:var(--surface-2);border-left:3px solid var(--warn)}
.muted{color:var(--muted)}
/* THE CONTRACT-COVERAGE STATES. `.ok` and `.warn` are emitted as BARE spans by the
   coverage table (`<span class="ok">checked</span>` / `<span class="warn">unobserved</span>`)
   and neither had a bare selector — only the compound `.pill.ok` and `.badge.ok`, which a
   plain span matches neither of. So the one table built to separate "we checked this"
   from "NOBODY LOOKED" rendered both as identical unstyled text, while `n/a` WAS greyed
   because `.muted` happens to be defined. Measured on talos_v11: three checked and three
   unobserved rows, visually indistinguishable. Collapsing UNOBSERVED into CHECKED is
   absence rounded up into a verdict, in pixels. */
.ok{color:var(--ok);font-weight:700}
.warn{color:var(--warn);font-weight:700}
.empty{background:var(--surface);border:1px dashed var(--border);padding:13px 16px;
color:var(--muted);font-size:13px;font-style:italic;margin:4px 0}
details{margin:6px 0;background:var(--surface);border:1px solid var(--border);
padding:0 14px;border-radius:0}
summary{cursor:pointer;padding:10px 0;font-weight:600;font-size:13.5px;color:var(--ink);
list-style:none;outline:none}
summary::-webkit-details-marker{display:none}
summary::before{content:"▸ ";color:var(--cyan);margin-right:4px}
details[open] summary::before{content:"▾ "}
details table{margin:4px 0 10px}
ul.foot{padding-left:18px;margin:6px 0 0;font-size:13.5px;line-height:1.6}
ul.foot li{margin:6px 0}
ul.foot li::marker{color:var(--yellow)}
ul.foot b{color:var(--yellow);font-weight:700;letter-spacing:.04em}
.gen{color:var(--muted);font-size:11.5px;margin-top:38px;border-top:1px solid var(--border);
padding-top:14px;letter-spacing:.04em}
/* RUN CARDS — per-locus validated-run cards. Shared into the Layer-2 run
   dashboard (run_dashboard_html) via the same _CSS, so both artifacts are one
   visual family; unused by the Layer-1 env report itself. */
.run-card{border:1px solid var(--border);border-left:3px solid var(--cyan);
background:var(--surface);padding:12px 16px 14px;margin:12px 0}
.run-title{font-size:14px;font-weight:700;color:var(--ink);margin:2px 0 6px;
display:flex;align-items:center;gap:10px;flex-wrap:wrap}
.run-card .note{margin:4px 0 10px}
.run-card details{margin:10px 0 2px}
/* stale marker — a locus whose evidence ran against a DIFFERENT env digest than
   the one this workflow is headlined by (accretion is honest only per-digest). */
.stale{color:var(--warn);font-weight:600}
.how{border:1px solid var(--cyan);border-left:3px solid var(--cyan);
background:linear-gradient(180deg,var(--accent-wash),transparent 70%);
padding:14px 18px 16px;margin:12px 0}
"""


def _e(v: Any) -> str:
    return escape("" if v is None else str(v))


def _badge(passed: Optional[bool], check: str = "", tool: str = "") -> str:
    if passed is None:
        return '<span class="badge na">—</span>'
    cls, mark = ("ok", "✓") if passed else ("bad", "✗")
    c = f' <code>{_e(check)}</code>' if check else ""
    # DISCLOSURE: how deeply the evidence exercised the tool. A shallow proof (version/
    # import/help = presence only) is labelled so it can't read as a functional run — the
    # honesty lever the Talos reconstruction slipped past (imported clean, didn't RUN).
    depth = ""
    if check:
        try:
            from agent.skills.env_honesty import evidence_depth, is_shallow_evidence
            d = evidence_depth(check, tool)
            # ASK the classifier; never re-derive its answer — a hand-copied depth list
            # drifts from _SHALLOW_DEPTHS, which matters most on adopted envs, whose
            # evidence IS a presence check. `unknown` (the classifier declining to
            # guess) counts as shallow too: rendering it as a functional run would be
            # an assertion built out of a shrug.
            shallow = is_shallow_evidence(check, tool) or d == "unknown"
            depth = (f' <span class="note" title="evidence depth: {d} '
                     f'({"presence only — not a functional run" if shallow else "runs the tool"})">'
                     f'{"⚠ " if shallow else ""}{_e(d)}</span>')
        except Exception:
            depth = ""
    return f'<span class="badge {cls}">{mark}</span>{c}{depth}'


# What each evidence-depth class means to a reader. Keyed by the classifier's own
# vocabulary; an unlisted class falls back to its name so nothing renders blank.
_DEPTH_WORDS = {
    "presence":   "present in the image",
    "version":    "present and reports a version",
    "help":       "present and prints its help",
    "import":     "loads as a library",
    "functional": "ran on data",
    "unknown":    "check ran (what it exercised is unclassified)",
}


def _evidence_cell(v: dict, tool: str) -> str:
    """The Tools table's evidence cell: ✓/✗, what the check showed in words, whether
    the control experiment made the pass mean something, and the literal command
    under a disclosure. Reads depth through the classifier, never re-derives it —
    a hand-copied depth list drifts from `_SHALLOW_DEPTHS`."""
    from agent.skills.env_honesty import (
        CONTROL_DISCRIMINATING, CONTROL_UNCHECKED, CONTROL_VACUOUS,
        evidence_depth, is_shallow_evidence)
    check = v.get("check", "")
    passed = v.get("passed")
    bits: list[str] = []
    if passed:
        d = evidence_depth(check, tool)
        shallow = is_shallow_evidence(check, tool) or d == "unknown"
        words = _DEPTH_WORDS.get(d, d)
        bits.append('<span class="badge ok">✓</span> ' + _e(words))
        if shallow:
            bits.append('<span class="muted">· not exercised</span>')
    else:
        rc = v.get("rc")
        bits.append('<span class="badge bad">✗</span> failed'
                    + (f' <span class="muted">(exit {_e(rc)})</span>' if rc not in (None, "") else ""))
    control = v.get("control")
    if control == CONTROL_DISCRIMINATING:
        bits.append('<span class="muted">· fails in an image without the tool</span>')
    elif control == CONTROL_VACUOUS:
        bits.append('<span class="pill bad">passes without the tool too</span>')
    elif control == CONTROL_UNCHECKED:
        bits.append('<span class="muted">· not tried without the tool</span>')
    out = " ".join(bits)
    if check:
        out += (f'<details class="ev"><summary>command</summary>'
                f'<pre>{_e(check)}</pre></details>')
    return out


def _shallow_evidence_tools(r: dict) -> list[str]:
    """Which of this record's PASSING evidence commands only read as presence.

    ONE reading, shared by the per-tool badge above and the header summary: computed
    separately, the table can say `⚠ version` per tool while the header says
    `1/1 validated in image` flat — a page that qualifies its small print and not
    its headline. Two spellings of one question is how that happens.

    Only PASSING evidence is counted — a failed check is already refusing, and
    calling it shallow on top would answer a question nobody is asking. `unknown`
    counts as shallow here for the same reason the badge does: a classifier
    declining to guess is not a functional run.
    """
    from agent.skills.env_honesty import evidence_depth, is_shallow_evidence
    out = []
    for v in (r.get("verifications") or []):
        if not isinstance(v, dict) or not v.get("passed"):
            continue
        check, tool = v.get("check", ""), v.get("tool", "")
        if not check:
            continue
        if is_shallow_evidence(check, tool) or evidence_depth(check, tool) == "unknown":
            out.append(tool or v.get("label", "?"))
    return out


def _kv_table(rows: list[tuple[str, str]]) -> str:
    body = "".join(f'<tr><td class="k">{_e(k)}</td><td>{v}</td></tr>'
                   for k, v in rows if v != "" and v is not None)
    return f'<div class="tbl-wrap"><table>{body}</table></div>'


def _empty(msg: str) -> str:
    return f'<p class="empty">{_e(msg)}</p>'


def _accel_declared_vs_observed(r: dict, accel: dict | None, accel_type: str) -> str:
    """The accelerator row: what was DECLARED, beside what the image actually carries.

    The section this sits in is headed "submitter-declared … not a runtime-verified
    fact", but freeze reads the toolkit off the shipped image, so showing only the
    claim would hide the one part of the row that IS an observation — and a GPU claim
    is precisely the thing a reader cannot check for themselves before committing the
    allocation.

    Three states, as everywhere: observed-and-agreeing, observed-and-absent (the
    contract refuses this, so it can only appear on a record from before the check), and
    nothing-looked.

    A declared `none` still looks at the observation: a row reading `Accelerator —
    none` over an apt SBOM that lists the CUDA runtime answers this row's question —
    "do I request a GPU node for this?" — wrongly for a GPU tool. Under-claiming is
    not a contract violation (the harmful direction is claiming a GPU you do not
    have), but it is absolutely a fact a reader needs.
    """
    declared = _e(accel_type)
    if accel_type in ("", "none"):
        obs = r.get("image_accelerator")
        if not isinstance(obs, dict) or not obs.get("resolved"):
            return declared or "none"
        otype = (obs.get("type") or "").strip().lower()
        if otype in ("", "none"):
            return (f'{declared or "none"} <span class="muted">— and the shipped image '
                    f'carries no accelerator toolkit either</span>')
        over = obs.get("version") or ""
        shown = f"{_e(otype)}{(' ' + _e(over)) if over else ''}"
        return (f'{declared or "none"} <span class="badge warn">but the shipped image '
                f'carries {shown}</span> <span class="muted">({_e(obs.get("source") or "the image")})'
                f' — no GPU capability is CLAIMED for this env, so nothing here has been '
                f'checked against a driver; the toolkit is simply present</span>')
    tv = (accel or {}).get("toolkit_version") or ""
    if tv:
        declared = f"{declared} {_e(tv)}"

    obs = r.get("image_accelerator")
    if not isinstance(obs, dict) or not obs.get("resolved"):
        return (f'{declared} <span class="muted">— declared only; nothing read the '
                f'toolkit off the shipped image</span>')
    otype = (obs.get("type") or "").strip().lower()
    if otype == "none":
        return (f'{declared} <span class="badge bad">shipped image carries no '
                f'accelerator toolkit</span>')
    over = obs.get("version") or ""
    shown = f"{_e(otype)}{(' ' + _e(over)) if over else ''}"
    src = _e(obs.get("source") or "the image")
    return (f'{declared} <span class="badge ok">image carries {shown}</span> '
            f'<span class="muted">({src})</span>')


# --- Shared page shell + theme toggle -------------------------------------
# The two reports (Layer-1 env report, Layer-2 run dashboard) open/close through
# these so the theme machinery lives in ONE place. Cyber is the default; the
# toggle flips :root[data-theme] to "light" (professional/print) and persists the
# choice. Pure presentation — it authors no content field, so the reports' "no
# field authored by the agent" claim is untouched.
_THEME_INIT = (  # runs in <head> before paint → no theme flash
    '<script>try{var t=localStorage.getItem("bioinf-theme");'
    'document.documentElement.setAttribute("data-theme",t==="light"?"light":"cyber")}'
    'catch(e){document.documentElement.setAttribute("data-theme","cyber")}</script>'
)
_THEME_TOGGLE = ('<button id="__tt" class="theme-toggle" onclick="__toggleTheme()" '
                 'title="Switch cyber / light theme"></button>')
_THEME_JS = (  # sets the toggle label to the OTHER theme; flips + persists on click
    '<script>(function(){'
    'function lbl(t){return t==="light"?"◐ Cyber":"◑ Light"}'
    'window.__toggleTheme=function(){'
    'var c=document.documentElement.getAttribute("data-theme")||"cyber";'
    'var n=c==="light"?"cyber":"light";'
    'document.documentElement.setAttribute("data-theme",n);'
    'try{localStorage.setItem("bioinf-theme",n)}catch(e){}'
    'var b=document.getElementById("__tt");if(b)b.textContent=lbl(n)};'
    'var c0=document.documentElement.getAttribute("data-theme")||"cyber";'
    'var b0=document.getElementById("__tt");if(b0)b0.textContent=lbl(c0)'
    '})()</script>'
)


def _open_page(tab_title: str) -> str:
    """Shared shell open: doctype + head (with no-flash theme init) + body + the
    toggle control + the .wrap container. Both reports call this."""
    return (
        '<!DOCTYPE html>'
        '<html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f'{_THEME_INIT}'
        f'<title>{_e(tab_title)}</title><style>{_CSS}</style></head><body>'
        f'{_THEME_TOGGLE}'
        '<div class="wrap">'
    )


def _close_page(gen_note_html: str = "") -> str:
    """Shared shell close: an optional generated-by note + the toggle JS + the
    closing tags. Pass the note here (env report) or append it first and pass ""
    (run dashboard, which builds its own honest provenance note)."""
    return f'{gen_note_html}</div>{_THEME_JS}</body></html>'


def _header_banner(title_html: str, pill_html: str, rows: list[tuple[str, str]]) -> str:
    """The header banner — the ONE shared page-header used by BOTH the Layer-1
    env report and the Layer-2 run dashboard, so the two artifacts read as one
    family. The corner blocks are decoration the light theme hides; the frame
    itself is `.head::before`. `title_html` and every row VALUE are inserted verbatim (callers
    escape); row KEYS are escaped here. Empty-value rows are dropped."""
    body = "".join(f'<tr><td class="k">{_e(k)}</td><td>{v}</td></tr>'
                   for k, v in rows if v != "" and v is not None)
    return (
        '<div class="head">'
        '<span class="cr cr-tl"></span><span class="cr cr-br"></span>'
        f'<h1>{title_html}{pill_html}</h1>'
        f'<table class="head-kv">{body}</table>'
        '</div>'
    )


# Re-exported under the historical private name so call sites here don't churn —
# the canonical helper now lives in env_report (so the .md renderer can share it,
# the R1 fix point); see env_report.requested_versions docstring.
_requested_versions = _shared_req_versions


def _tier_for(t: str, is_adopt: bool, pkg: Optional[dict], shipped: list) -> str:
    if is_adopt:
        return "adopted (biocontainer)"
    return _install_method(t, pkg, shipped)


# Per-tool SHIP assurance — HOW each shipped long-tail tool is anchored. Binary
# carries the install→ship integrity-chain verdict (F1/F2); C5 extends the SAME
# disclosure to every other tier (env_freeze._replay_assurance) so the report can
# never imply uniform trust — a source tool on a floating branch (drifts) must not
# look like a digest-pinned one. `ok` = an immutable anchor a rebuild reproduces;
# `na` = ships valid but disclosed-unverified. Conflating the two is what F1/F2 forbid.
_ASSURANCE_BADGE = {
    # binary tier (install→ship checksum chain)
    "authenticated":            ("ok",  "✓ checksum-verified"),
    "pinned_tofu":              ("na",  "⚠ pinned (unverified, TOFU)"),
    "unanchored_cross_platform":("na",  "⚠ cross-platform ship (unverified)"),
    "unanchored":               ("na",  "⚠ unverified"),
    # C5 — other tiers. verified (immutable, rebuild-reproducible):
    "commit_pinned":            ("ok",  "✓ pinned @ commit (rebuilt in-image)"),
    "built_pinned":             ("ok",  "✓ built from pinned source"),
    "lock_pinned":              ("ok",  "✓ lock-pinned"),
    # disclosed-unverified (moves / not content-anchored):
    "ref_pinned_tofu":          ("na",  "⚠ pinned to a movable ref (TOFU)"),
    "built_unpinned":           ("na",  "⚠ unpinned version (drifts)"),
    "cpan_tofu":                ("na",  "⚠ CPAN (unverified, TOFU)"),
    "repo_tofu":                ("na",  "⚠ repo version (unverified, TOFU)"),
    "command_pinned":           ("na",  "⚠ literal command (unverified)"),
    "unpinned":                 ("na",  "⚠ unpinned (floating branch — drifts)"),
}


def _assurance_badge(s: dict) -> str:
    """A pill disclosing a shipped tool's ship assurance. Empty for steps with no
    `assurance` key (nothing to disclose), so unaffected steps render unchanged."""
    a = (s or {}).get("assurance")
    if not a:
        return ""
    cls, txt = _ASSURANCE_BADGE.get(a, ("na", f"⚠ {a}"))
    return f' <span class="pill {cls}" style="font-size:11px">{_e(txt)}</span>'


def _installed_version(t: str, is_adopt: bool, pkg: Optional[dict], v: Optional[dict],
                       shipped: Optional[list] = None,
                       adopt_source: Optional[dict] = None,
                       image_digest: str = "") -> str:
    """THE installed-version cell — sourced ONLY from observations of the shipped
    image, NEVER from what was requested.

    Both modes defer to `_resolved_version` (SBOM version > recorded binary version >
    the version the tool PRINTED in its in-image evidence, first-line-narrowed against
    the htslib trap > install anchor). ADOPT gets ONE extra rung ahead of the printed
    banner: the biocontainer tag, which the manifest digest binds to exactly that
    bioconda build (`1.21--h50ea8bc_0` for samtools) — an observation of the shipped
    bytes, not a request echo. The tag is a shared mulled hash for a MULTI-tool
    biocontainer, so it is used only when the SBOM couldn't give a per-tool version.

    It must NEVER fall back to the requested version. When an author-image adopt ships
    a tool compiled from source in the authors' Dockerfile, that tool is absent from
    the SBOM and has no biocontainer tag; returning `req_v` there prints the REQUESTED
    version in the Installed column, unlabelled, on a green validated row — "says
    1.21, ships 1.19". The honest value is what the image yields, or '' = unrecorded;
    the renderer shows absence and the requested number stays in its own column."""
    if (is_adopt and not (pkg and pkg.get("version"))
            and adopt_source and adopt_source.get("tag")):
        return adopt_source["tag"]
    return _resolved_version(t, pkg, v, shipped)


def _created_line(iso: str) -> str:
    """`2026-09-29T07:19:42.203990+00:00` → `2026-09-29 07:19 UTC`. Anything that is
    not an ISO timestamp is printed as recorded — never reformatted into a guess."""
    if not iso:
        return "—"
    try:
        from datetime import datetime, timezone
        dt = datetime.fromisoformat(str(iso))
    except ValueError:
        return str(iso)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    return dt.strftime("%Y-%m-%d %H:%M")


def _tools_line(requested: int, passed: int, total: int, is_adopt: bool, shallow: int) -> str:
    """The header's one sentence about the requested tools, qualified by evidence depth."""
    if requested == 0:
        return "none requested"
    s = f"{requested} requested"
    if is_adopt and not total:
        return s + ", adopted by published digest (no check was re-run here)"
    if not total:
        return s + ", no check was run in the shipped image"
    if passed == total:
        if total == 1:
            s += ". It ran and passed its check inside the shipped image"
        else:
            s += f". All {total} ran and passed their check inside the shipped image"
    else:
        s += f". {passed} of {total} passed their check inside the shipped image"
    if shallow:
        if total == 1:
            who = "it"
        elif shallow >= total:
            who = "all of them"
        else:
            who = f"{shallow} of them"
        s += (f". For {who} the check only confirmed the tool is present and reports "
              f"a version; it did not exercise it")
    return s + "."


# What an UNOBSERVED clause means to a reader, keyed by the clause's own id. The
# fallback is the clause's recorded `detail`, so a new clause is never silently
# dropped from the sentence — it just arrives in the contract's words until it is
# given plain ones here.
_PLAIN_GAP = {
    "WELL_FORMED.shipped_binaries":
        "a pre-built biocontainer carries no list of the binaries it shipped, so that "
        "part of the record is empty",
    "WELL_FORMED.tool_identities":
        "the record does not say what each tool calls itself, so the page cannot "
        "describe the tools' own identities",
    "BUILT.platform":
        "the shipped image's architecture was not read, so it is not confirmed to be "
        "the one the record claims",
    "VALIDATED_IN_IMAGE":
        "no check was run inside the shipped image, so nothing here shows the tools work",
    "VALIDATED_IN_IMAGE.discriminates":
        "the checks were not re-run in a control image without the tool, so a pass is "
        "not shown to be about this image",
    "POLICY_CLEAN.accelerator_observed":
        "the shipped image was not opened to read its GPU toolkit, so the accelerator "
        "claim is not confirmed against it",
}


def _plain_gap(c) -> str:
    return _PLAIN_GAP.get(c.clause) or (c.detail or c.clause)


def _push_line(push: str) -> str:
    """`push_status` in words. The producer writes one of four shapes (freeze_tools):
    `not-configured` · `pushed: <ref>` · `push-failed: <ref> (tarball fallback)` ·
    `skipped: …`. Anything else is printed as recorded."""
    if push == "not-configured":
        return ('no <span class="note">— no registry is configured; the image is '
                'delivered as the archive above</span>')
    kind, _, rest = push.partition(":")
    rest = rest.strip()
    if kind == "pushed":
        return f'yes <span class="note">— <code>{_e(rest)}</code></span>'
    if kind == "push-failed":
        return (f'<span class="warn">no — the push failed</span> <span class="note">'
                f'({_e(rest)}); the archive above is the delivery</span>')
    if kind == "skipped":
        return f'no <span class="note">— {_e(rest)}</span>'
    return _e(push)


def _status_line(contract) -> str:
    """The header's Status row for a PASSING contract: a Passed pill, a plain
    account of any guarantee that could not be examined, and the coverage tag."""
    from agent.skills.env_honesty import ASSURANCE
    gaps = contract.unobserved
    if not gaps:
        # "applicable" is load-bearing: NOT_APPLICABLE clauses (accelerator/license/
        # provenance on an ordinary env) examined nothing by design, and the
        # guarantee table below renders them n/a.
        return ('<span class="pill ok">Passed</span> registered and shippable; every '
                'applicable guarantee was checked on this record. '
                'Coverage tag: <code>proven</code>.')
    n = len(gaps)
    parts = [f'<span class="pill ok">Passed</span> registered and shippable. '
             f'{n} guarantee{"s" if n != 1 else ""} could not be examined: '
             + "; ".join(_e(_plain_gap(c)) for c in gaps) + "."]
    if any(c.establishes == ASSURANCE for c in gaps):
        parts.append("This limits what was proved. Re-freeze with "
                     "<code>evidence={tool: &lt;command that runs the tool&gt;}</code> to close it.")
    if any(c.establishes != ASSURANCE for c in gaps):
        parts.append("This is expected on this install path and limits what the report "
                     "describes, not what it proved.")
    parts.append('Coverage tag: <code>degraded</code>.')
    return " ".join(parts)


def render_env_report_html(record: dict) -> str:
    """Render the freeze record as a self-contained HTML page (see module docstring
    for the honesty contract this upholds).

    This is a PURE Layer-1 artifact: it asserts only build-locus honesty (BUILT /
    VALIDATED_IN_IMAGE / POLICY_CLEAN) and is written ONCE at freeze, immutable
    thereafter. It never claims the env works on a cluster — that is a Layer-2
    fact carried by a sealed workflow (see run_dashboard_html.render_run_dashboard_html).
    Rebuilding an env yields a NEW freeze record with a NEW digest and its OWN
    ENV.html; this one is never mutated by a downstream seal."""
    r = record or {}
    name = r.get("name") or (r.get("image") or "env").split(":")[0].split("/")[-1]
    mode = r.get("mode", "?")
    is_adopt = mode == "adopt"
    requested = list(r.get("requested_tools") or [])
    resolved = list(r.get("resolved_packages") or [])
    system = list(r.get("system_packages") or [])
    verifs = list(r.get("verifications") or [])
    shipped = list(r.get("shipped_binaries") or [])
    conda_specs = list(r.get("conda_specs") or [])
    adopt_source = r.get("adopt_source") if isinstance(r.get("adopt_source"), dict) else None
    image_digest_raw = r.get("image_digest") or ""
    vidx, pidx = _verif_index(verifs), _pkg_index(resolved)
    req_versions = _requested_versions(r)
    # VERSION DIVERGENCE: the tools whose OBSERVED installed
    # version differs from what was requested — computed ONCE here (the shared
    # definition), flagged ⚠ in the Tools table below AND carried into the attestation
    # + list_installed so a mismatch shows up identically wherever both numbers appear.
    diverging = {d["tool"].lower(): d for d in _version_divergences(r)}
    # IDENTITY DISCLOSURE: the tool's OWN self-description, keyed by tool name.
    # Rendered as a labelled, clearly-UNVERIFIED sub-line under each tool row — the record
    # already parsed at register/check_build, but degrade gracefully rather than crash a
    # report over an optional disclosure.
    try:
        identities = {i.tool.lower(): i for i in _tool_identities(r)}
    except Exception:
        identities = {}
    requested_set = {t.lower() for t in requested}
    ride = [p for p in resolved if isinstance(p, dict) and p.get("name")
            and p["name"].lower() not in requested_set]
    passed = sum(1 for v in verifs if isinstance(v, dict) and v.get("passed"))
    total = len(verifs)

    # THE CONTRACT, COMPUTED BEFORE THE PILL — because the pill is a claim ABOUT it.
    #
    # The pill must rest on `_contract.violations` (did any clause FAIL), not on
    # `_contract.coverage` alone (did each clause RUN), and not on `passed == total`
    # over the verifications list — a different and much weaker question, and one the
    # adopt branch never even asks. A green tick over a failed honesty contract (a
    # WELL_FORMED record dialect that cannot be read without guessing; evidence that
    # pipes into `head -5` and so records HEAD's exit status) is the precise failure
    # this codebase exists to prevent, in the one artifact the user actually opens.
    # `check_build` is the SAME function `freeze` refuses on, so the page and the
    # gate answer alike.
    from agent.skills.env_honesty import (CHECKED, NOT_APPLICABLE, UNOBSERVED,
                                          check_build,
                                          evaluate_build, guarantee_verdicts)
    try:
        _contract = evaluate_build(r)
        _violations = list(check_build(r))
    except Exception as e:                       # never let a report die on its own audit
        _contract, _violations = None, []
        _contract_error = f"{type(e).__name__}: {e}"
    else:
        _contract_error = ""

    P: list[str] = []
    P.append(_open_page(f"Environment report — {name}"))

    # -- HEADER BANNER ------------------------------------------------------
    # Yellow title "Bioinfo install report — {name}" + status pill, with a small
    # kv table of the run's at-a-glance facts inside the cyan-bordered banner.
    if _violations:
        # FIRST, AND UNCONDITIONALLY. A failed contract outranks every other thing this
        # page could say about itself, including "adopted by digest" — an adopted image
        # is still an image that has to earn its record.
        pill = (f'<span class="pill bad">✗ FAILS the honesty contract — '
                f'{len(_violations)} violation{"s" if len(_violations) != 1 else ""}</span>')
    elif _contract_error:
        pill = '<span class="pill bad">⚠ contract could not be evaluated</span>'
    elif is_adopt:
        pill = '<span class="pill adopt">Adopted by digest</span>'
    elif total and passed == total:
        pill = '<span class="pill ok">✓ Validated in shipped image</span>'
    elif total:
        pill = '<span class="pill bad">✗ Validation incomplete</span>'
    else:
        pill = '<span class="pill na">No tools recorded</span>'
    mode_desc = mode
    if r.get("build_method"):
        mode_desc += f" · {r['build_method']}"
    if r.get("engine") and r.get("engine") != "none":
        mode_desc += f" · engine {r['engine']}"
    # -- THE TOOLS LINE. The count a reader takes away, and unqualified it can sit
    # over an env whose tool cannot import its own plotting module — `--help`
    # evidence is answered by argparse before any dependency is touched. The
    # per-tool table badges depth (`⚠ version`); the headline must too, or the
    # strongest sentence on the page is the least qualified one. The qualifier
    # says what the check confirmed, not what the tool is: `evidence_depth` is a
    # structural reading of command TEXT and under-reports a command that loads a
    # library and then uses it. Under-claiming costs a reader nothing.
    _shallow_tools = _shallow_evidence_tools(r)
    tools_line = _tools_line(len(requested), passed, total, is_adopt, len(_shallow_tools))
    also = []
    if ride:
        also.append(f"{len(ride)} conda package{'s' if len(ride) != 1 else ''}")
    if system:
        also.append(f"{len(system)} system package{'s' if len(system) != 1 else ''} (apt)")
    head_rows = [
        ("Image", f'<code>{_e(r.get("image",""))}</code>' if r.get("image") else "—"),
        ("Created", _e(_created_line(r.get("created_at", "")))),
        ("Platform", _e(r.get("platform", "—"))),
        ("Mode", _e(mode_desc) or "—"),
        ("Validated on", _e(_locus_line(r.get("validation_locus", ""))) or "—"),
        ("Tools", _e(tools_line)),
        ("Also in the image", " · ".join(_e(a) for a in also)),
    ]
    # -- THE STATUS ROW. freeze returns `proven` or `degraded` and that value
    # evaporates with the session; the README teaches a reader to look for the word,
    # so the page prints it — as a tag at the end of a plain sentence, not as the
    # headline. Derived at render time from the SAME contract walk the gate runs
    # (`contract.unobserved` is what freeze's two literal terminals branch on), so
    # the page and the tag cannot drift apart. The sentence is written for the
    # reader from the unobserved clauses themselves (`_plain_gap`); the agent-facing
    # advisory `coverage_disclosure` writes into the freeze return is the same facts
    # in the contract's vocabulary, and the coverage table below carries the ids.
    # When the contract FAILS, the ⛔ section outranks any status and this row
    # renders nothing: proven/degraded is a vocabulary for REGISTERED envs, and
    # printing either over a violation would soften it.
    if _contract is not None and not _violations and not _contract_error:
        head_rows.append(("Status", _status_line(_contract)))
    # A loud, dedicated header line when any observed version diverges from the request —
    # so the mismatch is unmissable before the reader even scrolls to the Tools table.
    if diverging:
        parts = ", ".join(f'{_e(d["tool"])} (requested {_e(d["requested"])} → '
                          f'installed {_e(d["installed"])})' for d in diverging.values())
        head_rows.append(("Version check",
                          f'<span class="pill bad">⚠ {len(diverging)} '
                          f'requested ≠ installed</span> {parts}'))
    # Shared header banner — same
    # helper the Layer-2 run dashboard uses, so the two reports are one family.
    P.append(_header_banner(f"Bioinfo install report — {_e(name)}", pill, head_rows))

    # -- HONESTY CONTRACT VIOLATIONS (first, or not at all) -----------------
    # Placed ABOVE the tools table on purpose. The reader's question when they open this
    # page is "can I trust what it says" — so anything that says NO has to arrive before
    # the content it undermines, not in a coverage table 700 lines down.
    if _violations:
        P.append('<section class="bx"><h2>⛔ This env FAILS the honesty contract</h2>')
        P.append('<div class="bx-body">')
        P.append('<p><b>Do not treat the rest of this page as verified.</b> '
                 f'{len(_violations)} clause{"s" if len(_violations) != 1 else ""} of the '
                 'contract <code>freeze</code> enforces did not hold for this record. '
                 '<code>freeze</code> refuses to register an env on any of these, so a '
                 'record carrying one was either written before that clause existed or '
                 'was assembled outside the primitive.</p>')
        P.append('<div class="tbl-wrap"><table class="t">'
                 '<thead><tr><th>Clause</th><th>Where</th><th>What is wrong</th></tr>'
                 '</thead><tbody>')
        for v in _violations:
            if not isinstance(v, dict):
                v = {"message": str(v)}
            P.append(f'<tr><td><code>{_e(v.get("invariant", "?"))}</code></td>'
                     f'<td><code>{_e(v.get("where", "—"))}</code></td>'
                     f'<td>{_e(v.get("message", ""))}</td></tr>')
        P.append('</tbody></table></div></div></section>')
    elif _contract_error:
        P.append('<section class="bx"><h2>⚠ The honesty contract could not be evaluated</h2>'
                 '<div class="bx-body"><p>This page could not run the contract over its own '
                 'record, so it makes <b>no claim either way</b> about whether the env '
                 'satisfies it — that is UNCHECKED, not a pass. The evaluation raised: '
                 f'<code>{_e(_contract_error)}</code></p></div></section>')

    # -- TOOLS (centerpiece — SAME columns for build & adopt) ---------------
    P.append('<section class="bx">')
    P.append(f'<h2>Tools <span class="note">({len(requested)} requested)</span></h2>')
    P.append('<div class="bx-body">')
    if requested:
        P.append('<div class="tbl-wrap"><table>')
        P.append("<tr><th>Requested tool</th><th>Requested version</th>"
                 "<th>Installed version</th><th>Install tier</th>"
                 "<th>Validated in image</th></tr>")
        for t in requested:
            pkg = pidx.get(t.lower())
            v = vidx.get(t.lower())
            req_v = req_versions.get(t, "")
            req_cell = _e(req_v) if req_v else '<span class="muted">any</span>'
            inst_v = _installed_version(t, is_adopt, pkg, v, shipped,
                                         adopt_source=adopt_source,
                                         image_digest=image_digest_raw)
            anchor = _install_anchor(t, shipped)
            if inst_v and anchor and anchor != inst_v and _is_sha(anchor):
                # full provenance: banner/conda version + the commit it was built
                # from. The commit alone IS the install identity for synthesized /
                # source tiers — keep it visible even when a human-friendly version
                # is now leading the cell.
                inst_cell = (f"{_e(inst_v)} <span class=\"muted\">"
                             f"(commit {_e(anchor[:12])})</span>")
            elif inst_v:
                inst_cell = _e(inst_v)
            elif is_adopt:
                # Author-image adopt of a tool absent from the SBOM with no tag and no
                # printed version — we adopted the bytes by digest and did not observe a
                # version. Say exactly that; NEVER echo the requested number (W1).
                inst_cell = ('<span class="muted" title="image adopted by digest — '
                             'no version observed in the shipped image">not recorded</span>')
            else:
                inst_cell = '<span class="muted">—</span>'
            # DIVERGENCE ⚠ (W5): observed installed ≠ requested. Append a loud pill so a
            # human catches the mismatch AT A GLANCE, not by diffing two columns.
            if t.lower() in diverging:
                d = diverging[t.lower()]
                inst_cell += (f' <span class="pill bad" title="requested {_e(d["requested"])}'
                              f', installed {_e(d["installed"])}">⚠ ≠ requested '
                              f'{_e(d["requested"])}</span>')
            tier_cell = _e(_tier_for(t, is_adopt, pkg, shipped))
            if v and (v or {}).get("check"):
                # Real in-image evidence exists — say what it showed, with the literal
                # command one click away. A freeze_from_image adopt VALIDATES in-image,
                # unlike a biocontainer adopt, so hiding it behind 'trusted by digest'
                # would understate what we proved.
                status = _evidence_cell(v, t)
            elif is_adopt:
                status = '<span class="badge na">trusted by digest</span>'
            else:
                status = _badge(v.get("passed") if v else None, "", t)
            P.append(f"<tr><td>{_e(t)}</td><td>{req_cell}</td>"
                     f"<td>{inst_cell}</td><td>{tier_cell}</td><td>{status}</td></tr>")
            # Identity disclosure sub-row — the tool's OWN words, from the registry the
            # shipped package came from. Labelled UNVERIFIED and visually distinct from the
            # green validation badge: this is what the tool CLAIMS to be, never proof it is.
            # A human reads "Translate Spreadsheet Cell Ranges" under `cellranger` and knows.
            idn = identities.get(t.lower())
            if idn is not None and idn.self_description:
                who = _e(idn.source) if idn.source else "its registry"
                P.append(
                    f'<tr class="id-row"><td colspan="5" title="the package\'s own '
                    f'description, as published by the registry it was installed from. '
                    f'Not verified by this report: it is what the tool says it is, so a '
                    f'name that resolved to the wrong project shows up here.">'
                    f'<span class="muted">{who} describes it as</span> '
                    f'<q>{_e(idn.self_description)}</q></td></tr>')
        P.append("</table></div>")
    else:
        P.append(_empty("(no tools recorded)"))
    P.append('</div></section>')

    # -- ALONG FOR THE RIDE + INSTALL COMMANDS (same bordered panel) --------
    P.append('<section class="bx"><details class="fold"><summary>')
    P.append(f'<h2>Along for the ride <span class="note">'
             f'({len(ride)} transitive dependencies)</span></h2>')
    P.append('</summary><div class="bx-body">')
    if ride:
        P.append('<div class="tbl-wrap"><table>')
        P.append("<tr><th>Package</th><th>Version</th><th>Kind</th></tr>")
        for p in ride:
            P.append(f"<tr><td>{_e(p['name'])}</td><td>{_e(p.get('version',''))}</td>"
                     f"<td>{_e(p.get('kind',''))}</td></tr>")
        P.append("</table></div>")
    else:
        P.append(_empty("(closure not captured in-locus — an adopted image is trusted "
                        "by its published digest, not introspected here)" if is_adopt else
                        "(none — every resolved package was directly requested)"))
    P.append('</div></details></section>')

    # -- INSTALL COMMANDS (own top-level section, per the "all reports share
    # the same set of sections" rule; this also matches the SBOM split
    # everywhere else, where "what was installed" and "how it was installed"
    # are separately enumerable). Long-tail commands are the binary/source/
    # synthesized/perl/cargo/go install bodies baked verbatim into the
    # shipped image — the command IS the provenance.
    P.append('<section class="bx"><details class="fold"><summary>')
    if is_adopt:
        # For an ADOPT, the install command IS the apptainer/docker pull-by-digest
        # against the published biocontainer. The bytes WE shipped == the bytes
        # the BioContainer registry serves at that digest — pulling by digest is
        # the install. We render it whether or not adopt_source is populated
        # (legacy records have just `image`, which is enough to reconstruct).
        image_ref = r.get("image", "")
        pull_cmd = f"apptainer pull docker://{image_ref}" if image_ref else ""
        P.append('<h2>Install commands '
                 '<span class="note">(adopt — pull the published biocontainer '
                 'by manifest digest; the digest IS the provenance)</span></h2>')
        P.append('</summary><div class="bx-body">')
        if adopt_source and adopt_source.get("tag"):
            P.append('<p style="margin:10px 0 2px"><b>'
                     f'{_e(adopt_source.get("repo") or "biocontainer")} '
                     f'@ tag <code>{_e(adopt_source["tag"])}</code></b></p>')
        elif image_ref:
            P.append('<p style="margin:10px 0 2px"><b>'
                     'biocontainer (tag not captured at freeze time; '
                     'manifest digest pins identity)</b></p>')
        if pull_cmd:
            P.append(f'<pre>{_e(pull_cmd)}</pre>')
        else:
            P.append(_empty("(no image ref recorded — cannot reconstruct command)"))
        P.append('</div></details></section>')
    else:
        P.append(f'<h2>Install commands <span class="note">({len(shipped)} long-tail '
                 'step(s) baked verbatim into the shipped image — the command IS '
                 'the provenance)</span></h2>')
        P.append('</summary><div class="bx-body">')
        # Parse ONCE, and survive a record that does not conform. `shipped` above is the
        # RAW list (used only for the count); this is the typed read, and on a legacy
        # record it raises. Letting that raise escape the renderer costs the user the
        # ENTIRE page ("html report render failed: ValidationError…") for the one
        # record class that most needs explaining — so degrade gracefully, the same
        # courtesy as the sibling identity read three sections up, and say WHY rather
        # than silently showing nothing.
        try:
            typed_shipped, shipped_parse_error = _shipped_binaries(r), ""
        except Exception as e:
            typed_shipped, shipped_parse_error = [], str(e)
        if shipped_parse_error:
            P.append(_empty(
                f"({len(shipped)} long-tail step(s) recorded, but they do not conform to the "
                f"declared ShippedBinary shape, so they cannot be shown without guessing at "
                f"their fields. This record predates the schema — re-freeze it. "
                f"See the contract-coverage table below: {shipped_parse_error[:200]})"))
        elif typed_shipped:
            for s in typed_shipped:
                # `name or purpose or "tool"` read keys the authors'-image producer
                # never wrote, so every one of its binaries fell through to the literal
                # string "tool" — four rows labelled <b>tool</b> under a header
                # asserting "the command IS the provenance", with the tool NAME
                # rendered inside the <pre> as if it were a shell line. `tool` and
                # `install_command` are separate fields now; an adopted binary has no
                # command and says so rather than borrowing another field's value.
                # The TOOL names itself; the provenance prose ("seqkit (release
                # binary)") rides alongside as the note it always was, rather than
                # standing in for a name it never was.
                prov = f' <span class="note">{_e(s.provenance)}</span>' if s.provenance else ""
                P.append(f'<p style="margin:10px 0 2px"><b>{_e(s.tool)}</b>'
                         f'{_assurance_badge(s.model_dump())}{prov}</p>')
                if s.install_command:
                    P.append(f"<pre>{_e(s.install_command.strip())}</pre>")
        else:
            # SAY WHAT THE RECORD SAYS, NOT A CATEGORY. "Pure conda env" claimed for
            # ANY env with zero baked RUN steps contradicts a pip-built record's own
            # Mode row (`engine pixi`) and Install-tier column (`pip (PyPI)`) two
            # screens above. A plain pip install leaves no baked command because it
            # rides the ENGINE's PyPI layer and is pinned by the lock, not by a RUN
            # line — so derive the sentence from the same `packages[].kind` the tier
            # column reads, and only claim conda-only when the record shows conda-only.
            _pypi = [p.get("name", "?") for p in (r.get("packages") or [])
                     if isinstance(p, dict) and p.get("kind") == "pypi"]
            if _pypi:
                P.append(_empty(
                    f"(no long-tail RUN steps — nothing was installed by a raw baked "
                    f"command. Not a pure-conda env: {len(_pypi)} pip package(s) "
                    f"({', '.join(sorted(_pypi)[:6])}{', …' if len(_pypi) > 6 else ''}) "
                    f"came through the engine's PyPI layer and are pinned by the lock "
                    f"in the recipe, not by a command shown here.)"))
            else:
                P.append(_empty("(no long-tail steps — every install came through "
                                "the conda layer)"))
        P.append('</div></details></section>')

    # -- SYSTEM (apt) PACKAGES (always shown; foldable when present) --------
    P.append('<section class="bx"><details class="fold"><summary>')
    P.append(f'<h2>System packages (apt) <span class="note">'
             f'({len(system)} captured — OS layer; SBOM only, NOT pinned in the content digest)</span></h2>')
    P.append('</summary><div class="bx-body">')
    if system:
        P.append('<div class="tbl-wrap"><table>')
        P.append("<tr><th>Package</th><th>Version</th></tr>")
        for p in system:
            if isinstance(p, dict) and p.get("name"):
                P.append(f"<tr><td>{_e(p['name'])}</td><td>{_e(p.get('version',''))}</td></tr>")
        P.append("</table></div>")
    else:
        P.append(_empty("(no apt SBOM captured — adopted image; the apt layer was not "
                        "introspected in-locus)" if is_adopt else
                        "(no system packages recorded)"))
    P.append('</div></details></section>')

    # -- ARTIFACTS (one table — image · digests · files · delivery) ---------
    P.append('<section class="bx">')
    P.append('<h2>Artifacts <span class="note">'
             'the files that travel with this environment</span></h2>')
    P.append('<div class="bx-body">')
    # Order: identity (image + two checksums) → the two PRIMARY companion artifacts
    # (recipe = rebuild instructions, attestation = provenance) → delivery
    # (archive / lock / registry). This HTML IS the canonical Layer-1 view;
    # there is no sibling .md to list. The HPC delivery hints on the record are
    # NOT repeated here — the RUN dashboard and the rebuild instructions carry them.
    art_rows: list[tuple[str, str]] = [
        ("Docker image", f'<code>{_e(r.get("image","—"))}</code>' if r.get("image") else "—"),
        ("Image checksum",
         f'<code>{_e(r.get("image_digest","—"))}</code>'
         '<span class="note"> — the exact bytes that ship; the HPC image is built from these</span>'
         if r.get("image_digest") else "—"),
        ("Build inputs checksum",
         f'<code>{_e(r.get("content_digest","—"))}</code>'
         '<span class="note"> — what went into the build (package lock, install commands, '
         'platform, base image); a rebuild from the recipe must reproduce it</span>'
         if r.get("content_digest") else "—"),
    ]
    # The build recipe ALWAYS exists, in BOTH forms, for every install path — a frozen
    # env is only a solved component if anyone can reproduce it. For adopt mode the
    # recipe is "pull the biocontainer by digest"; for a build it is the self-contained
    # replayable recipe verify_env_recipe rebuilds and digest-checks.
    _verify_note = ('<span class="note"> — for an adopted image the recipe is to pull the '
                    'published container by its checksum</span>' if is_adopt else
                    '<span class="note"> — the agent can rebuild from this file alone and '
                    'confirm the build inputs checksum matches</span>')
    art_rows.append(("Rebuild recipe (machine-readable)",
                     f'<a href="{_e(name)}.recipe.yaml"><code>{_e(name)}.recipe.yaml</code></a>'
                     + _verify_note))
    art_rows.append(("Rebuild instructions",
                     f'<a href="{_e(name)}.recipe.md"><code>{_e(name)}.recipe.md</code></a>'
                     '<span class="note"> — the command sequence for a rebuild by hand</span>'))
    art_rows.append(("Provenance statement",
                     f'<a href="{_e(name)}.attestation.json"><code>{_e(name)}.attestation.json</code></a>'
                     '<span class="note"> — which image was built from which inputs, in the '
                     'in-toto/SLSA format third-party signing tools verify. Unsigned as written.</span>'))
    if r.get("tarball"):
        tb = r["tarball"]
        art_rows.append(("Image archive (.tar)",
                         f'<a href="file://{_e(tb)}"><code>{_e(tb)}</code></a>'
                         '<span class="note"> — the exported image the HPC image is built from</span>'))
    else:
        art_rows.append(("Image archive (.tar)",
                         '<span class="muted">not produced for an adopted image</span>' if is_adopt else
                         '<span class="muted">not produced (delivered through a registry, or the '
                         'build skipped it)</span>'))
    engine = r.get("engine") if r.get("engine") not in (None, "", "none") else ""
    _lock_note = f'<span class="note"> — the exact package solve, by {_e(engine)}</span>' if engine \
        else '<span class="note"> — the exact package solve</span>'
    if r.get("conda_lock"):
        cl = r["conda_lock"]
        art_rows.append(("Conda lock file",
                         f'<a href="file://{_e(cl)}"><code>{_e(cl)}</code></a>' + _lock_note))
    else:
        art_rows.append(("Conda lock file",
                         '<span class="muted">not produced for this environment</span>'))
    push = r.get("push_status", "")
    if push:
        art_rows.append(("Pushed to a registry", _push_line(push)))
    P.append(_kv_table(art_rows))
    P.append('</div></section>')

    # -- DECLARED POLICY (verified vs declared — plain table + note) --------
    gated = _record_is_gated(r)
    licenses = list(r.get("licenses") or [])
    accel = r.get("accelerator") if isinstance(r.get("accelerator"), dict) else None
    accel_type = (accel or {}).get("type") or "none"
    P.append('<section class="bx">')
    P.append('<h2>Declared policy <span class="note">submitter-declared; the contract checks '
             'these for consistency (I12/I13), <b>not</b> a runtime-verified fact — a caller assertion</span></h2>')
    P.append('<div class="bx-body">')
    pol_rows = [
        ("License-gated", "yes" if gated else "no"),
        ("Redistributable", "yes" if r.get("redistributable", not gated) else "no"),
        ("Licenses", ", ".join(_e(x) for x in licenses) if licenses
                     else '<span class="muted">— (none declared)</span>'),
        ("Accelerator", _accel_declared_vs_observed(r, accel, accel_type)),
    ]
    P.append(_kv_table(pol_rows))
    P.append('</div></section>')

    # -- HOW VERIFIED (rendered FROM the contract — never a second account) --
    #
    # No hand-written paragraph per build_method: emitted unconditionally, such prose
    # asserts "every requested tool re-ran green" and "POLICY_CLEAN — I12 and I13
    # passed" over records whose generated coverage table DIRECTLY BELOW IT marks
    # those same clauses `unobserved` / `n/a` — an absent observation rounded up into
    # "passed", under the heading "How this was verified", in the prose half a human
    # reads first.
    #
    # Instead: one bullet per Layer-1 guarantee. The STATEMENT comes from
    # env_honesty.LAYER1_GUARANTEES (the roster the registry lint keeps complete);
    # the VERDICT comes from env_honesty.guarantee_verdicts over THIS record. No
    # sentence here can contradict the record, because no sentence here was written
    # by a human who had not read it.
    P.append('<section class="bx">')
    P.append('<h2 id="verify">How this was verified</h2>')
    P.append('<div class="bx-body">')
    if _contract is None:
        P.append(_empty("the contract could not be evaluated over this record — "
                        "see the notice at the top of the page"))
        P.append('</div></section>')
        P.append(_close_page('<p class="gen">Generated deterministically from the freeze '
                             'record — no field on this page was authored by the agent.</p>'))
        return "\n".join(P)

    _rows = guarantee_verdicts(_contract)
    P.append('<p class="note">One line per Layer-1 guarantee — what it promises, and what '
             'the contract actually established <b>over this record</b>. A guarantee that had '
             'nothing to examine says so; it is never reported as passed.</p>')
    P.append('<ul class="foot">')
    for g in _rows:
        badge = _VERDICT_BADGE.get(g["verdict"], _e(g["verdict"]))
        bits: list[str] = []
        if g["observations"]:
            bits.append(f"{g['observations']} observation(s) examined")
        if g["failed_clauses"]:
            bits.append("objected: " + ", ".join(f"<code>{_e(c)}</code>" for c in g["failed_clauses"]))
        if g["unobserved"]:
            bits.append("nothing to examine: "
                        + ", ".join(f"<code>{_e(c)}</code>" for c in g["unobserved"]))
        tail = f'<span class="note"> — {" · ".join(bits)}</span>' if bits else ""
        # The clause's own sentence for anything that did NOT check out. A verdict with
        # no reason sends the reader to the coverage table to discover things like "no
        # evidence was run in the shipped image" — the single most consequential fact
        # this page can carry, and it should not be a scavenger hunt.
        why = "".join(f'<div class="note" style="margin-left:1rem">{_e(n)}</div>'
                      for n in g["notes"])
        P.append(f'<li><b>{_e(g["guarantee"])}</b> {badge}<br>'
                 f'<span class="muted">{_e(g["statement"])}</span>{tail}{why}</li>')
    P.append("</ul>")

    # PROVENANCE is not a contract clause — it is where the bytes came from, which the
    # contract takes as its input rather than establishing. Kept separate from the list
    # above precisely so it cannot be misread as something that was verified.
    P.append('<h3 style="margin-top:1.2rem">Provenance of the bytes</h3>')
    P.append('<ul class="foot">')
    if is_adopt:
        P.append("<li><b>ADOPTED_BY_DIGEST</b> — these bytes were pulled by their immutable "
                 "manifest digest (above), not built here. Their provenance IS that digest: you "
                 "trust it exactly as far as you trust its publisher. What the digest cannot tell "
                 "you — whether the image carries the tool you asked for — is the "
                 "<code>VALIDATED_IN_IMAGE</code> line above.</li>")
    else:
        # PROVENANCE ONLY — no outcome verb. This bullet said "installed and VALIDATED
        # inside the image that ships … the bytes VALIDATED are the bytes that run on
        # HPC", branched on build method, emitted unconditionally. That is F2's exact
        # shape at one-tenth the size, and it was written INTO THE COMMIT THAT FIXED F2,
        # over a record that may carry zero verifications, with the whole suite green.
        # Where the bytes came from is a fact about the build; whether anything was
        # exercised in them is the VALIDATED_IN_IMAGE bullet above, and this section
        # must not answer that question a second time.
        P.append("<li><b>BUILT IN-CONTAINER</b> — these bytes were assembled inside the image "
                 "that ships, rather than built on the host and copied in, so install and ship "
                 "are one event. What was exercised in them is the "
                 "<code>VALIDATED_IN_IMAGE</code> line above.</li>")
        P.append("<li><b>Reproducibility</b> — the content digest binds the conda/PyPI lock, the "
                 "long-tail commands, the platform, and the digest-pinned base image. Release "
                 "binaries are sha256-anchored. The apt runtime layer is captured but not "
                 "version-pinned (<code>apt-get</code> is not reproducible across time).</li>")
    P.append("</ul>")

    # -- WHAT THE CONTRACT ACTUALLY LOOKED AT -------------------------------
    # The guarantee bullets above roll up to five rows. This table is the clause-level
    # mechanics underneath them — which sub-check ran, on what, and whether it objected.
    # Both are read off the same `_contract`, computed at the top of this function;
    # recomputing here would be a second evaluation of one record, and two evaluations
    # is how two answers to one question start.
    _mark = {CHECKED: ('<span class="ok">checked</span>', ""),
             NOT_APPLICABLE: ('<span class="muted">n/a</span>', ""),
             UNOBSERVED: ('<span class="warn">unobserved</span>', "")}
    P.append(f'<h3 style="margin-top:1.2rem">Contract coverage <span class="note">'
             f'{_e(_contract.summary())}</span></h3>')
    P.append('<table class="t"><thead><tr><th>Clause</th><th>Ran?</th><th>Establishes</th>'
             '<th>What it examined</th></tr></thead><tbody>')
    # A CLAUSE THAT RAN AND FAILED MUST NOT READ AS "checked" AND NOTHING ELSE.
    # This column answers "did it run", which is genuinely a different question from "did
    # it pass" — but for talos_v11 the effect was that the ONE mention of
    # `WELL_FORMED.shipped_binaries` anywhere on the page was a row marked `checked`, for
    # the clause that had just refused the record. That is worse than the violation being
    # absent: it reads as a clean result for the thing that failed.
    _failed_clauses = {v.get("invariant", "") for v in _violations if isinstance(v, dict)}

    def _clause_failed(clause: str) -> bool:
        # A clause owns its dotted children: `POLICY_CLEAN.accelerator` owns `I12.*` only
        # via _covers, so match on the recorded ids AND on the clause prefix.
        return any(fid == clause or fid.startswith(clause + ".") or clause.startswith(fid + ".")
                   for fid in _failed_clauses)

    for c in _contract.coverage:
        badge = _mark.get(c.status, (_e(c.status), ""))[0]
        if _clause_failed(c.clause) or any(_clause_failed(x) for x in (c.covers or ())):
            badge += ' <span class="pill bad">and FAILED</span>'
        P.append(f'<tr><td><code>{_e(c.clause)}</code></td><td>{badge}</td>'
                 f'<td class="muted">{_e(c.establishes)}</td><td>{_e(c.detail)}</td></tr>')
    P.append('</tbody></table>')
    if _contract.unobserved:
        P.append('<p class="note"><b>Read this page accordingly.</b> The clause(s) marked '
                 '<i>unobserved</i> had nothing to examine — they neither passed nor failed, '
                 'so nothing on this page rests on them. <i>n/a</i> is different: the '
                 'precondition is genuinely absent (no accelerator claimed, not license-gated), '
                 'which is itself a fact about the artifact.</p>')
    P.append('</div></section>')

    P.append(_close_page('<p class="gen">Generated deterministically from the freeze record'
                          ' — no field on this page was authored by the agent.</p>'))
    return "\n".join(P)
