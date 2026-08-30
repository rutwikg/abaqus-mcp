"""MCP server exposing the Abaqus engine as tools.

Runs over stdio so any MCP client (Claude Desktop, Claude Code, a local-LLM
client) can drive Abaqus in natural language. Tools return LLM-friendly text.

Long-running solver calls are plain synchronous functions; the MCP runtime
executes sync tools in a worker thread, so the event loop stays responsive.

Run directly:  python -m abaqus_mcp.server
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from mcp.server.mcpserver import MCPServer

from . import __version__
from .authoring import build_and_run_spec, build_deck_from_spec
from .config import CONFIG
from .loop import autocorrect_run
from .report import build_report
from .results import extract_results, format_results
from .runner import run_deck
from .spec import PARAM_SHAPES
from .spec import dumps as spec_dumps
from .spec import example_parametric_spec, example_spec, validate_spec

server = MCPServer(
    name="abaqus-agent",
    version="0.1.0",
    instructions=(
        "Drive Abaqus/Standard FEA simulations. Provide an Abaqus keyword input "
        "deck (.inp) via a file path. Use `autocorrect_simulation` to run a job "
        "and automatically diagnose and fix convergence/singularity/deck errors "
        "by reading the .sta/.msg/.dat files and retrying. Use `run_simulation` "
        "for a single pass, and the read_* tools to inspect results."
    ),
)


def _job_dir(job_name: str) -> Path:
    return CONFIG.runs_dir / job_name


def _results_block(job_name: str) -> str:
    """Best-effort results summary to append after a successful run."""
    try:
        res = extract_results(job_name)
    except Exception as e:  # never let results extraction sink a good run
        return "\n\n(Results extraction failed: %s)" % e
    if res.get("ok"):
        return "\n\nRESULTS:\n" + format_results(res)
    return "\n\n(Results not extracted: %s)" % res.get("error", "unknown")


@server.tool()
def check_environment() -> str:
    """Report whether Abaqus is available and where jobs will run."""
    return (
        "Abaqus command: %s\n"
        "Available: %s\n"
        "Runs directory: %s\n"
        "Default CPUs: %d, job timeout: %ds"
        % (
            CONFIG.command,
            CONFIG.available(),
            CONFIG.runs_dir,
            CONFIG.default_cpus,
            CONFIG.job_timeout_s,
        )
    )

@server.tool()
def greeting() -> str:
    """Return a friendly greeting to the user."""
    return "Hello! I am your Abaqus agent. How can I assist you today?"


@server.tool()
def run_simulation(
    inp_path: str,
    job_name: Optional[str] = None,
    cpus: int = 1,
    timeout_s: int = 3600,
) -> str:
    """Run a single Abaqus job from an .inp deck and return its status report.

    Args:
        inp_path: Path to the Abaqus keyword input deck (.inp).
        job_name: Optional job name (defaults to the deck's file stem).
        cpus: Number of CPUs for the solver.
        timeout_s: Wall-clock ceiling for the solver run.
    """
    report = run_deck(inp_path, job_name=job_name, cpus=cpus, timeout_s=timeout_s)
    out = report.summary()
    if report.succeeded:
        out += _results_block(report.job_name)
    return out


@server.tool()
def autocorrect_simulation(
    inp_path: str,
    job_name: Optional[str] = None,
    max_iters: int = 5,
    cpus: int = 1,
    timeout_s: int = 3600,
) -> str:
    """Run a job and autonomously fix failures (convergence, singularity, deck
    errors) by editing the deck from the .sta/.msg/.dat diagnostics, retrying up
    to max_iters times. Returns a full narrative of every attempt and fix.

    Args:
        inp_path: Path to the Abaqus keyword input deck (.inp).
        job_name: Optional job name (defaults to the deck's file stem).
        max_iters: Maximum run/fix iterations.
        cpus: Number of CPUs for the solver.
        timeout_s: Wall-clock ceiling per solver run.
    """
    result = autocorrect_run(
        inp_path,
        job_name=job_name,
        max_iters=max_iters,
        cpus=cpus,
        timeout_s=timeout_s,
    )
    tail = ""
    if result.working_deck:
        tail = "\n\nFinal working deck: %s" % result.working_deck
    out = result.narrative() + tail
    if result.succeeded:
        out += _results_block(result.job_name)
    return out


@server.tool()
def get_job_status(job_name: str) -> str:
    """Parse the current output files of a job and return a status summary."""
    d = _job_dir(job_name)
    if not d.is_dir():
        return "No job named '%s' under %s" % (job_name, CONFIG.runs_dir)
    report = build_report(job_name, d)
    return report.summary()


@server.tool()
def read_job_file(job_name: str, extension: str = "msg", max_lines: int = 200) -> str:
    """Return the tail of a job's output file for inspection.

    Args:
        job_name: The job to inspect.
        extension: One of inp, sta, msg, dat, log (without the dot).
        max_lines: Maximum number of trailing lines to return.
    """
    ext = extension.lstrip(".")
    f = _job_dir(job_name) / ("%s.%s" % (job_name, ext))
    if not f.is_file():
        return "File not found: %s" % f
    lines = f.read_text(errors="replace").splitlines()
    tail = lines[-max_lines:]
    return "\n".join(tail)


@server.tool()
def get_spec_template() -> str:
    """Return an example simulation spec (JSON) showing every field the model
    authoring pipeline accepts. Fill this in to describe a new simulation."""
    return spec_dumps(example_spec())


@server.tool()
def get_parametric_spec_template() -> str:
    """Return an example spec that builds geometry parametrically (no CAD file).
    Also lists the supported shapes and their required params."""
    shapes = "\n".join("  %s: %s" % (k, v) for k, v in sorted(PARAM_SHAPES.items()))
    return ("Supported parametric shapes and required params:\n%s\n\nExample:\n%s"
            % (shapes, spec_dumps(example_parametric_spec())))


@server.tool()
def validate_simulation_spec(spec_json: str) -> str:
    """Check a simulation spec (JSON string) against the schema without running
    anything. Returns 'valid' or a list of problems to fix."""
    try:
        spec = json.loads(spec_json)
    except json.JSONDecodeError as e:
        return "Invalid JSON: %s" % e
    errors = validate_spec(spec)
    return "valid" if not errors else "Problems:\n- " + "\n- ".join(errors)


@server.tool()
def build_model(spec_json: str, job_name: Optional[str] = None) -> str:
    """Build a meshed model from a simulation spec (JSON) and export an .inp
    deck, WITHOUT running it. Imports CAD (STEP/IGES), meshes, and applies
    materials/BCs/loads. Returns the deck path and mesh stats, or build errors.

    Args:
        spec_json: The simulation spec as a JSON string (see get_spec_template).
        job_name: Optional job name (defaults to the spec's model_name).
    """
    try:
        spec = json.loads(spec_json)
    except json.JSONDecodeError as e:
        return "Invalid JSON: %s" % e
    res = build_deck_from_spec(spec, job_name=job_name)
    if not res["ok"]:
        out = "BUILD FAILED:\n- " + "\n- ".join(res["errors"])
        if res.get("traceback"):
            out += "\n\nCAE traceback (tail):\n" + res["traceback"][-1200:]
        return out
    return "BUILT: %s\nStats: %s" % (res["inp"], json.dumps(res["stats"]))


@server.tool()
def build_and_simulate(
    spec_json: str, job_name: Optional[str] = None, max_iters: int = 5
) -> str:
    """Full pipeline: build a model from a spec (CAD import + mesh + physics),
    then autonomously run and error-correct it. Returns build stats plus the
    run narrative.

    Args:
        spec_json: The simulation spec as a JSON string (see get_spec_template).
        job_name: Optional job name (defaults to the spec's model_name).
        max_iters: Maximum run/fix iterations.
    """
    try:
        spec = json.loads(spec_json)
    except json.JSONDecodeError as e:
        return "Invalid JSON: %s" % e
    res = build_and_run_spec(spec, job_name=job_name, max_iters=max_iters)
    build = res["build"]
    if not build["ok"]:
        out = "BUILD FAILED:\n- " + "\n- ".join(build["errors"])
        if build.get("traceback"):
            out += "\n\nCAE traceback (tail):\n" + build["traceback"][-1200:]
        return out
    out = ("BUILD ok. Stats: %s\n\n%s"
           % (json.dumps(build["stats"]), res["run"]))
    if res.get("succeeded"):
        out += _results_block(job_name or spec.get("model_name", "model"))
    return out


@server.tool()
def get_results(job_name: str) -> str:
    """Extract and report headline results (peak von Mises stress, peak
    displacement, plastic strain / yielding, net reaction force) from a
    finished job's .odb."""
    try:
        res = extract_results(job_name)
    except Exception as e:
        return "Results extraction failed: %s" % e
    return format_results(res)


@server.tool()
def list_jobs() -> str:
    """List all jobs (run directories) known to the engine."""
    root = CONFIG.runs_dir
    if not root.is_dir():
        return "No runs directory yet at %s" % root
    dirs = sorted(p.name for p in root.iterdir() if p.is_dir())
    return "\n".join(dirs) if dirs else "(no jobs yet)"


def main() -> None:
    """Launch the server. Transport is selectable so non-stdio MCP clients work:

        python -m abaqus_mcp.server                  # stdio (default)
        python -m abaqus_mcp.server streamable-http  # HTTP (Streamable HTTP)
        python -m abaqus_mcp.server sse              # HTTP (SSE)

    Or set ABAQUS_AGENT_TRANSPORT. Host/port for the HTTP transports default to
    the library's 127.0.0.1:8000 and can be overridden with ABAQUS_AGENT_HOST /
    ABAQUS_AGENT_PORT.
    """
    import os
    import sys

    transport = (
        sys.argv[1] if len(sys.argv) > 1
        else os.environ.get("ABAQUS_AGENT_TRANSPORT", "stdio")
    )
    if transport not in ("stdio", "sse", "streamable-http"):
        sys.stderr.write("Unknown transport '%s'; use stdio | sse | "
                         "streamable-http\n" % transport)
        raise SystemExit(2)

    # Startup banner -> STDERR only. stdout is the JSON-RPC channel for stdio
    # transport and must stay clean, so diagnostics never go there. These lines
    # appear in your terminal when run by hand and in the client's MCP logs.
    def _log(msg):
        sys.stderr.write("[abaqus-agent] %s\n" % msg)
        sys.stderr.flush()

    _log("starting MCP server v%s (transport=%s)" % (__version__, transport))
    _log("Abaqus command: %s  (available=%s)" % (CONFIG.command, CONFIG.available()))
    _log("runs dir: %s" % CONFIG.runs_dir)
    _log("ready - waiting for an MCP client to connect...")

    if transport == "stdio":
        server.run(transport="stdio")
    else:
        host = os.environ.get("ABAQUS_AGENT_HOST")
        port = os.environ.get("ABAQUS_AGENT_PORT")
        # Apply host/port to the server settings if the client provided them;
        # otherwise the library defaults (127.0.0.1:8000) apply.
        if host is not None and hasattr(server, "settings"):
            server.settings.host = host
        if port is not None and hasattr(server, "settings"):
            server.settings.port = int(port)
        server.run(transport=transport)


if __name__ == "__main__":
    main()
