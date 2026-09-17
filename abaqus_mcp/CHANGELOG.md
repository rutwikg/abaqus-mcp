# Changelog

All notable changes to abaqus-mcp. Readable from any MCP client via the
`whats_new` tool, so an upgrade does not silently change the tool list.

Format follows [Keep a Changelog](https://keepachangelog.com/); this project
uses [semantic versioning](https://semver.org/).

## [0.3.0] - 2026-09-17

Four new tools, and the first ones that judge a result rather than just
produce one.

### Added

- **`check_validity`** — decides whether a COMPLETED job is believable. A green
  solver status only means the analysis reached the end of the step; this reads
  the energy balance and rejects runs where artificial (hourglass) energy is
  carrying the load, where kinetic energy dominates a quasi-static event, or
  where total energy is not conserved. Verified against four real runs, every
  one of which Abaqus reported as successful: a coarse reduced-integration
  cantilever came back at 99.9% artificial energy, refining it reached 31% and
  was still rejected, and only changing the element formulation produced a
  believable 0.0%.
- **`inspect_deck`** — summarises what a deck actually contains (mesh,
  materials, sections, sets, steps, loads, interactions, output requests) and
  names any references pointing at things the deck never defines. Reads the
  file only: no solver, no licence token.
- **`start_sweep`** — runs a parametric sweep, applying the fix rules and then
  the validity gate to each design, and parking whatever needs judgement.
  Resumable: re-running a sweep name skips designs that already finished.
- **`whats_new`** — this changelog, readable from the client.
- **Energy extraction.** `extract_odb` now pulls whole-model energy histories
  (`ALLIE ALLKE ALLAE ALLPD ALLSE ALLDMD ETOTAL`) and the ratios an analyst
  checks. Ratios are only evaluated once internal energy passes 5% of its peak,
  because early in an impact it is near zero and an unguarded ratio is
  enormous and meaningless.

### Changed

- Nothing is dropped silently any more. Every design in a sweep ends as
  `valid`, `parked` or `abandoned`, each with a recorded reason — including
  when the sweep runner itself throws, which parks the traceback rather than
  leaving a gap.
- A fix that buys convergence at the cost of fidelity now says so.
  `instability_damping` can hold a model on the *unstable* branch, so runs
  repaired that way report as `SUCCEEDED (with caveats)` rather than passing
  quietly.

### Not shipped

- **Image rendering is written but not exposed.** `abaqus_mcp/render.py` and
  `scripts_py27/capture_odb.py` produce a PNG contour plot on the deformed
  shape, but `abaqus viewer noGUI` dies with an access violation
  (`EXCEPTION_ACCESS_VIOLATION` in `ABQvwrK`) on the machine this was developed
  on -- a crash below the Python layer, most likely offscreen rendering with no
  display context. Rather than advertise a tool that fails, it is left
  unregistered. If it works on your install, registering it is a one-line
  change. Two things already learned and encoded there: a tensor field needs
  `refinement=(INVARIANT, 'Mises')` with that exact capitalisation, and
  `printToFile` silently writes nothing if the filename contains a dot.
- Rendering is also not free: `abaqus viewer` checked out a **`cae`** token on
  this licence server, not a cheaper viewer one.

### Notes

- A run with no energy history is reported as **not checked**, never as passed.
  Silently passing and silently failing are both worse than saying so.
- `apply_reasoned_fix` refuses edits that delete an output request or most of
  the deck. A run is judged on its output, so removing the output is not a fix.

## [0.2.2] - 2026-09-06

### Fixed

- The MCP client config only worked for one install method. `pip install` and
  `uv tool install` put `abaqus-mcp` on PATH so the client can call it by name;
  `uvx` installs nothing, so the client has to invoke `uvx` itself. Both forms
  are now documented.
- Documented that `uv` cannot install this on Windows (tested 0.12.5): it fails
  unpacking `pywin32`, a dependency of `mcp` rather than of this package. `pip`
  is unaffected.

## [0.2.1] - 2026-08-31

### Fixed

- The server advertised the wrong version. `MCPServer` was constructed with a
  hardcoded `version="0.1.0"` while the package was 0.2.0, so every client was
  told the wrong number. Now derived from `__version__`, with a test asserting
  they stay in step.
- `check_environment` explains what to do when no Abaqus launcher is found
  instead of reporting `Available: False` and leaving you to guess.

### Added

- A Dockerfile. The image deliberately does **not** contain Abaqus, which is
  licensed software that cannot be redistributed: mount the host installation
  and set `ABAQUS_AGENT_COMMAND` to run jobs.

## [0.2.0] - 2026-08-30

### Added

- `unknown_keyword_repair` — fuzzy-corrects a misspelled `*KEYWORD` against a
  known vocabulary, with a strict threshold so an unfamiliar-but-valid keyword
  is left alone.
- `duplicate_definition` — drops *identical* repeat definitions only. Two
  blocks defining the same name differently are a real conflict and are kept.
- `instability_damping` — damps negative eigenvalues (buckling, snap-through).
- Mesh-level repair: a negative Jacobian or a distorted element is a property
  of the mesh, so the remedy is to refine the spec and rebuild through CAE
  rather than to keep editing solver controls.

### Changed

- Failures with no safe automatic repair are no longer guessed at. Inventing an
  elastic modulus or a shell thickness produces a deck that converges to a
  meaningless answer, so `missing_material`, `missing_section`,
  `element_definition` and `overconstraint` yield guidance naming what you must
  supply — including the offending nodes, elements and DOFs.
- Rules key on whether the job failed, not on diagnostic level. Abaqus reports
  some failure *causes* as warnings while the error is the downstream symptom;
  keying on error level meant `instability_damping` never fired on a real
  buckling job.

## [0.1.1] - 2026-08-30

### Fixed

- Install instructions and packaging metadata.

## [0.1.0] - 2026-08-30

First public release: solver runner, `.sta`/`.msg`/`.dat` parsers, the
autonomous fix loop, model authoring from a JSON spec (parametric shapes or
imported STEP/IGES), and results extraction from the `.odb`.
