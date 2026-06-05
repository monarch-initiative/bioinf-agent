"""mcp_tools — submodules that register MCP tools on the bioinf-agent FastMCP app.

Each submodule imports `mcp`, `config`, and the skill singletons from
`agent.mcp_server` and registers its `@mcp.tool()`-decorated functions as a
side effect of being imported.

The split is by THEME (see CLAUDE.md), not arbitrary. The themes:

  env_tools           — research + install primitives (one per ecosystem) +
                        verify_installation + run_install_command
  run_tools           — run_pipeline_step / run_step_in_container / run_in_env /
                        validate_output (the "execute against test data" surface)
  freeze_tools        — freeze + verify_env_recipe + generate_user_guide
                        (Layer-1 deliverable production)
  workflow_tools      — pipeline lifecycle (start/show/patch/discard) +
                        mark_step_validated + stage_authored_artifact +
                        seal_workflow + write_pipeline_provenance +
                        fetch_r_package_deps + list_installed_pipelines
                        (Layer-2 surface + draft-state machinery)
  data_tools          — add_core_*, phenopacket_*, select_test_data,
                        list_available_resources, download_resource,
                        download_reference_database, install_pipeline_brief
                        (test-data + reference-data acquisition)
  service_tools       — start_service / stop_service / check_service_health /
                        verify_service_dependency / check_gpu
                        (service-dependency lifecycle + GPU probe)
  jobs_tools          — run_in_background / check_job / cancel_job / list_jobs
                        (JobManager surface for >10-min tool calls)
  observability_tools — agent_status + snapshot_project
                        (pure-read snapshot surfaces — "where am I?" + cluster)
  bridge_tools        — upload_to_scratch / fetch_from_scratch
                        (Phase 2 HPC actuators — sibling to observability;
                        same permission gate, same ControlMaster pattern,
                        bytes move both directions with sha256 round-trip)

ALL helpers, types (StrList/OptStrList), and skill singletons live in
`agent.mcp_server` — submodules import what they need from there. This keeps
the back-compat surface (tests / freeze_runner subprocess) trivially
preserved: `from agent.mcp_server import freeze` still resolves because the
tool functions are re-exported there after `from . import mcp_tools` fires.

The circular import is safe — by the time `mcp_server.py` triggers this
package import (at the BOTTOM of the file), every name a submodule needs from
mcp_server (mcp, config, singletons, helpers) is already bound. Python's
partial-module handling gives the submodules the bound names. Adding a new
submodule is one line below.
"""
from __future__ import annotations

# Submodule imports register `@mcp.tool()` decorators as a side effect.
# This list grows as the mcp_server.py split proceeds (PHASE 1..8).
# Adding a new submodule = one line here + the submodule file + delete the
# original tool(s) from mcp_server.py + the back-compat re-export at the
# bottom of mcp_server.py.
from . import bridge_tools         # noqa: F401  (upload_to_scratch, fetch_from_scratch)
from . import data_tools           # noqa: F401  (download_reference_database, list_available_resources, download_resource, add_core_test_data, add_core_pod5_data, add_phenopacket, phenopacket_to_vcf, select_test_data, install_pipeline_brief)
from . import env_tools            # noqa: F401  (search_package, resolve_tool, create_conda_env, install_conda_packages, install_git_repo, synth_fetch, synth_build, install_spack_package, install_release_binary, install_perl_package, install_cargo_tool, install_go_tool, install_jar_tool, install_r_package, install_pip_package, run_install_command)
from . import freeze_tools         # noqa: F401  (freeze, verify_env_recipe, generate_user_guide)
from . import jobs_tools           # noqa: F401  (run_in_background, check_job, cancel_job, list_jobs)
from . import observability_tools  # noqa: F401  (agent_status, snapshot_project)
from . import run_tools            # noqa: F401  (run_pipeline_step, run_step_in_container, verify_installation, run_in_env, validate_output)
from . import service_tools        # noqa: F401  (check_gpu, start_service, stop_service, check_service_health, verify_service_dependency)
from . import workflow_tools       # noqa: F401  (seal_workflow, write_pipeline_provenance, list_installed_pipelines, fetch_r_package_deps, start_pipeline, discard_pipeline_draft, show_pipeline_draft, patch_pipeline, stage_authored_artifact, mark_step_validated)
