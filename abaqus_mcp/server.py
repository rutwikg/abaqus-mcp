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
    name="abaqus-mcp",
    # Single source of truth: a hardcoded literal here silently drifted and was
    # still advertising 0.1.0 to clients at release 0.2.0.
    version=__version__,
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
    available = CONFIG.available()
    lines = [
        "Abaqus command: %s" % CONFIG.command,
        "Available: %s" % available,
        "Runs directory: %s" % CONFIG.runs_dir,
        "Default CPUs: %d, job timeout: %ds"
        % (CONFIG.default_cpus, CONFIG.job_timeout_s),
    ]
    if not available:
        # This is the first thing a new user hits when the solver is missing --
        # notably anyone running the container image, which cannot bundle a
        # licensed Abaqus. Say what to do rather than just reporting False.
        lines += [
            "",
            "No Abaqus launcher was found, so every tool that submits a job or",
            "builds a model will fail. The deck parsers and spec validation still",
            "work. To fix:",
            "  - install Abaqus and put its launcher on PATH, or",
            "  - set ABAQUS_AGENT_COMMAND to the launcher's full path",
            "    (e.g. C:\\SIMULIA\\Commands\\abaqus.bat), and",
            "  - if running in a container, mount the host Abaqus installation",
            "    and its licence server into the container as well.",
        ]
    return "\n".join(lines)

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
        sys.stderr.write("[abaqus-mcp] %s\n" % msg)
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


# --------------------------------------------------------------------------
# sweeps: unattended runs + the parked-failure queue
# --------------------------------------------------------------------------

@server.tool()
def sweep_status(sweep: str) -> str:
    """Summarise a parametric sweep: how many designs are valid, parked or
    abandoned, and where its results table lives.

    Args:
        sweep: The sweep name given when it was launched.
    """
    from .sweep import load_state, summarise, sweep_dir
    state = load_state(sweep)
    if not state:
        return "No sweep named '%s' under %s" % (sweep, CONFIG.runs_dir / "_sweeps")
    d = sweep_dir(sweep)
    return ("Sweep '%s': %d designs -- %s\n"
            "Results table: %s\nFix log: %s"
            % (sweep, len(state), summarise(state),
               d / "results.csv", d / "fixlog.jsonl"))


@server.tool()
def list_parked_failures(sweep: str) -> str:
    """List the designs a sweep could not resolve on its own.

    These are waiting for judgement: each either failed to converge, or
    converged to something the physical-validity gate rejected. Use
    get_parked_failure to read the diagnostics for one.

    Args:
        sweep: The sweep name.
    """
    from .sweep import list_parked
    parked = list_parked(sweep)
    if not parked:
        return "Nothing parked in sweep '%s' -- either all designs resolved, or it has not run." % sweep
    lines = ["%d design(s) awaiting judgement in '%s':" % (len(parked), sweep)]
    for p in parked:
        bits = []
        if p["error_categories"]:
            bits.append("errors: " + ", ".join(p["error_categories"]))
        if p["validity_failures"]:
            bits.append("rejected: " + ", ".join(p["validity_failures"]))
        if p["fixes_already_tried"]:
            bits.append("already tried: " + ", ".join(p["fixes_already_tried"]))
        lines.append("  %-24s %s" % (p["design_id"], p["parked_because"][:90]))
        for b in bits:
            lines.append("      " + b)
    return "\n".join(lines)


@server.tool()
def get_parked_failure(sweep: str, design_id: str, deck_chars: int = 8000) -> str:
    """Full diagnostics for one parked design, to reason about before fixing.

    Returns the solver status, the error-level diagnostics, the tail of the
    .sta and .msg, any input-processor errors, the validity verdict, the fixes
    already attempted, and the deck itself.

    Args:
        sweep: The sweep name.
        design_id: Which parked design to inspect.
        deck_chars: How much of the .inp to include (from the start).
    """
    from .sweep import get_parked
    b = get_parked(sweep, design_id)
    if b is None:
        return "No parked design '%s' in sweep '%s'." % (design_id, sweep)

    out = ["design: %s" % design_id,
           "params: %s" % json.dumps(b.get("params") or {}),
           "parked because: %s" % b.get("parked_because", ""),
           "solver status: %s (succeeded=%s)"
           % (b.get("solver_status"), b.get("succeeded"))]

    val = b.get("validity")
    if val:
        out.append("")
        out.append("VALIDITY: %s" % ("valid" if val.get("valid") else "REJECTED"))
        for c in val.get("checks", []):
            out.append("  " + c)
        if val.get("remedy"):
            out.append("  remedy: " + val["remedy"])

    if b.get("errors"):
        out.append("")
        out.append("ERROR-LEVEL DIAGNOSTICS:")
        for e in b["errors"]:
            loc = ""
            if e.get("node"):
                loc = " (node %s%s)" % (e["node"], ", DOF %s" % e["dof"] if e.get("dof") else "")
            elif e.get("element"):
                loc = " (element %s)" % e["element"]
            out.append("  [%s]%s %s" % (e.get("category"), loc, (e.get("text") or "")[:300]))

    if b.get("dat_errors"):
        out.append("")
        out.append("INPUT-PROCESSOR ERRORS (.dat):")
        out.append(b["dat_errors"])

    for key, title in (("sta_tail", "STATUS FILE (.sta) tail"),
                       ("msg_tail", "MESSAGE FILE (.msg) tail")):
        if b.get(key):
            out.append("")
            out.append(title + ":")
            out.append(b[key])

    if b.get("fixes_already_tried"):
        out.append("")
        out.append("ALREADY TRIED (do not repeat): "
                   + ", ".join(b["fixes_already_tried"]))
    if b.get("stopped_reason"):
        out.append("loop stopped: " + b["stopped_reason"])

    if b.get("deck"):
        out.append("")
        out.append("DECK (%s):" % b.get("deck_path", ""))
        out.append(b["deck"][:max(0, deck_chars)])
    return "\n".join(out)


@server.tool()
def apply_reasoned_fix(
    sweep: str,
    design_id: str,
    edits_json: str,
    rationale: str,
    max_iters: int = 3,
    cpus: int = 4,
) -> str:
    """Apply a reasoned deck edit to a parked design and re-run it.

    Edits are literal find/replace so the change is reviewable and lands in the
    fix log. Edits that remove output requests, or that delete most of the
    deck, are refused: a run is judged on its output, so deleting the output is
    not a fix.

    Args:
        sweep: The sweep name.
        design_id: The parked design to repair.
        edits_json: JSON list of {"find": ..., "replace": ...} applied in order.
        rationale: Why this edit should fix the diagnosed failure. Recorded.
        max_iters: Deterministic fix iterations allowed on the re-run.
        cpus: CPUs for the solver.
    """
    from .sweep import RefusedEdit, apply_reasoned_fix as _apply
    try:
        edits = json.loads(edits_json)
    except json.JSONDecodeError as e:
        return "edits_json is not valid JSON: %s" % e
    if not isinstance(edits, list) or not edits:
        return "edits_json must be a non-empty JSON list of {find, replace} objects."
    try:
        entry = _apply(sweep, design_id, edits, rationale,
                       max_iters=max_iters, cpus=cpus)
    except RefusedEdit as e:
        return "EDIT REFUSED: %s" % e
    except (ValueError, KeyError) as e:
        return "Could not apply: %s" % e

    head = ("RESOLVED" if entry["outcome"] == "valid"
            else "still %s" % entry["outcome"])
    return ("%s -- %s\n  was: %s\n  edits: %s\n  now: %s"
            % (design_id, head, entry["was"], "; ".join(entry["edits"]),
               entry["reason"]))


@server.tool()
def get_fix_log(sweep: str) -> str:
    """The record of every reasoned fix attempted in a sweep, successful or not.

    What failed, what was diagnosed, what changed, and whether it then
    converged.

    Args:
        sweep: The sweep name.
    """
    from .sweep import fix_log
    entries = fix_log(sweep)
    if not entries:
        return "No reasoned fixes recorded for sweep '%s' yet." % sweep
    lines = ["%d reasoned fix attempt(s) in '%s':" % (len(entries), sweep)]
    for e in entries:
        lines.append("")
        lines.append("%s  %s -> %s" % (e.get("at", ""), e["design_id"], e["outcome"]))
        lines.append("  was      : %s" % e.get("was", ""))
        lines.append("  rationale: %s" % e.get("rationale", ""))
        for ed in e.get("edits", []):
            lines.append("  edit     : %s" % ed)
        lines.append("  result   : %s" % e.get("reason", ""))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# inspection, judgement and rendering
# --------------------------------------------------------------------------

@server.tool()
def check_validity(job_name: str, quasi_static: bool = False) -> str:
    """Judge whether a COMPLETED job is physically believable.

    A green solver status only means the analysis reached the end of the step.
    This reads the energy balance and reports whether the answer can be trusted:
    artificial (hourglass) energy carrying the load, kinetic energy dominating a
    quasi-static event, or total energy not being conserved.

    Args:
        job_name: The job to judge.
        quasi_static: True if the event is meant to be slow (a crush, a press).
            Kinetic energy is only a failure signal when it is.
    """
    from .validity import assess
    res = extract_results(job_name)
    if not res.get("ok"):
        return "Could not read results for '%s': %s" % (job_name, res.get("error"))
    v = assess(res, quasi_static=quasi_static)
    out = [v.summary()]
    if v.metrics:
        out.append("")
        out.append("metrics:")
        for k in sorted(v.metrics):
            out.append("  %-28s %.4f" % (k, v.metrics[k]))
    return "\n".join(out)


@server.tool()
def inspect_deck(inp_path: str) -> str:
    """Summarise what is actually in an Abaqus input deck.

    Reports the mesh, materials, sections, sets, steps, loads and boundary
    conditions a deck defines, plus any references pointing at names the deck
    never defines. Reads the file only: no solver, no licence token.

    Args:
        inp_path: Path to the .inp deck, or a job name under the runs directory.
    """
    from .inp import Deck
    from .fixes import _referenced_names
    p = Path(inp_path)
    if not p.is_file():
        cand = _job_dir(inp_path) / ("%s.inp" % inp_path)
        if cand.is_file():
            p = cand
        else:
            return "No deck at %s (also tried %s)" % (inp_path, cand)

    deck = Deck.load(p)
    defined = deck.defined_names()
    counts = {}
    for b in deck.blocks:
        if not b.is_comment and b.keyword:
            counts[b.keyword] = counts.get(b.keyword, 0) + 1

    out = ["Deck: %s" % p, "%d keyword blocks" % sum(counts.values()), ""]
    groups = (
        ("MESH", ("NODE", "ELEMENT")),
        ("SECTIONS", ("SOLID SECTION", "SHELL SECTION", "BEAM SECTION",
                      "MEMBRANE SECTION")),
        ("MATERIALS", ("MATERIAL", "ELASTIC", "PLASTIC", "DENSITY")),
        ("STEPS", ("STEP", "STATIC", "DYNAMIC", "VISCO", "FREQUENCY", "BUCKLE",
                   "HEAT TRANSFER")),
        ("LOADS/BCs", ("BOUNDARY", "CLOAD", "DLOAD", "DSLOAD", "TEMPERATURE",
                       "INITIAL CONDITIONS")),
        ("INTERACTIONS", ("CONTACT", "CONTACT PAIR", "TIE", "SURFACE",
                          "SURFACE INTERACTION")),
        ("OUTPUT", ("OUTPUT", "NODE OUTPUT", "ELEMENT OUTPUT", "ENERGY OUTPUT",
                    "NODE PRINT", "EL PRINT")),
    )
    for title, kws in groups:
        present = ["*%s x%d" % (k, counts[k]) for k in kws if k in counts]
        if present:
            out.append("%-14s %s" % (title + ":", ", ".join(present)))

    out.append("")
    for cat in ("material", "nset", "elset", "surface"):
        names = sorted(defined.get(cat) or [])
        if names:
            shown = ", ".join(names[:12])
            if len(names) > 12:
                shown += ", ... (%d total)" % len(names)
            out.append("%-9s %s" % (cat + ":", shown))

    # Dangling references are the commonest reason a deck refuses to run.
    refs = _referenced_names(deck)
    dangling = []
    for name in refs["material"]:
        if name not in defined["material"]:
            dangling.append("material '%s'" % name)
    for name in refs["set"]:
        if name not in (defined["nset"] | defined["elset"]):
            dangling.append("set '%s'" % name)
    out.append("")
    if dangling:
        out.append("DANGLING REFERENCES (this deck will not run):")
        for d in dangling:
            out.append("  " + d)
        out.append("autocorrect_simulation can usually repair these.")
    else:
        out.append("No dangling set/material references.")
    return "\n".join(out)


@server.tool()
def start_sweep(
    sweep_name: str,
    designs_json: str,
    max_iters: int = 3,
    cpus: int = 4,
    quasi_static: bool = False,
) -> str:
    """Run a parametric sweep, parking whatever needs judgement.

    Each design goes through the autocorrect loop and then the validity gate.
    Anything that fails, or that converges to something not believable, is
    parked with full diagnostics rather than dropped. Resumable: re-running the
    same sweep name skips designs that already finished.

    This blocks until the sweep completes, so keep the design count small from
    an interactive client. For a long unattended sweep, run the sweep module as
    a detached process instead.

    Args:
        sweep_name: Name for this sweep; also its results directory.
        designs_json: JSON list of {"design_id":..., "inp":..., "params":{...}}.
        max_iters: Deterministic fix iterations allowed per design.
        cpus: CPUs per solver run.
        quasi_static: Whether to judge the event as quasi-static.
    """
    from .sweep import Design, run_sweep, summarise, sweep_dir
    try:
        raw = json.loads(designs_json)
    except json.JSONDecodeError as e:
        return "designs_json is not valid JSON: %s" % e
    if not isinstance(raw, list) or not raw:
        return "designs_json must be a non-empty JSON list."

    designs = []
    for i, d in enumerate(raw):
        if not isinstance(d, dict) or not d.get("design_id"):
            return "design %d needs a 'design_id'" % i
        designs.append(Design(design_id=d["design_id"],
                              params=d.get("params") or {},
                              inp=d.get("inp"), spec=d.get("spec")))

    state = run_sweep(sweep_name, designs, max_iters=max_iters, cpus=cpus,
                      quasi_static=quasi_static)
    sd = sweep_dir(sweep_name)
    lines = ["Sweep '%s': %s" % (sweep_name, summarise(state)), ""]
    for o in sorted(state.values(), key=lambda x: x.design_id):
        lines.append("  %-22s %-10s %s" % (o.design_id, o.status, o.reason[:70]))
    lines.append("")
    lines.append("results: %s" % (sd / "results.csv"))
    lines.append("Use list_parked_failures to see what needs judgement.")
    return "\n".join(lines)


@server.tool()
def whats_new(version: str = "") -> str:
    """What changed in this release, and in earlier ones.

    Args:
        version: A specific version such as "0.3.0". Omit for the current
            release, or pass "all" for the whole history.
    """
    from .changelog import render_changelog
    return render_changelog(version or None)
