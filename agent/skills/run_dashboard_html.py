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

THE PAGE MUST SHOW EVERYTHING THE SEAL GATED ON. A field the invariants read and the
renderer does not is a fact the record knows and the artifact will not say — and the
reader's recourse is to open the yaml, which is the thing these pages exist to spare
them. Three were in that state and are now rendered: the I4 self-test's actual
invocation (the substituted commands, not just the template), `runtime_configs` (in
I8's traceable universe — it carries the filters and thresholds a reader is reviewing),
and `service_dependencies` with its health probes (I10 refuses a seal on them). When
you add a field a gate reads, render it here, or add it to the list of things this
docstring admits are invisible.

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

from agent.models import core_data as _core_data
from agent.models.core_data import (RESOURCES_AUTHORITATIVE as _RES_AUTHORITATIVE,
                                    RESOURCES_EMULATED as _RES_EMULATED,
                                    RESOURCES_UNRECORDED as _RES_UNRECORDED,
                                    USAGE_LABELS,
                                    resource_usage_authority as _resource_usage_authority,
                                    step_is_validated, step_validation_failed,
                                    usage_commands, usage_output_type,
                                    usage_status)
from agent.skills.env_report_html import (
    _badge, _close_page, _e, _empty, _header_banner, _kv_table, _open_page,
)

#: How the three resource states are shown. EXHAUSTIVE over the three
#: `RESOURCES_*` constants — asserted by a test, because the whole point of F5's
#: fix is that "measured under emulation" and "nobody recorded" must not render
#: alike, and a map missing a key would silently collapse them into a KeyError or
#: a default.
_RESOURCE_AUTHORITY_BADGE = {
    _RES_AUTHORITATIVE: '<span class="ok">authoritative</span>',
    _RES_EMULATED: '<span class="warn">NOT authoritative</span>',
    _RES_UNRECORDED: '<span class="muted">authority unrecorded</span>',
}
_RESOURCE_AUTHORITY_NOTE = {
    _RES_AUTHORITATIVE: "",
    _RES_EMULATED: (
        "This step ran under CPU emulation (the image's architecture differs from the "
        "host's), so the wall time and CPU figures above are wrong by roughly two orders "
        "of magnitude and the peak RSS is not representative. <b>Do not size "
        "<code>#SBATCH --mem</code> or <code>--time</code> from them.</b> Re-run the step "
        "on hardware matching the image's architecture to get numbers you can budget from."
    ),
    _RES_UNRECORDED: (
        "This step was recorded before the runtime captured whether its resource "
        "measurements were taken natively or under emulation. The numbers above are "
        "therefore of unknown authority — not known-good, and not known-bad."
    ),
}


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
        # The scheduler's verdict, shown alongside the raw State/ExitCode that
        # produced it. Both, deliberately: the verdict is what a reader should
        # act on, and the two columns behind it are what lets them check it —
        # `TIMEOUT | 0:0` looks clean until you know the State outranks the rc.
        verdict = step.get("cluster_job_verdict")
        state, ec = step.get("cluster_state"), step.get("cluster_exit_code")
        detail = f"{_e(job)} on {_e(node or '?')}"
        if state or ec:
            detail += f" — {_e(state or '?')} / exit {_e(ec or '?')}"
        if verdict:
            mark = "✓" if verdict == "succeeded" else "⚠"
            detail += f" — {mark} {_e(verdict)}"
        rows.append(("SLURM job", detail))
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
    # THE STEP'S OWN EXIT CODE. `returncode` was recorded by every run primitive and read
    # by no renderer, for any locus — while this page's own footer asserts that exit codes
    # are part of the machine-observed evidence it shows. A cluster step displayed the
    # SLURM job's `State / exit 0:0`, which is the scheduler's exit, not the tool's; the
    # two can and do differ (a scheduler-killed job reports rc=0, which is why
    # `cluster_job_status` tells you to read `verdict` instead).
    rc = step.get("returncode")
    if isinstance(rc, int):
        cls = "ok" if rc == 0 else "bad"
        title += f' <span class="pill {cls}">exit {rc}</span>'
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
            # Records are keyed by absolute path (pipeline_state.validation_key), because
            # two outputs of one step can share a basename. Show the name prominently and
            # the directory dim beside it: the part that DISTINGUISHES two same-named rows
            # is the directory, so a table that printed only the name would render the
            # collision invisible again, this time in the report.
            head, _, tail = _e(fn).rpartition("/")
            name_cell = (f'<span class="muted">{head}/</span>{tail}' if head else tail)
            rows.append(f"<tr><td>{name_cell}</td><td>{_badge(passed)}</td>"
                        f"<td>{_e(method)}</td></tr>")
        P.append('<div class="tbl-wrap"><table>'
                 '<tr><th>Output</th><th>Validated</th><th>Check</th></tr>'
                 + "".join(rows) + "</table></div>")
    ru = step.get("resource_usage") or {}
    if isinstance(ru, dict) and ru:
        # F5. These three numbers are what a reader sizes `#SBATCH --mem` and
        # `--time` from, and `resource_usage` carries a sibling field whose entire
        # job is to say when they cannot be trusted. Nothing read it: under QEMU
        # emulation the timings are wrong by ~two orders of magnitude and the page
        # presented them flat. The correction was in the same file as the numbers.
        authority = _resource_usage_authority(step)
        P.append('<p class="note">resources: '
                 f'wall {_e(ru.get("wall_seconds"))}s · '
                 f'peak RSS {_e(ru.get("peak_rss_mb"))} MB · '
                 f'CPU {_e(ru.get("max_cpu_percent"))}% · '
                 f'locus {_e(ru.get("locus", "?"))} · {_RESOURCE_AUTHORITY_BADGE[authority]}</p>')
        if authority != _RES_AUTHORITATIVE:
            P.append(f'<p class="warn-note">{_RESOURCE_AUTHORITY_NOTE[authority]}</p>')
    if _run_locus(step) == "cluster":
        P.append(_render_cluster_context(step))
    return "".join(P)


def _run_status_html(spec: dict, failed: list) -> str:
    """The seal's OWN verdict on the run, rendered — with the three states kept apart.

    `derive_pipeline_status` writes `pipeline_status` into every sealed spec, and no
    renderer ever read it. A spec containing a step that exited non-zero carries
    `pipeline_status: "failed"` while its dashboard reported "Steps validated 2/2" under a
    green "✓ validated in shipped image" pill.

    A spec sealed before that field existed carries nothing, and that is UNRECORDED, not
    "passed" — the same rule the I4 usage states follow one row above. So absence renders
    as absence, and a disagreement between the recorded status and what the steps actually
    say is SHOWN rather than silently resolved: if they ever diverge, the reader should
    see both and distrust the artifact, which is the honest outcome.
    """
    from agent.skills.spec_writer import derive_pipeline_status
    stated = (spec.get("pipeline_status") or "").strip()
    derived = derive_pipeline_status(spec.get("pipeline_steps") or [])

    if not stated:
        return '<span class="note">unrecorded — this spec predates the field</span>'
    if stated == derived:
        cls = "bad" if stated == "failed" else "ok"
        return f'<span class="pill {cls}">{_e(stated)}</span>'

    # THEY DISAGREE — SHOW BOTH, AND THIS IS NOT HYPOTHETICAL.
    #
    # `derive_pipeline_status`'s docstring says seal STORES the status and the renderer
    # READS the stored value, "so there is no forked derivation". That is the right design
    # and it is why this cross-check is not a fork: the SAME function is used, to ask
    # whether the stored field still describes the steps beside it.
    #
    # It has to be asked, because the field was introduced to replace "the fabricated
    # `pipeline_status = "in_progress"` default that seal used to stamp into every spec
    # regardless of the run" — and every spec sealed before that fix still carries the
    # fabrication. Measured on the corpus: 4 of 7 sealed specs say `in_progress` while
    # their steps derive `fully_validated`. Rendering the stored value verbatim would have
    # printed "in_progress" across the top of four complete, fully-validated runs — a new
    # falsehood introduced by the fix that was meant to end one.
    #
    # Picking a winner silently is the wrong move in both directions: preferring `stated`
    # ships the stale default, preferring `derived` re-computes a sealed field and hides
    # that the artifact is internally inconsistent. So both are shown and named.
    cls = "bad" if derived == "failed" else "na"
    return (f'<span class="pill {cls}">{_e(derived)}</span> '
            f'<span class="note">— derived from the steps on this page. The sealed record '
            f'says <code>{_e(stated)}</code>, which does not match. A spec sealed before '
            f'`derive_pipeline_status` landed carries a stamped default rather than a '
            f'finding; re-seal to settle it.</span>')


def _render_locus_group(locus: str, steps: list[dict], primary_digest: Optional[str]) -> str:
    P = ['<div class="run-card">']
    any_stale = any(not _digest_state(s, primary_digest)[0] for s in steps)
    n_failed = sum(1 for s in steps if step_validation_failed(s))
    if n_failed:
        # A locus group holding ONLY a failed step was badged "✓ validated here".
        badge = (f'<span class="pill bad">✗ {n_failed} step(s) FAILED here</span>')
    elif any_stale:
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
    whole audit is about.

    ...and the derivation now lives in `core_data.usage_status`, not here. Keeping it
    private to the renderer only shrank the disagreement rather than ending it: the
    markdown guide went on printing the bare bool, so two ARTIFACTS about one workflow
    still disagreed. Same fix as `usage_commands` — one field, one reading, in a leaf."""
    return usage_status(spec)


#: How each I4 state reads in a one-line summary cell.
#: Wording lives in core_data.USAGE_LABELS — see there for why it is not local.
_USAGE_LABEL = USAGE_LABELS


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
    elif status == "unrecorded":
        # NOT the same pill as not_attempted, which is what it used to get. This spec was
        # sealed before the producer was required to state its I4 outcome, so nothing here
        # knows whether the self-test ran. Saying "not attempted" would be a finding
        # invented out of a missing field — and it lands on the panel a reader consults to
        # decide whether the how-to can be trusted.
        tag = '<span class="pill na">self-test outcome UNRECORDED</span>'
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
    elif status == "unrecorded":
        # The reason field does not exist on these specs — that IS the state. Say what the
        # page does not know rather than filling the gap with the nearest verdict.
        P.append('<p class="note"><b>Self-test outcome unrecorded:</b> this workflow was '
                 'sealed before the seal was required to state what I4 concluded, so this '
                 'page cannot tell you whether the command below was self-tested. It is '
                 'neither a pass nor a failure — re-seal to earn a stated outcome.</p>')
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
        # F6. This column read `usage.outputs[*].type` — the AUTHORED field, empty on
        # every spec in the corpus — while the I4 self-test had already resolved and
        # type-validated each of these files. `usage_output_type` prefers the author's
        # word when they gave one and falls back to what the validator actually used;
        # an unfillable cell says so rather than rendering blank, because a declared
        # column that is silently empty reads as "this output has no type".
        def _type_cell(o: dict) -> str:
            t = usage_output_type(spec, o)
            return _e(t) if t else '<span class="muted">unrecorded</span>'

        rows = "".join(
            f'<tr><td>{_e(o.get("name"))}</td>'
            f'<td>{_e(", ".join(o.get("files", []) or []))}</td>'
            f'<td>{_type_cell(o)}</td></tr>' for o in outs)
        P.append('<div class="tbl-wrap"><table>'
                 '<tr><th>Name</th><th>Files</th><th>Type</th></tr>'
                 + rows + '</table></div>')
    P.append(_render_trials(spec, usage, status))
    P.append("</div></div></section>")
    return "".join(P)


def _subs_table(subs: dict, output_slots: list, proven: bool) -> str:
    """Slot → the concrete value, with the ephemeral half named as such.

    The two kinds of substitution have different lifetimes and a reader must not
    confuse them. An INPUT slot holds a durable path to real data — the answer to
    "proven against which genome, which reads", which is the whole reason this table
    exists. An OUTPUT slot holds the fresh scratch dir the self-test created and then
    deleted; showing it beside the others without comment would hand a reader a path
    that no longer exists and invite them to reuse it.

    `output_slots` comes from the record (spec_writer stamps it when it overrides the
    slot), never from re-deriving `_is_output_slot` here."""
    outs = set(output_slots or [])
    rows = []
    for slot in sorted(subs):
        val = subs[slot]
        if slot in outs:
            note = ('<span class="note">the fresh scratch directory the self-test '
                    'created for this trial — already deleted; supply your own '
                    'output directory</span>' if proven else
                    '<span class="note">output directory — filled by the self-test '
                    'at run time</span>')
            rows.append(f'<tr><td>{{{_e(slot)}}}</td><td><code>{_e(val)}</code></td>'
                        f'<td>{note}</td></tr>')
        else:
            rows.append(f'<tr><td>{{{_e(slot)}}}</td><td><code>{_e(val)}</code></td>'
                        f'<td></td></tr>')
    if not rows:
        return ""
    return ('<div class="tbl-wrap"><table>'
            '<tr><th>Placeholder</th><th>Value</th><th></th></tr>'
            + "".join(rows) + "</table></div>")


def _render_trials(spec: dict, usage: dict, status: str) -> str:
    """The input shapes the how-to was tested against — and, when the record has it,
    the LITERAL invocation each one ran.

    This panel used to be a bare list of trial names. That is the least useful half of
    what the seal knows: the self-test resolves every {PLACEHOLDER} to a concrete path
    and executes the result, so `hisat2 -x {OUTPUT_DIR}/idx -1 {R1} -2 {R2}` was
    actually run as a fully-substituted command against a specific genome and specific
    reads — and none of that reached the page a human reads to decide whether to run
    the pipeline on their own data. "Which reference did you prove this against" had no
    answer in the artifact.

    THREE cases, kept apart, because the difference between them is the difference
    between evidence and a plan:

      * PROVEN — `usage_verification.trials` carries the transcript. Every command
        shown is the literal text that ran; the substitutions are the values it ran
        with. This is an observation.
      * DECLARED ONLY — the record has no transcript, but `usage.trials` declares
        substitutions. Those are AUTHORED values. They may be exactly what ran (on a
        verified spec sealed before the transcript was captured) or values nothing ever
        executed (on an unverified one), and this page cannot tell which — so it says
        so instead of picking.
      * NEITHER — nothing to show, said plainly rather than by rendering an empty box.
    """
    # `status` alone, never a `verified` bool passed in beside it: two spellings of one
    # verdict travelling together is how a panel ends up saying "not self-tested" above
    # "Self-tested against 1 shape", which this very function did.
    verified = status == "verified"
    proven = _core_data.usage_proven_trials(spec)
    declared = [t for t in (usage.get("trials") or []) if isinstance(t, dict)]

    if proven:
        n_ok = sum(1 for t in proven if t.get("ok"))
        lead = (f'Self-tested against {len(proven)} input shape(s), '
                f'{n_ok} passing. Each command below is the LITERAL text that was '
                f'executed, with every placeholder resolved — not the template above.')
        P = [f'<p class="note">{_e(lead)}</p>']
        for t in proven:
            ok = bool(t.get("ok"))
            badge = ('<span class="pill ok">✓ ran, outputs validated</span>' if ok
                     else '<span class="pill bad">✗ this trial FAILED</span>')
            # `run-card`, not `how`: these are nested INSIDE the how-to panel, and a
            # `how` inside a `how` doubles the cyan border and the gradient wash into
            # a muddy box-in-a-box. run-card is the neutral nested card the locus
            # groups already use.
            P.append(f'<div class="run-card"><div class="run-title">'
                     f'{_e(t.get("name", "trial"))} {badge}</div>')
            if t.get("description"):
                P.append(f'<p class="note">{_e(t["description"])}</p>')
            cmds = [c for c in (t.get("commands_run") or []) if isinstance(c, str)]
            if cmds:
                P.append('<p class="note"><b>Ran</b></p>')
                if len(cmds) == 1:
                    P.append(f"<pre>{_e(cmds[0])}</pre>")
                else:
                    P.append("<pre>" + "\n".join(f"{i}. {_e(c)}" for i, c
                                                 in enumerate(cmds, 1)) + "</pre>")
            tbl = _subs_table(t.get("substitutions") or {},
                              t.get("output_slots") or [], proven=True)
            if tbl:
                P.append('<p class="note"><b>With</b></p>')
                P.append(tbl)
            prod = [p for p in (t.get("produced_files") or []) if isinstance(p, str)]
            if prod:
                P.append('<p class="note"><b>Produced</b> '
                         '<span class="note">(relative to the scratch dir; each was '
                         'type-validated)</span></p><ul class="foot">')
                P.extend(f"<li><code>{_e(p)}</code></li>" for p in prod)
                P.append("</ul>")
            P.append("</div>")
        return "".join(P)

    if declared:
        # Guarded by `verified`. This line used to render unconditionally, so a dashboard
        # could say "not self-tested" and "Self-tested against 1 declared input shape(s)"
        # in the same panel — cluster_refdata_validation did exactly that.
        if verified:
            lead = (f'Self-tested against {len(declared)} declared input shape(s). The '
                    f'values below are what the author DECLARED for the self-test; this '
                    f'spec was sealed before the exact invocation was recorded, so this '
                    f'page cannot show the transcript of what ran. Re-seal to capture it.')
        else:
            lead = (f'{len(declared)} declared input shape(s) — NOT self-tested (see '
                    f'above). Nothing below was executed; these are authored values.')
        P = [f'<p class="note">{_e(lead)}</p>']
        for t in declared:
            P.append(f'<div class="run-card"><div class="run-title">'
                     f'{_e(t.get("name", "trial"))}</div>')
            if t.get("description"):
                P.append(f'<p class="note">{_e(t["description"])}</p>')
            subs = t.get("substitutions") if isinstance(t.get("substitutions"), dict) else {}
            tbl = _subs_table(subs, [], proven=False)
            P.append(tbl or _empty("(no substitutions declared for this trial)"))
            P.append("</div>")
        return "".join(P)

    if status == "verified":
        # The inferred-trial path: no `usage.trials` was authored, so the self-test built
        # one from pipeline_steps[*].inputs and ran it. Nothing on an old spec records
        # what it chose. Absence, stated — the alternative is a verified how-to whose page
        # is silent about ever having been tested with anything.
        return ('<p class="note">The self-test passed, but this spec records neither '
                'declared trials nor the invocation that ran — it was sealed before the '
                'transcript was captured, so what the how-to was proven against is '
                '<b>unrecorded</b> here. Re-seal to record it.</p>')
    return ""


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
    # RUNTIME CONFIGS — the fourth member of I8's external universe, and the only one
    # this page never showed. `_walk_input_provenance` widens the traceable universe
    # with `runtime_configs[*].path`, so a step consuming an Exomiser analysis YAML or a
    # GATK interval list seals cleanly on the strength of a record the reader could not
    # see. That is the same shape as the how-to gap above: the seal knows, the artifact
    # does not say. And a config is not incidental — it carries the HPO terms, the
    # filters, the memory settings; "review all the parameters before running on real
    # data" is unanswerable without it.
    cfgs = [c for c in (spec.get("runtime_configs") or []) if isinstance(c, dict)]
    td = spec.get("test_data") if isinstance(spec.get("test_data"), dict) else None
    if not (rdbs or arts or td or cfgs):
        return ""
    P = ['<section class="bx">']
    P.append('<h2>Inputs &amp; external sources '
             '<span class="note">what the validated run consumed (I8 provenance)</span></h2>')
    P.append('<div class="bx-body">')
    if td:
        # Paths via the leaf — this list used to be a FOURTH hand-spelling of the
        # test_data key set, and the panel showed a bare path beside reference DBs and
        # authored artifacts that show their sha256, so unpinned data read as pinned.
        paths = _core_data.test_data_paths(td)
        if paths:
            anchors = _core_data.test_data_anchors(td)
            status = ((spec.get("test_data_integrity") or {}).get("status")
                      if isinstance(spec.get("test_data_integrity"), dict) else None)
            note = {"verified": "re-verified at seal against the bytes selected",
                    "unanchored": "NOT content-anchored — these paths were recorded "
                                  "before anchoring existed, so only their presence is proven",
                    "diverged": "CONTENT DIVERGED from what was selected"}.get(status or "", "")
            P.append('<h3 class="sub">Test data'
                     + (f' <span class="note">{_e(note)}</span>' if note else "") + "</h3>")
            rows = []
            for k, raw in sorted(paths.items()):
                sha = (anchors.get(k) or {}).get("sha256")
                kind = (anchors.get(k) or {}).get("kind")
                pin = (f'<code>{_e(sha[:19])}…</code>' if sha
                       else ("directory (no single hash)" if kind == "directory"
                             else "<em>not anchored</em>"))
                rows.append(f'<tr><td>{_e(k)}</td><td><code>{_e(raw)}</code></td>'
                            f'<td>{pin}</td></tr>')
            P.append('<div class="tbl-wrap"><table>'
                     '<tr><th>Slot</th><th>Path</th><th>sha256</th></tr>'
                     + "".join(rows) + "</table></div>")
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
    if cfgs:
        # THE COLUMN SAYS WHAT IT IS. Every other sha256 on this page was verified by a
        # seal-side gate — authored_artifacts are re-hashed by I8, reference_databases by
        # I5. A runtime_config's hash is neither: nothing at seal reads it. It is an
        # anchor the producer recorded which `run_production_pipeline`'s data-pin check
        # compares BEFORE a production run. That is a real guarantee and a different one,
        # and a column that looked identical to the two above it would borrow their
        # meaning. These entries are also agent-authored (patch_pipeline's allowlist), so
        # the whole row is a claim until that later check runs.
        P.append('<h3 class="sub">Runtime configuration '
                 '<span class="note">files the tools read at run time — part of the '
                 'I8 traceable universe, so a step may legitimately consume one. '
                 'Agent-authored; the hashes below are NOT verified at seal (unlike '
                 'authored artifacts and reference DBs above) — they are anchors '
                 'run_production_pipeline re-checks before a production run</span>'
                 '</h3>')
        rows = []
        for c in cfgs:
            sha = c.get("sha256") or ""
            # `sha256` is optional on RuntimeConfig, unlike on an authored artifact.
            # Absent means unpinned, and it says so — rendering an empty cell beside
            # the pinned rows above would read as pinned at a glance.
            pin = (f'<code>{_e(sha[:19])}…</code>' if sha else "<em>not pinned</em>")
            rows.append(f'<tr><td>{_e(c.get("name",""))}</td>'
                        f'<td>{_e(c.get("format",""))}</td>'
                        f'<td><code>{_e(c.get("path",""))}</code></td>'
                        f'<td>{pin}</td></tr>')
        P.append('<div class="tbl-wrap"><table>'
                 '<tr><th>Name</th><th>Format</th><th>Path</th><th>sha256</th></tr>'
                 + "".join(rows) + "</table></div>")
        # The inline snapshot, when the producer took one. A reader reviewing parameters
        # before committing an allocation should not have to go find the file — and on a
        # cluster run they often cannot, because it lives at a locus they have no shell on.
        for c in cfgs:
            content = c.get("content")
            if isinstance(content, str) and content.strip():
                P.append(f'<p class="note"><b>{_e(c.get("name",""))}</b> — recorded '
                         f'contents</p><pre>{_e(content)}</pre>')
    P.append("</div></section>")
    return "".join(P)


# ---------------------------------------------------------------------------
# Runtime prerequisites — the services that had to be up (I10).
# ---------------------------------------------------------------------------

def _render_services(spec: dict) -> str:
    """Services this workflow depends on, with the health evidence I10 gated on.

    Rendered because a service dependency is a PREREQUISITE, not an input: a reader
    who stages the image, uploads the data and submits the job still has nothing if
    the pipeline needs Redis on :6379 and nobody started it. I10 refuses a seal for a
    service that never showed a healthy probe, so a sealed spec's services did come up
    — but which ones, on which ports, started how, was recorded and never shown.

    The healthy/total split comes from `core_data.service_healthy_probes`, the same
    leaf I10 refuses on. A service with zero healthy probes cannot pass the seal, so
    the ✗ badge should be unreachable on any spec sealed since I10 was restored; it is
    rendered anyway rather than assumed away, because an older artifact predates the
    gate and this page is where a reader would find out."""
    svcs = [s for s in (spec.get("service_dependencies") or []) if isinstance(s, dict)]
    if not svcs:
        return ""
    P = ['<section class="bx">']
    P.append('<h2>Runtime prerequisites '
             '<span class="note">background services that must be running — each was '
             'observed healthy during the run (I10)</span></h2>')
    P.append('<div class="bx-body">')
    for s in svcs:
        probes = _core_data.service_probe_log(s)
        healthy = _core_data.service_healthy_probes(s)
        if healthy:
            badge = (f'<span class="pill ok">✓ observed healthy '
                     f'({len(healthy)}/{len(probes)} probe(s))</span>')
        else:
            badge = (f'<span class="pill bad">✗ never observed healthy '
                     f'({len(probes)} probe(s), 0 healthy)</span>')
        name = s.get("name") or s.get("service_name") or "service"
        title = _e(name)
        if s.get("version"):
            title += f' <span class="note">{_e(s["version"])}</span>'
        P.append(f'<div class="run-card"><div class="run-title">{title} {badge}</div>')
        rows: list[tuple[str, str]] = [
            ("Type", _e(s.get("type", ""))),
            ("Port", _e(s.get("port")) if s.get("port") else ""),
            # `status` is the LIFECYCLE, distinct from the badge above: "stopped" beside
            # a green badge is the normal, correct end state for a run that finished —
            # the badge asks whether it ever came up, this says where it ended.
            ("Lifecycle", _e(s.get("status", ""))),
            ("Start", f'<pre>{_e(s["start_command"])}</pre>' if s.get("start_command") else ""),
            ("Health check",
             f'<pre>{_e(s["health_check_command"])}</pre>'
             if s.get("health_check_command") else ""),
            ("Stop", f'<pre>{_e(s["stop_command"])}</pre>' if s.get("stop_command") else ""),
        ]
        env_vars = s.get("env_vars") if isinstance(s.get("env_vars"), dict) else {}
        if env_vars:
            rows.append(("Environment",
                         ", ".join(f'<code>{_e(k)}={_e(v)}</code>'
                                   for k, v in sorted(env_vars.items()))))
        P.append(_kv_table(rows))
        if probes:
            # The probe log itself. This is the machine-observed half — the one thing on
            # the panel the agent cannot author — so it is shown rather than summarized
            # into the badge alone.
            prows = "".join(
                f'<tr><td>{_e(p.get("timestamp",""))}</td>'
                f'<td>{_badge(_core_data.probe_is_healthy(p))}</td>'
                f'<td>{_e(p.get("returncode"))}</td>'
                f'<td><code>{_e(p.get("command",""))}</code></td></tr>' for p in probes)
            P.append('<div class="tbl-wrap"><table>'
                     '<tr><th>Probed at</th><th>Healthy</th><th>rc</th><th>Command</th></tr>'
                     + prows + '</table></div>')
        P.append("</div>")
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
    # VALIDATED AND NOT FAILED. `step_is_validated` asks whether validation RECORDS
    # exist; it is not the negation of failure, and a step that exited non-zero satisfies
    # it. The header counted such a step toward "Steps validated N/M", so a run containing
    # a failure reported 2/2. See core_data.step_validation_failed.
    failed = [st for st in steps if step_validation_failed(st)]
    validated = [st for st in steps
                 if step_is_validated(st) and not step_validation_failed(st)]
    loci = [locus for locus in _LOCUS_ORDER
            if any(_run_locus(st) == locus for st in steps)]
    # Primary digest for stale-detection MUST be a bare image digest (sha256:…),
    # the same shape a step stamps as container_image_digest. env_image is a
    # repo@sha256:… REF (different shape) — never use it here or every local step
    # would false-flag stale. Absent a real digest we compare nothing (unknown →
    # current; we don't fabricate a mismatch we can't prove).
    primary_digest = (env_record or {}).get("image_digest") or None

    shipped = bool(s.get("validated_in_shipped_image"))
    if failed:
        # A FAILED STEP OUTRANKS EVERY OTHER HEADLINE. `validated_in_shipped_image` was
        # earned legitimately — the digests really do match — but it answers "did this run
        # in the image we ship", not "did it work", and the page presented it as the
        # verdict. A reader deciding whether to run this on real data must not have to
        # find one ✗ glyph in a per-output table below the fold.
        pill = (f'<span class="pill bad">✗ {len(failed)} step(s) FAILED — '
                f'do not run this as-is</span>')
    elif shipped:
        pill = '<span class="pill ok">✓ validated in shipped image</span>'
    elif steps:
        pill = '<span class="pill na">not shipped-image verified</span>'
    else:
        pill = '<span class="pill na">no runs recorded</span>'

    head_rows = [
        ("Sealed", _e(s.get("created_at", "—"))),
        ("Validated on", ", ".join(_e(x) for x in loci) if loci else "—"),
        # STATED BY THE SEAL, NEVER RENDERED. `derive_pipeline_status` computes this
        # correctly and writes it into the spec — a spec containing a failed step carries
        # `pipeline_status: failed` — and no renderer read the field. The record knew; the
        # view did not say. One line, and it is the most load-bearing byte on the page.
        ("Run status", _run_status_html(s, failed)),
        ("Steps validated", f"{len(validated)}/{len(steps)}"
                            + (f' <span class="pill bad">{len(failed)} FAILED</span>'
                               if failed else "")),
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
    # BEFORE the inputs, because it is a precondition rather than a consumable: if the
    # service isn't up, the inputs are moot.
    services = _render_services(s)
    if services:
        P.append(services)
    inputs = _render_inputs(s)
    if inputs:
        P.append(inputs)
    # HONEST PROVENANCE. This used to claim "no field on this page was authored by the
    # agent", which is false and was false when written: the description, the usage
    # description, and the command_template are all rendered here and all sit in
    # patch_pipeline's agent-authored allowlist (CLAUDE.md). A page that overstates its own
    # purity is the same defect class it exists to prevent — so it now says which parts are
    # machine-observed and which are authored, and lets the reader weigh them differently.
    # HONEST PROVENANCE, AND THE LIST HAS TO KEEP UP WITH THE PAGE. This paragraph
    # enumerates which side of the line each thing on the dashboard falls, so it goes
    # stale the moment a panel is added and not accounted for — which is the same
    # failure it was rewritten to fix (it used to claim NOTHING here was authored,
    # while rendering three fields from patch_pipeline's allowlist). `runtime_configs`
    # is agent-authored and now rendered; the self-test transcript and the service
    # health probes are runtime-captured and now rendered. Both sides updated together.
    P.append('<p class="gen">Generated deterministically from the sealed WorkflowSpec. '
             'The <b>evidence</b> — commands run, exit codes, outputs, validations, '
             'digests, resource usage, the self-test transcript (what each trial '
             'actually executed, and with which files), and the service health probes — '
             'is machine-observed and cannot be authored by the agent. The '
             '<b>agent-authored</b> parts — this workflow\'s description, the how-to '
             'description, the command template itself, and the runtime configuration '
             'entries — are in patch_pipeline\'s allowlist; the how-to\'s self-test '
             'status above says whether that command was actually executed. The env '
             'report (Layer 1) is a separate, immutable page.</p>')
    P.append(_close_page())
    return "\n".join(P)
