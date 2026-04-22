"""
ReportBuilder — generates a self-contained HTML pipeline report from a saved spec dict.
Called automatically by InstallPipelineSkill._save_spec after every successful install.
Expects spec dicts conforming to the PipelineSpec model (agent/models/core_data.py).
"""

from datetime import datetime
from pathlib import Path


_CSS = """
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
       background: #f5f7fa; color: #1a1a2e; line-height: 1.6; }
.page { max-width: 960px; margin: 0 auto; padding: 2rem 1.5rem; }
header { background: linear-gradient(135deg, #16213e 0%, #0f3460 100%);
         color: #fff; border-radius: 12px; padding: 2rem 2.5rem; margin-bottom: 2rem; }
header h1 { font-size: 2rem; font-weight: 700; letter-spacing: -0.5px; }
header .meta { margin-top: 0.5rem; opacity: 0.8; font-size: 0.9rem; }
header .badge { display: inline-block; background: rgba(255,255,255,0.15);
                border-radius: 20px; padding: 2px 12px; font-size: 0.8rem;
                margin-right: 0.5rem; margin-top: 0.5rem; }
.badge.pass  { background: #22c55e33; color: #16a34a; border: 1px solid #16a34a55; }
.badge.fail  { background: #ef444433; color: #dc2626; border: 1px solid #dc262655; }
.badge.skip  { background: #f59e0b33; color: #d97706; border: 1px solid #d9770655; }
.section { background: #fff; border-radius: 10px; padding: 1.5rem 2rem;
           margin-bottom: 1.5rem; box-shadow: 0 1px 4px rgba(0,0,0,0.07); }
.section h2 { font-size: 1.15rem; font-weight: 600; color: #0f3460;
              border-bottom: 2px solid #e2e8f0; padding-bottom: 0.5rem; margin-bottom: 1rem; }
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
footer { text-align: center; font-size: 0.8rem; color: #94a3b8; margin-top: 2rem; }
"""


def _badge(text: str, kind: str = "pass") -> str:
    return f'<span class="badge {kind}">{text}</span>'


def _status_badge(spec: dict) -> str:
    status = spec.get("status", "")
    if status in ("fully_validated", "complete"):
        return _badge("✓ Validated", "pass")
    if status == "in_progress":
        return _badge("⏳ In Progress", "skip")
    if status and status != "unknown":
        return _badge("✗ " + status, "fail")
    steps = spec.get("pipeline_steps", [])
    if steps and all(s.get("status") == "validated" for s in steps):
        return _badge("✓ Validated", "pass")
    if steps:
        return _badge("⏳ Partial", "skip")
    return _badge("Unknown", "skip")


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
        rows.append(
            f"<tr><td><strong>{p.get('name','')}</strong></td>"
            f"<td>{version}</td>"
            f"<td>{p.get('channel','')}</td>"
            f"<td>{p.get('description','')}</td>"
            f"<td>{link}</td></tr>"
        )
    return f"""
<div class="section">
  <h2>📦 Packages</h2>
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
    upstream = td.get("upstream_pipelines", [])
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
        status = s.get("status", "")

        if rc is not None:
            exit_html = f'<span style="margin-left:auto;font-size:0.8rem;color:{"#16a34a" if rc == 0 else "#dc2626"}">exit {rc}</span>'
        elif status == "validated":
            exit_html = '<span style="margin-left:auto;font-size:0.8rem;color:#16a34a">✓ validated</span>'
        else:
            exit_html = ""

        validation = s.get("validation") or s.get("validation_details")

        val_html = ""
        if isinstance(validation, dict):
            for fname, vr in validation.items():
                if isinstance(vr, dict):
                    ok = vr.get("passed", False)
                    size = vr.get("size_bytes", 0)
                    size_str = f"{size / 1024:.1f} KB" if size else ""
                else:
                    ok = bool(vr)
                    size_str = ""
                icon = "✅" if ok else "❌"
                val_html += f'<div class="val-row">{icon} <code>{fname}</code> {size_str}</div>'
        elif validation:
            val_html = f'<div class="val-row">{"✅" if validation else "❌"} {validation}</div>'
        elif status == "validated":
            val_html = '<div class="val-row">✅ validated</div>'

        outputs = s.get("outputs", {})
        out_html = ""
        if outputs:
            out_html = "<br><strong>Outputs:</strong><ul style='margin:0.4rem 0 0 1.2rem;font-size:0.85rem'>"
            for k, v in outputs.items():
                out_html += f"<li><code>{Path(str(v)).name}</code></li>"
            out_html += "</ul>"

        blocks.append(f"""
<div class="step-block">
  <div class="step-header">
    <span class="step-num">{s.get('step', '?')}</span>
    {tool_label} {version}
    {exit_html}
  </div>
  <strong style="font-size:0.85rem;color:#475569">Command:</strong>
  <pre>{cmd}</pre>
  {out_html}
  {('<div style="margin-top:0.6rem"><strong style="font-size:0.85rem;color:#475569">Validation:</strong>' + val_html + '</div>') if val_html else ''}
</div>""")

    return f"""
<div class="section">
  <h2>⚙️ Pipeline Steps &amp; Usage</h2>
  {"".join(blocks)}
</div>"""


def _usage_guide(spec: dict) -> str:
    steps = spec.get("pipeline_steps", [])
    if not steps:
        return ""
    env = spec.get("conda_env", "bioinf_<name>")
    cmds = "\n\n".join(
        f"# Step {s.get('step','?')}: {s.get('tool','')}\n{s.get('command','')}"
        for s in steps
    )
    doc_links = []
    for p in spec.get("packages", []):
        hp = p.get("homepage", "")
        if hp and p.get("name") != "conda-pack":
            doc_links.append(f'<li><a href="{hp}" target="_blank">{p["name"]} documentation</a></li>')
    doc_html = (
        f"<ul style='margin:0.8rem 0 0 1.2rem'>{''.join(doc_links)}</ul>" if doc_links else ""
    )
    return f"""
<div class="section">
  <h2>📖 Usage Guide</h2>
  <p style="font-size:0.9rem;color:#475569;margin-bottom:0.5rem">
    Activate the conda environment, then run:
  </p>
  <pre>conda activate {env}\n\n{cmds}</pre>
  {doc_html}
</div>"""


def _notes_section(spec: dict) -> str:
    notes = list(spec.get("notes", []))
    if not notes:
        return ""
    items = "".join(f"<li style='margin-bottom:0.3rem'>{n}</li>" for n in notes)
    return f"""
<div class="section">
  <h2>📝 Notes</h2>
  <ul style="padding-left:1.3rem;font-size:0.9rem">{items}</ul>
</div>"""


def generate(spec: dict) -> str:
    name = spec.get("pipeline_name", "pipeline")
    primary = next((p for p in spec.get("packages", []) if p.get("name") != "conda-pack"), {})
    version = primary.get("resolved_version") or primary.get("version", "")
    env = spec.get("conda_env", "")
    created = spec.get("created_at", "")
    if created:
        try:
            created = datetime.fromisoformat(created).strftime("%Y-%m-%d %H:%M UTC")
        except Exception:
            pass

    title = f"{name} {version}".strip()
    header_meta = " ".join(filter(None, [
        f'<span class="badge">{env}</span>' if env else "",
        _status_badge(spec),
        f'<span class="badge">{created}</span>' if created else "",
    ]))

    body = "".join([
        _packages_table(spec),
        _test_data_section(spec),
        _steps_section(spec),
        _usage_guide(spec),
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
    <div style="margin-top:0.75rem">{header_meta}</div>
  </header>
  {body}
  <footer>Generated by bioinf-agent · <a href="https://github.com/monarch-initiative/bioinf-agent" style="color:#94a3b8">monarch-initiative/bioinf-agent</a></footer>
</div>
</body>
</html>"""
