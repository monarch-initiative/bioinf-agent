"""
ReportBuilder — generates a self-contained HTML pipeline report from a saved spec dict.
Called automatically by spec_writer.save_pipeline_spec after every install.
Expects spec dicts conforming to the PipelineSpec model (agent/models/core_data.py).
"""

import re
from datetime import datetime
from pathlib import Path


def _primary_packages(spec: dict) -> list[dict]:
    """Identify the primary tool(s) for this pipeline, fully derived from the spec.

    A package is "primary" if its name (case-insensitive, with r-/bioconductor- prefix
    stripped) appears as a whole-word token in any of:
      - usage.command_template
      - usage.example
      - pipeline_steps[*].tool
      - pipeline_steps[*].subcommand
      - pipeline_steps[*].command

    This is fully programmatic — no curated "infrastructure" list, no hardcoded tool
    names. A pipeline that legitimately uses curl as a primary tool (e.g. a download
    pipeline) gets curl as primary; one that uses it only to fetch a JAR during
    install does not (because curl isn't mentioned in pipeline_steps or usage).

    Multiple primaries are returned in spec-order — a wgs_pipeline with bwa+samtools+
    freebayes invoked in pipeline_steps surfaces all three. The caller (the report)
    decides how to render them.
    """
    haystack_parts: list[str] = []
    usage = spec.get("usage") or {}
    if isinstance(usage, dict):
        haystack_parts.append(usage.get("command_template") or "")
        haystack_parts.append(usage.get("example") or "")
    for s in spec.get("pipeline_steps", []) or []:
        if not isinstance(s, dict):
            continue
        haystack_parts.append(s.get("tool") or "")
        haystack_parts.append(s.get("subcommand") or "")
        haystack_parts.append(s.get("command") or "")
    haystack = "\n".join(haystack_parts).lower()
    if not haystack.strip():
        return []

    primaries: list[dict] = []
    seen: set[str] = set()
    for p in spec.get("packages", []) or []:
        if not isinstance(p, dict):
            continue
        raw_name = (p.get("name") or "").strip()
        if not raw_name or raw_name in seen:
            continue
        nm = raw_name.lower()
        # Generate match candidates: the bare name + the un-prefixed form for r-* / bioconductor-*.
        candidates = {nm}
        for prefix in ("r-", "bioconductor-"):
            if nm.startswith(prefix):
                candidates.add(nm[len(prefix):])
        # Whole-word match — avoid r-curl matching "curl" or "ape" matching "shape".
        if any(
            c and len(c) >= 2 and re.search(rf"(?<![A-Za-z0-9_]){re.escape(c)}(?![A-Za-z0-9_])", haystack)
            for c in candidates
        ):
            primaries.append(p)
            seen.add(raw_name)
    return primaries


_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #f5f7fa; color: #1a1a2e; line-height: 1.6; }
.page { max-width: 960px; margin: 0 auto; padding: 2rem 1.5rem; }
header { background: linear-gradient(135deg, #16213e 0%, #0f3460 100%);
         color: #fff; border-radius: 12px; padding: 2rem 2.5rem; margin-bottom: 2rem; }
header h1 { font-size: 2rem; font-weight: 700; letter-spacing: -0.5px; }
header .meta { margin-top: 0.5rem; opacity: 0.8; font-size: 0.9rem; }
header .status-row { margin-top: 1rem; display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
header .status-label { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.08em; opacity: 0.7; margin-right: 0.4rem; }
header .badge { display: inline-block; background: rgba(255,255,255,0.15);
                border-radius: 20px; padding: 2px 12px; font-size: 0.8rem; }
.badge.pass  { background: #22c55e33; color: #16a34a; border: 1px solid #16a34a55; }
.badge.fail  { background: #ef444433; color: #dc2626; border: 1px solid #dc262655; }
.badge.skip  { background: #f59e0b33; color: #d97706; border: 1px solid #d9770655; }
.badge.partial { background: #3b82f633; color: #2563eb; border: 1px solid #2563eb55; }
.legend { background: #fff; border-radius: 10px; padding: 1rem 1.5rem; margin-bottom: 1.5rem;
          box-shadow: 0 1px 4px rgba(0,0,0,0.07); font-size: 0.85rem; }
.legend h3 { font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.06em;
             color: #475569; margin-bottom: 0.6rem; font-weight: 600; }
.legend dl { display: grid; grid-template-columns: max-content 1fr; gap: 0.4rem 0.9rem; }
.legend dt { font-weight: 600; color: #1e293b; }
.legend dd { color: #475569; }
.section { background: #fff; border-radius: 10px; padding: 1.5rem 2rem;
           margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,0.07); }
.section h2 { font-size: 1.15rem; font-weight: 600; color: #0f3460;
              border-bottom: 2px solid #e2e8f0; padding-bottom: 0.5rem; margin-bottom: 1rem; }
.self-test { border-radius: 6px; padding: 0.65rem 0.9rem; margin: 0.75rem 0;
             font-size: 0.85rem; }
.self-test.passed { background: #f0fdf4; border-left: 4px solid #16a34a; }
.self-test.failed { background: #fffbeb; border-left: 4px solid #d97706; }
.primary-docs { background: #f0f9ff; border-left: 4px solid #0ea5e9; border-radius: 6px;
                padding: 0.75rem 1rem; margin-bottom: 1rem; }
.primary-docs strong { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em;
                       color: #0369a1; margin-right: 0.75rem; }
.primary-docs .primary-doc { display: inline-block; background: #fff; border: 1px solid #0ea5e933;
                             border-radius: 16px; padding: 4px 12px; margin: 2px 4px;
                             font-size: 0.9rem; color: #0369a1; text-decoration: none; font-weight: 500; }
.primary-docs .primary-doc:hover { background: #0ea5e9; color: #fff; }
.primary-docs .primary-doc-nolink { color: #64748b; background: #f8fafc; }
.component-docs { margin-top: 1rem; font-size: 0.85rem; color: #475569; }
.component-docs summary { cursor: pointer; font-weight: 500; padding: 0.4rem 0; }
.component-docs ul { margin: 0.4rem 0 0 1.4rem; }
table { width: 100%; border-collapse: collapse; font-size: 0.9rem; }
th { background: #f1f5f9; text-align: left; padding: 0.55rem 0.8rem;
     font-weight: 600; color: #475569; border-bottom: 2px solid #e2e8f0; }
td { padding: 0.5rem 0.8rem; border-bottom: 1px solid #f1f5f9; vertical-align: top; }
tr:last-child td { border-bottom: none; }
tr:hover td { background: #f8fafc; }
pre { background: #1e293b; color: #e2e8f0; border-radius: 8px; padding: 1rem 1.2rem;
      font-size: 0.85rem; overflow-x: auto; line-height: 1.5; white-space: pre-wrap;
      word-break: break-all; margin-top: 0.5rem; }
.kv { display: grid; grid-template-columns: 160px 1fr; gap: 0.3rem 1rem; font-size: 0.9rem; }
.kv .key { color: #64748b; font-weight: 500; }
.kv .val { color: #1e293b; }
a { color: #0f3460; text-decoration: none; border-bottom: 1px solid #bfdbfe; }
a:hover { color: #1d4ed8; }
.step-num { display: inline-flex; align-items: center; justify-content: center;
            width: 26px; height: 26px; background: #0f3460; color: #fff;
            border-radius: 50%; font-size: 0.8rem; font-weight: 700; margin-right: 0.6rem;
            flex-shrink: 0; }
.step-header { display: flex; align-items: center; font-weight: 600;
               font-size: 1rem; margin-bottom: 0.6rem; }
.step-block { border: 1px solid #e2e8f0; border-radius: 8px; padding: 1rem 1.2rem;
              margin-bottom: 1rem; }
.step-block:last-child { margin-bottom: 0; }
.val-row { display: flex; align-items: center; gap: 0.5rem; font-size: 0.88rem;
           padding: 0.25rem 0; }
.io-group { display: flex; flex-direction: column; gap: 0.75rem; margin: 0.75rem 0 0.25rem; }
.io-block { min-width: 180px; }
.io-block .io-label { font-size: 0.78rem; font-weight: 600; text-transform: uppercase;
                      letter-spacing: 0.06em; color: #64748b; margin-bottom: 0.35rem; }
.io-file { display: flex; align-items: center; gap: 0.4rem; font-size: 0.85rem;
           padding: 0.18rem 0; color: #1e293b; }
.io-file code { background: #f1f5f9; border-radius: 4px; padding: 1px 6px; }
.io-ref  { display: flex; align-items: center; gap: 0.35rem; font-size: 0.8rem;
           padding: 0.1rem 0 0.1rem 1.6rem; color: #64748b; }
.io-ref code { background: #f1f5f9; border-radius: 4px; padding: 1px 5px; color: #475569; }
.io-ref::before { content: "↳"; color: #94a3b8; margin-right: 0.15rem; }
.io-size { font-size: 0.78rem; color: #94a3b8; }
footer { text-align: center; font-size: 0.8rem; color: #94a3b8; margin-top: 2rem; }
/* Scrollable description cells for long package/tool blurbs */
.desc-scroll { max-height: 4.5rem; overflow-y: auto; padding-right: 0.4rem;
               font-size: 0.85rem; line-height: 1.45;
               scrollbar-width: thin; }
.desc-scroll::-webkit-scrollbar { width: 6px; }
.desc-scroll::-webkit-scrollbar-thumb { background: #cbd5e1; border-radius: 3px; }
.desc-scroll::-webkit-scrollbar-track { background: transparent; }
/* Install step layout: command on left, packages on right */
.install-row { display: grid; grid-template-columns: 1fr 280px; gap: 1rem;
               padding: 0.9rem 0; border-bottom: 1px solid #f1f5f9; align-items: start; }
.install-row:last-child { border-bottom: none; }
.install-row .cmd { font-size: 0.85rem; }
.install-row .cmd-header { display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.35rem; }
.install-row .cmd pre { font-size: 0.78rem; margin-top: 0.3rem; max-height: 7rem; overflow-y: auto; }
.install-row .pkgs { background: #f8fafc; border-radius: 6px; padding: 0.5rem 0.7rem;
                     font-size: 0.8rem; max-height: 9rem; overflow-y: auto; }
.install-row .pkgs .label { font-weight: 600; color: #475569; font-size: 0.72rem;
                            text-transform: uppercase; letter-spacing: 0.06em;
                            margin-bottom: 0.3rem; display: block; }
.install-row .pkgs ul { list-style: none; padding: 0; margin: 0; }
.install-row .pkgs li { padding: 0.15rem 0; color: #1e293b; }
.install-row .pkgs li code { background: #fff; border-radius: 3px; padding: 0 4px; }
.install-row .pkgs .none { color: #94a3b8; font-style: italic; }
"""


def _badge(text: str, kind: str = "pass") -> str:
    return f'<span class="badge {kind}">{text}</span>'


_STATUS_BADGE_MAP = {
    "fully_validated":     ("✓ Fully Validated",     "pass"),
    "partially_validated": ("◐ Partially Validated", "partial"),
    "complete":            ("✓ Complete",            "pass"),
    "in_progress":         ("⏳ In Progress",         "skip"),
    "failed":              ("✗ Failed",              "fail"),
    "timeout":             ("✗ Timeout",             "fail"),
}


def _status_badge(status: str) -> str:
    if status in _STATUS_BADGE_MAP:
        text, kind = _STATUS_BADGE_MAP[status]
        return _badge(text, kind)
    if status:
        return _badge(status, "skip")
    return _badge("Unknown", "skip")


def _status_legend() -> str:
    """Top-of-page legend explaining what each status code means."""
    items = [
        ("✓ Fully Validated",
         "Env: every package was verify_command-checked. Pipeline: every step's outputs passed validate_output."),
        ("◐ Partially Validated",
         "Env: some packages verified, others not. Pipeline: some steps validated, others ran clean but were not validated."),
        ("✓ Complete",
         "All commands ran cleanly (returncode 0), but no validation/verification was recorded."),
        ("✗ Failed",
         "At least one command had a non-zero exit, or an output validation explicitly failed."),
    ]
    rows = "".join(f"<dt>{name}</dt><dd>{desc}</dd>" for name, desc in items)
    return f"""
<div class="legend">
  <h3>Status codes</h3>
  <dl>{rows}</dl>
</div>"""


def _runtime_env_section(spec: dict) -> str:
    re = spec.get("runtime_environment")
    if not re:
        return ""
    env_type = re.get("type", "conda")
    gpu_required = bool(re.get("gpu_required"))
    cuda_version = re.get("cuda_version")
    # Render whenever there's content worth showing: non-conda type, GPU
    # requirement, CUDA pin, or any of the resource / wrapper fields.
    has_content = (
        env_type != "conda"
        or gpu_required
        or cuda_version
        or re.get("java_flags") or re.get("jar_path") or re.get("wrapper_script")
        or re.get("docker_image") or re.get("min_ram_gb") or re.get("min_cpu")
    )
    if not has_content:
        return ""
    fields = [("Type", env_type)]
    if gpu_required:
        fields.append(("GPU required", "Yes"))
    if cuda_version:
        fields.append(("CUDA version", f"<code>{cuda_version}</code>"))
    if re.get("java_flags"):
        fields.append(("JVM flags", f"<code>{' '.join(re['java_flags'])}</code>"))
    if re.get("jar_path"):
        fields.append(("JAR path", f"<code>{re['jar_path']}</code>"))
    if re.get("wrapper_script"):
        fields.append(("Wrapper script", f"<code>{re['wrapper_script']}</code>"))
    if re.get("docker_image"):
        fields.append(("Docker image", f"<code>{re['docker_image']}</code>"))
    if re.get("min_ram_gb"):
        fields.append(("Min RAM", f"{re['min_ram_gb']} GB"))
    if re.get("min_cpu"):
        fields.append(("Min CPUs", str(re["min_cpu"])))
    kv = "\n".join(
        f'<div class="key">{k}</div><div class="val">{v}</div>' for k, v in fields
    )
    icon = "🎮" if gpu_required else "☕"
    return f"""
<div class="section">
  <h2>{icon} Runtime Environment</h2>
  <div class="kv">{kv}</div>
</div>"""


def _service_dependencies_section(spec: dict) -> str:
    deps = spec.get("service_dependencies") or []
    if not deps:
        return ""
    rows = []
    for d in deps:
        if not isinstance(d, dict):
            continue
        rows.append(
            f"<tr><td><strong>{d.get('name','')}</strong></td>"
            f"<td>{d.get('type','')}</td>"
            f"<td>{d.get('version') or '—'}</td>"
            f"<td>{d.get('port') or '—'}</td>"
            f"<td><code style='font-size:0.78rem'>{d.get('start_command') or '—'}</code></td>"
            f"<td><code style='font-size:0.78rem'>{d.get('health_check_command') or '—'}</code></td></tr>"
        )
    return f"""
<div class="section">
  <h2>🔌 Service Dependencies</h2>
  <p style="font-size:0.85rem;color:#475569;margin-bottom:0.75rem">
    Companion processes that must be running before the pipeline executes
    (web servers, databases, Spark drivers, …). Managed via the
    <code>start_service</code> / <code>stop_service</code> /
    <code>check_service_health</code> tools.
  </p>
  <table>
    <thead><tr><th>Name</th><th>Type</th><th>Version</th><th>Port</th><th>Start</th><th>Health check</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>"""


def _reference_databases_section(spec: dict) -> str:
    dbs = spec.get("reference_databases", [])
    if not dbs:
        return ""
    rows = []
    for db in dbs:
        size = f"{db.get('size_gb', 0):.1f} GB" if db.get("size_gb") else "—"
        coupled = db.get("coupled_to_version") or "—"
        local = db.get("local_path") or "—"
        avail = "✅" if db.get("available") else "❌"
        rows.append(
            f"<tr><td><strong>{db.get('name','')}</strong></td>"
            f"<td>{db.get('version','')}</td>"
            f"<td>{size}</td>"
            f"<td>{avail}</td>"
            f"<td>{coupled}</td>"
            f"<td><code style='font-size:0.8rem'>{local}</code></td></tr>"
        )
    return f"""
<div class="section">
  <h2>🗄️ Reference Databases</h2>
  <p style="font-size:0.9rem;color:#475569;margin-bottom:0.75rem">
    Mounted at runtime — not baked into the Docker image.
  </p>
  <table>
    <thead><tr><th>Database</th><th>Version</th><th>Size</th><th>Available</th><th>Coupled to version</th><th>Local path</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>"""


def _docker_section(spec: dict) -> str:
    docker = spec.get("docker")
    if not docker:
        return ""
    attempted = docker.get("build_attempted", False)
    success = docker.get("build_success", False)
    tag = docker.get("image_tag") or "—"
    registry = docker.get("registry", "local")
    reason = docker.get("reason", "")

    rows = [
        ("Build attempted", "Yes" if attempted else "No"),
        ("Build success", "Yes" if success else "No"),
        ("Image tag", f"<code>{tag}</code>" if tag != "—" else "—"),
        ("Registry", registry),
    ]
    if docker.get("nvidia_runtime"):
        rows.append(("NVIDIA runtime", "Yes — run with <code>--gpus all</code> (Docker) or <code>--nv</code> (Singularity)"))
    if docker.get("volume_mounts"):
        mounts_html = "<br>".join(f"<code>{m}</code>" for m in docker["volume_mounts"])
        rows.append(("Volume mounts", mounts_html))
    if docker.get("runtime_data_env"):
        rows.append(("Data dir env var", f"<code>{docker['runtime_data_env']}</code>"))
    if reason:
        rows.append(("Notes", reason))

    kv = "\n".join(
        f'<div class="key">{k}</div><div class="val">{v}</div>' for k, v in rows
    )
    return f"""
<div class="section">
  <h2>🐳 Docker Image</h2>
  <div class="kv">{kv}</div>
</div>"""


def _packages_table(spec: dict) -> str:
    packages = [p for p in spec.get("packages", []) if p.get("name") != "conda-pack"]
    if not packages:
        return ""
    rows = []
    for p in packages:
        hp = p.get("homepage", "")
        link = f'<a href="{hp}" target="_blank">{hp}</a>' if hp else "—"
        conda_spec = p.get("conda_spec", "")
        conda_ver = conda_spec.split("=")[-1] if "=" in conda_spec else ""
        version = p.get("resolved_version") or p.get("version") or conda_ver or "—"
        runtime_ver = (p.get("runtime_version") or "").strip()
        version_cell = (
            f'{version}<br><span style="font-size:0.75rem;color:#64748b">runtime: {runtime_ver}</span>'
            if runtime_ver else version
        )
        desc = (p.get("description") or "").strip()
        desc_html = f'<div class="desc-scroll">{desc}</div>' if desc else "—"
        rows.append(
            f"<tr><td><strong>{p.get('name','')}</strong></td>"
            f"<td>{version_cell}</td>"
            f"<td>{p.get('channel','')}</td>"
            f"<td>{desc_html}</td>"
            f"<td>{link}</td></tr>"
        )
    return f"""
<div class="section">
  <h2>📦 Packages</h2>
  <p style="font-size:0.85rem;color:#475569;margin-bottom:0.5rem">
    Derived from <code>conda list</code> + non-conda install steps at finalize-time.
    For R packages where the conda recipe version differs from <code>packageVersion()</code>,
    both are shown.
  </p>
  <table>
    <thead><tr><th>Package</th><th>Version</th><th>Channel</th><th>Description</th><th>Documentation</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>"""


def _test_data_section(spec: dict) -> str:
    td = spec.get("test_data")
    if not td:
        return ""

    assay = td.get("assay_type", "")
    end = td.get("end_type", "")
    read_t = td.get("read_type", "")
    subset = td.get("subset", "")
    parts = [p for p in [read_t, end, assay] if p]
    type_display = " / ".join(parts) if parts else "—"
    if subset:
        num = td.get("num_reads")
        type_display += f"  ({num:,} reads)" if isinstance(num, int) else f"  ({subset})"

    sample = td.get("sample", "")
    accession = td.get("accession", "")
    dataset = " / ".join(p for p in [sample, accession] if p) or "—"

    fields = [
        ("Dataset", dataset),
        ("Type", type_display),
        ("Genome build", td.get("genome_build", "—")),
    ]
    if td.get("chromosome_subset"):
        fields.append(("Chromosome", td["chromosome_subset"]))
    upstream = td.get("upstream_pipelines") or []
    if upstream:
        fields.append(("Upstream pipelines", ", ".join(upstream)))
    for fk in ("r1", "r2"):
        if td.get(fk):
            fields.append((fk.upper(), f"<code>{Path(td[fk]).name}</code>"))

    kv = "\n".join(
        f'<div class="key">{k}</div><div class="val">{v}</div>' for k, v in fields
    )
    return f"""
<div class="section">
  <h2>🧬 Test Data</h2>
  <div class="kv">{kv}</div>
</div>"""


def _io_group(step: dict) -> str:
    inputs = step.get("inputs", [])
    outputs = step.get("outputs", [])
    validation = step.get("validation") or {}
    if not inputs and not outputs:
        return ""

    def _input_rows(entries: list) -> str:
        """Inputs may be plain strings or {path, references} dicts."""
        html = ""
        for e in entries:
            if isinstance(e, dict):
                path = e.get("path", "")
                refs = e.get("references", []) or []
            else:
                path, refs = str(e), []
            name = Path(path).name if path else ""
            html += f'<div class="io-file">📄 <code>{name}</code></div>'
            for r in refs:
                rname = Path(r).name
                html += f'<div class="io-ref"><code>{rname}</code></div>'
        return html

    def _output_rows(files: list) -> str:
        html = ""
        for f in files:
            name = Path(f).name
            vr = validation.get(f) or validation.get(name) or {}
            passed = vr.get("passed")
            size = vr.get("size_bytes", 0)
            size_str = f'<span class="io-size">{size / 1024:.1f} KB</span>' if size else ""
            if passed is True:
                icon = "✅"
            elif passed is False:
                icon = "❌"
            else:
                icon = "🔲"
            html += f'<div class="io-file">{icon} <code>{name}</code> {size_str}</div>'
        return html

    in_html = out_html = ""
    if inputs:
        in_html = f'<div class="io-block"><div class="io-label">Inputs</div>{_input_rows(inputs)}</div>'
    if outputs:
        out_html = f'<div class="io-block"><div class="io-label">Outputs</div>{_output_rows(outputs)}</div>'

    return f'<div class="io-group">{in_html}{out_html}</div>'


def _install_steps_section(spec: dict) -> str:
    """Render install_steps in chronological order (sorted by step ascending)."""
    raw = spec.get("install_steps", [])
    if not raw:
        return ""
    steps = sorted(raw, key=lambda s: s.get("step", 0))

    rows = []
    for s in steps:
        tool = s.get("tool", "")
        sub = s.get("subcommand") or ""
        label = f"{tool} {sub}".strip() if sub else tool
        purpose = (s.get("purpose") or "").strip()
        cmd = s.get("command", "")
        rc = s.get("returncode")
        exit_html = ""
        if rc is not None:
            color = "#16a34a" if rc == 0 else "#dc2626"
            exit_html = (
                f'<span style="margin-left:auto;font-size:0.75rem;color:{color}">'
                f'exit {rc}</span>'
            )

        pkgs = s.get("installed_packages", []) or []
        if pkgs:
            items = "".join(
                f'<li><code>{p.get("name","")}'
                + (f"={p['version']}" if p.get('version') else "")
                + "</code>"
                + (f' <span style="color:#94a3b8">({p["channel"]})</span>'
                   if p.get("channel") else "")
                + "</li>"
                for p in pkgs if isinstance(p, dict)
            )
            pkgs_html = (
                f'<div class="pkgs"><span class="label">Installed '
                f'({len(pkgs)})</span><ul>{items}</ul></div>'
            )
        else:
            pkgs_html = '<div class="pkgs"><span class="label">Installed</span>' \
                        '<div class="none">—</div></div>'

        purpose_html = (
            f'<div style="color:#64748b;font-size:0.78rem;margin-bottom:0.2rem">'
            f'{purpose}</div>' if purpose else ""
        )

        rows.append(f"""
<div class="install-row">
  <div class="cmd">
    <div class="cmd-header">
      <span class="step-num">{s.get('step', '?')}</span>
      <strong>{label}</strong>
      {exit_html}
    </div>
    {purpose_html}
    <pre>{cmd}</pre>
  </div>
  {pkgs_html}
</div>""")

    return f"""
<div class="section">
  <h2>🛠️ Environment Install Steps</h2>
  <p style="font-size:0.85rem;color:#475569;margin-bottom:0.75rem">
    Commands run to build the conda environment, in chronological order.
    Right column shows what each command installed.
  </p>
  {"".join(rows)}
</div>"""


def _steps_section(spec: dict) -> str:
    steps = spec.get("pipeline_steps", [])
    if not steps:
        return ""
    blocks = []
    for s in steps:
        tool = s.get("tool", "")
        subcommand = s.get("subcommand", "")
        tool_label = f"{tool} {subcommand}".strip() if subcommand else tool
        version = s.get("version") or s.get("resolved_version", "")
        cmd = s.get("command", "")
        rc = s.get("returncode")

        exit_html = ""
        if rc is not None:
            color = "#16a34a" if rc == 0 else "#dc2626"
            exit_html = f'<span style="margin-left:auto;font-size:0.8rem;color:{color}">exit {rc}</span>'

        val_status = s.get("validation_status")
        val_html = ""
        if val_status == "passed":
            val_html = ' <span style="font-size:0.75rem;color:#16a34a;background:#22c55e1a;border-radius:10px;padding:1px 8px;margin-left:0.5rem">✓ outputs validated</span>'
        elif val_status == "failed":
            val_html = ' <span style="font-size:0.75rem;color:#dc2626;background:#ef44441a;border-radius:10px;padding:1px 8px;margin-left:0.5rem">✗ validation failed</span>'
        elif rc == 0:
            val_html = ' <span style="font-size:0.75rem;color:#d97706;background:#f59e0b1a;border-radius:10px;padding:1px 8px;margin-left:0.5rem">⚠ not validated</span>'

        deps = s.get("depends_on") or []
        deps_html = (
            f' <span style="font-size:0.75rem;color:#64748b;background:#e2e8f0;border-radius:10px;padding:1px 8px;margin-left:0.5rem">depends on step {", ".join(str(d) for d in deps)}</span>'
            if deps else ""
        )

        # I7 — observed resource cost (psutil-monitored). Surfaces here so
        # downstream HPC scheduling has truthful min-spec data per step.
        ru = s.get("resource_usage") or {}
        ru_html = ""
        if isinstance(ru, dict) and ("wall_seconds" in ru or "peak_rss_mb" in ru):
            wall = ru.get("wall_seconds")
            rss  = ru.get("peak_rss_mb")
            cpu  = ru.get("max_cpu_percent")
            chips = []
            if wall is not None:
                chips.append(f"⏱ {wall}s wall")
            if rss is not None:
                chips.append(f"🧠 {rss} MB peak RSS")
            if cpu:
                chips.append(f"⚙ {cpu}% peak CPU")
            ru_html = (
                '<div style="font-size:0.78rem;color:#475569;margin-top:0.4rem">'
                '<strong>Observed cost:</strong> '
                + " · ".join(chips)
                + '</div>'
            )

        io_html = _io_group(s)

        blocks.append(f"""
<div class="step-block">
  <div class="step-header">
    <span class="step-num">{s.get('step', '?')}</span>
    {tool_label} {version}{val_html}{deps_html}
    {exit_html}
  </div>
  <strong style="font-size:0.85rem;color:#475569">Command:</strong>
  <pre>{cmd}</pre>
  {ru_html}
  {io_html}
</div>""")

    return f"""
<div class="section">
  <h2>🧪 Test Run</h2>
  <p style="font-size:0.85rem;color:#475569;margin-bottom:0.75rem">
    Concrete commands the pipeline was exercised with during install — what was
    actually run, what it produced, and whether the outputs were validated.
    For the parameterized "run on new data" contract, see the <strong>Usage</strong> section above.
  </p>
  {"".join(blocks)}
</div>"""


def _self_test_block(spec: dict) -> str:
    """Render the usage.command_template self-test outcome — the keystone
    honesty check (I4). Multi-trial: each declared input shape was run; the
    block renders pass/fail per trial. usage_verified=True only when every
    trial passed."""
    usage = spec.get("usage") or {}
    if not isinstance(usage, dict):
        return ""
    st = usage.get("_self_test")
    if not st:
        return ""
    ok = bool(st.get("ok"))
    trials = st.get("trials") or []
    # Legacy single-trial shape (no `trials` key): synthesize one entry so
    # the renderer can use the same code path.
    if not trials and "command_run" in st:
        trials = [{
            "name":          "auto",
            "ok":            ok,
            "command_run":   st.get("command_run"),
            "produced_files": st.get("produced_files", []),
            "reason":        st.get("reason"),
        }]

    passed = sum(1 for t in trials if t.get("ok"))
    total  = len(trials)
    source = st.get("source", "")

    trial_blocks = []
    for t in trials:
        t_ok = bool(t.get("ok"))
        badge = "✅" if t_ok else "⚠️"
        name  = t.get("name", "trial")
        desc  = t.get("description") or ""
        desc_html = f"<div style=\"font-size:0.75rem;color:#64748b;margin-top:0.15rem\">{desc}</div>" if desc else ""
        cmd   = t.get("command_run", "") or ""
        prod  = t.get("produced_files", []) or []
        files_list = "".join(f"<li><code>{f}</code></li>" for f in prod[:8])
        details_inner = ""
        if cmd:
            details_inner += f"<strong style=\"font-size:0.78rem\">Command run:</strong><pre style=\"font-size:0.78rem\">{cmd}</pre>"
        if prod:
            details_inner += f"<strong style=\"font-size:0.78rem\">Files produced (first 8):</strong><ul style=\"font-size:0.78rem;margin-left:1.2rem\">{files_list}</ul>"
        if not t_ok and t.get("reason"):
            details_inner = f"<div style=\"font-size:0.78rem;color:#b91c1c\">Reason: {t['reason']}</div>" + details_inner
        details_html = (
            f"<details style=\"margin-top:0.4rem\">"
            f"<summary style=\"cursor:pointer;font-size:0.82rem\">Audit trail</summary>"
            f"<div style=\"margin-top:0.4rem\">{details_inner}</div></details>"
        ) if details_inner else ""
        trial_blocks.append(
            f"<div class=\"self-test-trial {'passed' if t_ok else 'failed'}\" "
            f"style=\"margin-top:0.5rem;padding:0.4rem 0.6rem;border-left:3px solid "
            f"{'#16a34a' if t_ok else '#dc2626'};background:#f8fafc\">"
            f"<strong>{badge} Trial: {name}</strong>{desc_html}{details_html}"
            f"</div>"
        )

    header_cls = "passed" if ok else "failed"
    header_badge = "✅" if ok else "⚠️"
    header_text = (
        f"Usage self-test: {passed}/{total} trial(s) passed"
        if total else "Usage self-test: no trials"
    )
    source_note = ""
    if source == "trials":
        source_note = "Trials were declared in <code>usage.trials</code> (explicit multi-shape contract)."
    elif source == "inferred":
        source_note = ("No <code>usage.trials</code> declared — single trial inferred from "
                       "pipeline_steps[*].inputs. Declare <code>trials</code> for multi-shape coverage.")

    return f"""
  <div class="self-test {header_cls}">
    <strong>{header_badge} {header_text}</strong>
    <div style="font-size:0.78rem;color:#475569;margin-top:0.3rem">{source_note}</div>
    {''.join(trial_blocks)}
  </div>"""


def _usage_section(spec: dict) -> str:
    """Single canonical "how to run this pipeline" section.

    Replaces the prior two-section split (🚀 Usage + 📖 Usage Guide) which were
    confusing and partially redundant (Usage Guide re-dumped the exact test command
    from Pipeline Steps with hardcoded host paths — non-portable and misleading).

    Contents:
      1. Primary-tool documentation links — surfaced prominently via _primary_packages.
      2. Description (from usage.description, LLM-authored).
      3. `conda activate {env}` + the command_template — runnable starting point.
      4. Inputs / Outputs slot tables.
      5. Example (if provided).
      6. Component documentation (collapsed) — non-primary package links.
    """
    u = spec.get("usage") or {}
    env = spec.get("conda_env", "")
    cmd_template = (u.get("command_template") or "").strip() if isinstance(u, dict) else ""
    if not cmd_template:
        return ""

    desc = (u.get("description") or "").strip()
    example = (u.get("example") or "").strip()

    # Primary-tool docs — fully derived from spec content
    primaries = _primary_packages(spec)
    primary_names = {p.get("name") for p in primaries}
    primary_links_html = ""
    if primaries:
        chips = []
        for p in primaries:
            name = p.get("name", "")
            hp = p.get("homepage", "")
            ver = p.get("resolved_version") or p.get("version") or ""
            label = f"{name} {ver}".strip()
            if hp:
                chips.append(
                    f'<a class="primary-doc" href="{hp}" target="_blank">📘 {label}</a>'
                )
            else:
                chips.append(f'<span class="primary-doc primary-doc-nolink">{label}</span>')
        primary_links_html = (
            '<div class="primary-docs"><strong>Documentation</strong>'
            + "".join(chips)
            + "</div>"
        )

    # Component docs — non-primary packages with a homepage
    component_links = []
    for p in spec.get("packages", []) or []:
        name = p.get("name", "")
        if not name or name == "conda-pack" or name in primary_names:
            continue
        hp = p.get("homepage", "")
        if hp:
            component_links.append(
                f'<li><a href="{hp}" target="_blank">{name}</a></li>'
            )
    component_html = ""
    if component_links:
        component_html = (
            '<details class="component-docs">'
            f'<summary>Component documentation ({len(component_links)} packages)</summary>'
            f'<ul>{"".join(component_links)}</ul>'
            '</details>'
        )

    def _slot_table(rows, label):
        if not rows:
            return ""
        body = "".join(
            f"<tr><td><code>{{{r.get('name','')}}}</code></td>"
            f"<td>{r.get('format','—') or '—'}</td>"
            f"<td>{(r.get('description') or '—')}</td></tr>"
            for r in rows
        )
        return f"""
  <h4 style="margin:0.8rem 0 0.3rem;font-size:0.95rem">{label}</h4>
  <table>
    <thead><tr><th>Slot</th><th>Format</th><th>Description</th></tr></thead>
    <tbody>{body}</tbody>
  </table>"""

    inputs_html  = _slot_table(u.get("inputs", []) if isinstance(u, dict) else [],  "Inputs")
    outputs_html = _slot_table(u.get("outputs", []) if isinstance(u, dict) else [], "Outputs")
    example_html = (
        f'<h4 style="margin:0.8rem 0 0.3rem;font-size:0.95rem">Example</h4><pre>{example}</pre>'
        if example else ""
    )
    desc_html = (
        f'<p style="font-size:0.95rem;color:#334155;margin-bottom:0.75rem">{desc}</p>'
        if desc else ""
    )
    activate_line = f"conda activate {env}\n\n" if env else ""

    self_test_html = _self_test_block(spec)
    return f"""
<div class="section">
  <h2>🚀 Usage</h2>
  {primary_links_html}
  {self_test_html}
  {desc_html}
  <h4 style="margin:0.4rem 0 0.3rem;font-size:0.95rem">Command</h4>
  <pre>{activate_line}{cmd_template}</pre>
  {inputs_html}
  {outputs_html}
  {example_html}
  {component_html}
</div>"""


def _notes_section(spec: dict) -> str:
    notes = list(spec.get("notes", []))
    if notes:
        items = "".join(f"<li style='margin-bottom:0.3rem'>{n}</li>" for n in notes)
        body = f"<ul style=\"padding-left:1.3rem;font-size:0.9rem\">{items}</ul>"
    else:
        body = "<p style='color:#888;font-size:0.9rem'>No notes.</p>"
    return f"""
<div class="section">
  <h2>📝 Notes</h2>
  {body}
</div>"""


def generate(spec: dict) -> str:
    # Lazy import to avoid circular dep: spec_writer also imports generate from here.
    from agent.skills.spec_writer import _pick_version_for_filename

    name = spec.get("pipeline_name", "pipeline")
    # Pick the version that actually matches the pipeline_name — not whatever
    # package happens to be first in spec.packages. Same logic the filename uses.
    version = _pick_version_for_filename(spec, name) or ""
    env = spec.get("conda_env", "")
    created = spec.get("created_at", "")
    if created:
        try:
            created = datetime.fromisoformat(created).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            pass

    title = f"{name} {version}".strip()
    env_status      = spec.get("env_status", "")
    pipeline_status = spec.get("pipeline_status", "")
    docker_status   = spec.get("docker_status", "")
    usage_verified  = bool(spec.get("usage_verified"))
    lock_sha        = spec.get("lock_sha256") or ""

    docker_badge = ""
    if docker_status and docker_status != "not_attempted":
        # built → green; failed → red. Same status badge styling as env/pipeline.
        kind = {"built": "passed", "failed": "failed"}.get(docker_status, "")
        if kind:
            docker_badge = (
                f'<span class="status-label" style="margin-left:0.6rem">Docker:</span>'
                f'{_status_badge(kind)}'
            )

    usage_verified_html = (
        '<span class="status-label" style="margin-left:0.6rem">Usage:</span>'
        + (_status_badge("passed") if usage_verified else _status_badge("partial"))
    ) if spec.get("usage") else ""

    lock_sha_html = (
        f'<span class="badge" title="sha256 of the env lock file — verify bit-exact env reproduction" '
        f'style="font-family:monospace;font-size:0.7rem">lock {lock_sha[:12]}…</span>'
        if lock_sha else ""
    )

    status_row = (
        '<div class="status-row">'
        + (f'<span class="badge">{env}</span>' if env else "")
        + (f'<span class="badge">{created}</span>' if created else "")
        + lock_sha_html
        + (f'<span class="status-label">Env:</span>{_status_badge(env_status)}'
           if env_status else "")
        + (f'<span class="status-label" style="margin-left:0.6rem">Pipeline:</span>'
           f'{_status_badge(pipeline_status)}' if pipeline_status else "")
        + docker_badge
        + usage_verified_html
        + "</div>"
    )

    body = "".join([
        _status_legend(),
        _usage_section(spec),                 # canonical "run on new data" contract — primary-tool docs + command template + IO
        _packages_table(spec),
        _runtime_env_section(spec),
        _service_dependencies_section(spec),
        _reference_databases_section(spec),
        _test_data_section(spec),
        _install_steps_section(spec),
        _steps_section(spec),
        _docker_section(spec),
        _notes_section(spec),
    ])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} — Pipeline Report</title>
<style>{_CSS}</style>
</head>
<body>
<div class="page">
  <header>
    <h1>{title}</h1>
    <div class="meta">{spec.get('description','')}</div>
    {status_row}
  </header>
  {body}
  <footer>Generated by bioinf-agent · <a href="https://github.com/monarch-initiative/bioinf-agent" style="color:#94a3b8">monarch-initiative/bioinf-agent</a></footer>
</div>
</body>
</html>"""
