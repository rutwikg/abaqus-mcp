# abaqus-mcp

[![PyPI](https://img.shields.io/pypi/v/abaqus-mcp)](https://pypi.org/project/abaqus-mcp/)
[![Python](https://img.shields.io/pypi/pyversions/abaqus-mcp)](https://pypi.org/project/abaqus-mcp/)
[![License](https://img.shields.io/badge/license-AGPL--3.0-blue)](LICENSE)

Natural-language driver for Abaqus/Standard FEA. Describe a problem, hand over an
input deck, and the agent runs the simulation and **autonomously diagnoses and
fixes failures** by reading the `.sta` / `.msg` / `.dat` files and retrying.

Exposed as an **MCP server**, so any MCP client (Claude Desktop, Claude Code, or
a future local-LLM client) can drive it.

> **Requires a working Abaqus installation and license.** This project automates
> Abaqus; it does not replace or include it. It is not affiliated with or
> endorsed by Dassault Systèmes.

## Status

| Phase | Piece | State |
|-------|-------|-------|
| 1 | Solver runner + `.sta`/`.msg`/`.dat` parsers + combined report | ✅ validated on real jobs |
| 2 | MCP server (`abaqus-mcp`, 13 tools) | ✅ working |
| 3 | Autonomous fix loop (deck-repair, stabilization, increment refinement) | ✅ working on real failures |
| 4 | Model authoring — CAD (STEP/IGES) import + auto-mesh + physics from a spec | ✅ working end-to-end |
| 4b | Parametric geometry library (block/plate/cylinder/notched bar/L-bracket) | ✅ working end-to-end |
| 4c | Results extraction from .odb (peak stress/disp, PEEQ/yield, reaction force) | ✅ working |
| 5 | Local-LLM desktop client (Ollama/llama.cpp) | ⏳ later |

## Architecture

Two Python interpreters, kept strictly separate:

- **Engine + MCP server** run on **system Python 3.11**.
- Anything handed to the Abaqus kernel (`abaqus python`, `abaqus cae -noGUI`)
  must be **Python 2.7** (Abaqus 2022) and lives under `abaqus_mcp/scripts_py27/`,
  invoked as a subprocess — never imported.

Model authoring is **hybrid**: CAE Python builds/meshes geometry → exports a flat
`.inp` → the solver runs the deck → **error-correction happens on the transparent
keyword deck** (easy to parse and patch), not on Python tracebacks.

### Model authoring (Phase 4)
Describe a job as a **simulation spec** (JSON) — geometry (STEP/IGES), mesh,
materials, section, steps, BCs and loads. Loads/BCs attach to faces via
coordinate-free selectors (`xmin`…`zmax`, or an explicit `box`) resolved against
the part's bounding box. The Py2.7 CAE builder imports the CAD, meshes it, applies
everything, and exports a flat `.inp`; the self-correcting loop runs it.
Geometry can also be **parametric** (no CAD file): set
`geometry: {type: "parametric", shape: ..., params: {...}}`. Shapes: `block`,
`beam`, `plate`, `cylinder`, `notched_bar`, `l_bracket`. See
`abaqus_mcp/spec.py` (schema + `example_spec()` / `example_parametric_spec()`)
and `abaqus_mcp/scripts_py27/build_from_spec.py` (the CAE builder). Try them:
`python tests/demo_cad_pipeline.py` and `python tests/demo_parametric.py notched_bar`.

### The self-correcting loop
Two nested loops. The inner one patches the **deck**; the outer one rebuilds the
**mesh**, because a distorted or inverted element is not something any edit to
`*STATIC` can repair.

```
                    ┌──────────────── outer loop (spec) ────────────────┐
spec → CAE build → .inp → ┌── inner loop (deck) ──┐                     │
                          │ run → parse .sta/.msg │                     │
                          │  /.dat → classify →   │                     │
                          │  patch deck → resubmit│                     │
                          └───────────┬───────────┘                     │
                                      │ mesh-shaped failure?            │
                                      └──→ refine seed size → rebuild ──┘
   (both bounded; every attempt's deck + report is kept for audit)
```
### Results extraction (Phase 4c)
After a job COMPLETES, `abaqus_mcp/results.py` runs the Py2.7 extractor
(`abaqus_mcp/scripts_py27/extract_odb.py`) under `abaqus python` (no CAE license needed) to
pull per-step peak von Mises stress, peak displacement, equivalent plastic strain
(PEEQ → yielded?), and net reaction force from the `.odb`. The `run_*` /
`build_and_simulate` MCP tools append this automatically; `get_results` fetches
it on demand.

Deck-level fix rules (`abaqus_mcp/fixes.py`), applied highest-priority first:
- **unknown_keyword_repair** — fuzzy-corrects a misspelled `*KEYWORD`. Only when
  the match is strong; an unfamiliar-but-valid keyword is left alone.
- **duplicate_definition** — drops *identical* repeat definitions. Two blocks
  defining the same name differently are a real conflict and are kept.
- **deck_name_repair** — fuzzy-corrects mistyped set/material references.
- **rigid_body_stabilization** — adds `STABILIZE` for zero-pivot / singular models.
- **instability_damping** — damps negative eigenvalues (buckling, snap-through)
  with an escalating `STABILIZE`.
- **convergence_refinement** — shrinks the initial/min time increment, raises the
  increment cap, and escalates to stabilization for non-converging steps.

Mesh-level repair (`abaqus_mcp/meshfix.py`) refines the spec's seed size and
rebuilds when the deck cannot express the problem (negative Jacobian, excessive
distortion, malformed connectivity) or when the CAE build itself fails.

### Converged is not correct
A remedy that buys convergence by changing the physics says so. `instability_damping`
can hold a model on the **unstable** branch — verified on a cantilever at 1.85×
its Euler load, which converged to 0.14 mm of lateral deflection instead of
buckling. Runs repaired that way report as `SUCCEEDED (with caveats)` and name
the risk, rather than passing silently.

Equally, failures with no *safe* automatic repair are not guessed at. Inventing
an elastic modulus or a shell thickness produces a deck that converges to a
meaningless answer, so `missing_material`, `missing_section`, `element_definition`
and `overconstraint` instead yield guidance naming what you must supply — and,
where the parsers captured them, the offending nodes, elements and DOFs.

## Layout
```
abaqus_mcp/
    config.py        # locate Abaqus, manage run dirs (env-var overridable)
    runner.py        # stage + run jobs headless (Windows cmd /c abaqus.bat)
    report.py        # combined JobReport over the three parsers
    inp.py           # edit-friendly keyword-deck model
    fixes.py         # failure -> fix rules
    loop.py          # autonomous run/diagnose/fix/retry loop
    results.py       # .odb extraction (peak stress/disp/PEEQ, reaction force)
    authoring.py     # spec -> meshed model -> flat .inp, via the CAE builder
    spec.py          # simulation-spec schema + validation
    server.py        # MCP server (stdio)
    meshfix.py       # spec-level repair: refine the mesh and rebuild
    parsers/         # sta.py, msg.py, dat.py
    scripts_py27/    # Py2.7 CAE/ODB scripts -- data files, never imported,
                     # shipped inside the package so a wheel is self-contained
tests/
    models/          # validation + deliberately-broken decks
    fixtures/        # real solver output the parser tests read
    test_parsers_smoke.py
    test_fix_rules.py
    test_meshfix.py
    test_spec.py
    demo_autocorrect.py
runs/                # job output (gitignored)
```

## Requirements

- **Abaqus** (developed against 2022) with a working license, on `PATH` or in
  `C:\SIMULIA\Commands`.
- **Python 3.9+** for the server. This is *separate* from the Python 2.7 that
  Abaqus bundles — do not install anything into the Abaqus interpreter.

## Install

```bash
pip install abaqus-mcp
```

That provides the `abaqus-mcp` command, which is what an MCP client launches.
Or skip installing altogether and let [uv](https://docs.astral.sh/uv/) fetch it
on demand:

```bash
uvx --from abaqus-mcp abaqus-mcp
```

> **Windows note.** `uv` can fail to install this with
> `Failed to install: pywin32-...whl ... being used by another process`.
> `pywin32` is a dependency of `mcp` on Windows, and uv's extraction races with
> on-access virus scanning. `pip install abaqus-mcp` is unaffected — use it
> instead. (Reproduced with both `uvx` and `uv tool install`; not specific to
> this package.)

### Docker

A container image is provided, but read this before reaching for it: **the image
cannot contain Abaqus.** Abaqus is licensed commercial software and cannot be
redistributed, so the image ships the agent alone. Out of the box you get a
server that starts, advertises its tools, validates specs and parses solver
output — but cannot run a job.

To actually solve, mount the host's Abaqus installation and point the agent at
it (the licence server must also be reachable from inside the container):

```bash
docker run --rm -i -v /opt/SIMULIA:/opt/SIMULIA:ro -v "$PWD/runs:/work/runs" -e ABAQUS_AGENT_COMMAND=/opt/SIMULIA/Commands/abaqus abaqus-mcp
```

Call `check_environment` first — it reports exactly what was found and what to
set if the launcher is missing. For a normal desktop install, the plain
`pip install` above is simpler and works better.

### From source

For development, or to run the demos and tests (which are not in the wheel):

```bash
git clone https://github.com/rutwikg/abaqus-mcp.git
```

```bash
cd abaqus-mcp && pip install -e .
```

## Verify it works

Check that the server can see your Abaqus installation — this prints the
resolved launcher and exits, without consuming a license token:

```bash
python -c "from abaqus_mcp.config import CONFIG; print(CONFIG.command, CONFIG.available())"
```

If that prints `False`, set `ABAQUS_AGENT_COMMAND` to your launcher's full path.

Then run the unit tests, which need no Abaqus license:

```bash
python tests/test_fix_rules.py && python tests/test_parsers_smoke.py && python tests/test_spec.py
```

And a real self-correcting run against the solver — this one *does* need a
license. It submits a deliberately broken deck and repairs it:

```bash
python tests/demo_autocorrect.py
```

Directly from Python:
```python
from abaqus_mcp.loop import autocorrect_run
result = autocorrect_run("path/to/model.inp", max_iters=5)
print(result.narrative())
```

## Use from an MCP client

Copy [`.mcp.json.example`](.mcp.json.example) to `.mcp.json` (Claude Code) or
merge it into `claude_desktop_config.json` (Claude Desktop), then edit the paths.

**The config must match how you installed it.** `pip install` and
`uv tool install` put an `abaqus-mcp` executable on `PATH`, so the client can
call it by name. `uvx` does not -- it runs the package from a temporary
environment and installs nothing -- so the client has to invoke `uvx` itself.

After `pip install abaqus-mcp` or `uv tool install abaqus-mcp`:

```json
{
  "mcpServers": {
    "abaqus-mcp": {
      "command": "abaqus-mcp",
      "args": [],
      "env": { "ABAQUS_AGENT_RUNS_DIR": "/where/job/output/should/go" }
    }
  }
}
```

Using `uvx`, with nothing installed:

```json
{
  "mcpServers": {
    "abaqus-mcp": {
      "command": "uvx",
      "args": ["--from", "abaqus-mcp", "abaqus-mcp"],
      "env": { "ABAQUS_AGENT_RUNS_DIR": "/where/job/output/should/go" }
    }
  }
}
```

Then ask for `check_environment` first — it reports whether the Abaqus launcher
was found — followed by `run_simulation`, `autocorrect_simulation`, or
`build_and_simulate`.

### Tools

`check_environment`, `run_simulation`, `autocorrect_simulation`,
`get_job_status`, `read_job_file`, `list_jobs`, `get_spec_template`,
`get_parametric_spec_template`, `validate_simulation_spec`, `build_model`,
`build_and_simulate`, `get_results`, `greeting`.

## Environment overrides
`ABAQUS_AGENT_COMMAND` (launcher path), `ABAQUS_AGENT_RUNS_DIR` (defaults to
`./runs` beside wherever the server was launched), `ABAQUS_AGENT_CPUS`,
`ABAQUS_AGENT_JOB_TIMEOUT`.

## Contributing

Open work is listed in [CONTRIBUTING.md](CONTRIBUTING.md), split by whether it
needs an Abaqus licence — several tasks don't. It also documents the one rule
that governs every fix: **never invent physics to make a job run.**

## License

**AGPL-3.0-or-later** — see [LICENSE](LICENSE). You may use, modify, and
redistribute this freely, but any distributed derivative — **including one
offered to users over a network** — must also be released under the AGPL with
source available. Attribution must be preserved.

If those terms don't work for you (for example, you want to build this into a
closed-source product), a separate commercial license is available — open an
issue to get in touch.

Academic use: please cite via [CITATION.cff](CITATION.cff).
