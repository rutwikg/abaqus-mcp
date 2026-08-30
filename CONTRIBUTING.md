# Contributing

Contributions welcome. This project automates a commercial solver, so most of
the interesting work needs an Abaqus licence — but a useful amount does not, and
those tasks are marked **no licence needed** below.

## Getting set up

```bash
git clone https://github.com/rutwikg/abaqus-mcp.git
```

```bash
cd abaqus-mcp && pip install -e .
```

Run the tests. All four are plain scripts — no pytest, no solver, no licence:

```bash
python tests/test_fix_rules.py && python tests/test_meshfix.py && python tests/test_parsers_smoke.py && python tests/test_spec.py
```

Check that your Abaqus is discoverable (this consumes no licence token):

```bash
python -c "from abaqus_mcp.config import CONFIG; print(CONFIG.command, CONFIG.available())"
```

## The one rule that matters

**A fix must never invent physics.**

Rules may change *numerical controls* (increment size, damping) or repair *deck
consistency* (a misspelled keyword, a dangling reference). They must not invent
an elastic modulus, a shell thickness, or a boundary condition to make a job run.
A deck that converges to a meaningless answer is worse than one that fails: the
user cannot tell it went wrong.

When a remedy buys convergence at the cost of fidelity, it must say so. Set
`FixAction.caveat` and the loop will report `SUCCEEDED (with caveats)`. The
existing example is `instability_damping`: on a cantilever at 1.85× its Euler
load it converged onto the *unstable* branch — 0.14 mm of lateral deflection
instead of buckling. Equilibrium was satisfied, the solver was happy, and the
answer was misleading.

When there is no safe fix, add an entry to `UNFIXABLE_GUIDANCE` in
`abaqus_mcp/fixes.py` naming what the author must supply, rather than guessing.

## How the pieces fit

- `parsers/{sta,msg,dat}.py` — classify raw solver output into categories
- `report.py` — combine the three into one `JobReport`
- `fixes.py` — **deck**-level rules; patch the keyword deck and re-run
- `meshfix.py` — **spec**-level repair; refine the mesh and rebuild through CAE
- `loop.py` / `authoring.py` — the inner and outer loops respectively
- `scripts_py27/` — runs inside Abaqus's bundled Python 2.7. Never imported by
  Python 3; passed to Abaqus by absolute path. Keep it Py2-compatible.

Adding a deck rule means subclassing `FixRule`, implementing `applicable()` and
`apply()`, and appending to `DEFAULT_RULES`. Two things to get right:

- **Key on `report.succeeded`, not diagnostic level, to avoid touching healthy
  runs.** Abaqus frequently reports the *cause* of a failure as a warning while
  the error is the downstream symptom — negative eigenvalues and excessive
  distortion both behave this way. A rule keyed on error level alone will never
  fire on the real failure. (This was a real bug; see `instability_damping`.)
- **Escalate.** `apply()` receives `attempt`, the number of times your rule has
  already fired this run. Reach for a stronger remedy rather than repeating.

---

# Open work

## Good first issues — no licence needed

**Investigate the `input_other` catch-all.** Real runs emit this category, and
it may be hiding fixable cases behind a generic label. Read
`parsers/dat.py::_CATEGORY_PATTERNS`, find what falls through, and split out
anything with a deterministic remedy. Sample output is in `tests/fixtures/`.

**Extend the keyword vocabulary.** `ABAQUS_KEYWORDS` in `fixes.py` covers common
keywords, not the whole manual. Missing entries are harmless (an unknown keyword
is left alone), but a broader set means more typos get caught. Purely additive
and easy to test.

**More parser fixtures.** The parsers are only exercised against one solved job.
If you have `.sta`/`.msg`/`.dat` from failures we don't cover, they make
excellent test data. **Scrub the Abaqus licence banner first** — it names the
licensee (see `tests/fixtures/validate_cube/`, where it reads `<licensee>`).

**Port the tests to pytest.** They are currently hand-rolled `assert` scripts
with a `__main__` block. Fine, but pytest would give parametrisation and better
failure output. Keep them runnable without a solver.

## Needs an Abaqus licence

**Verify the mesh-repair path fires for real.** ⚠️ *Known gap.* The outer loop's
orchestration is covered by scripted tests, and two real runs confirm it never
falsely remeshes a healthy job — but the repair itself has never fired on a
genuine CAE mesh failure. Abaqus's tet mesher handled every degenerate case
attempted (a 0.25 mm notch at an 18 mm seed) by locally refining. Messy imported
CAD with sliver faces is the likely trigger. If you can produce a real
`negative_jacobian`, that deck is a valuable test fixture.

**Contact-specific fix rules.** `contact` failures currently fall through to
generic increment refinement. Real remedies differ: adjusting initial
over-closure, softening contact stiffness, adding `*CONTACT CONTROLS`,
`ADJUST=` on the tie. Needs someone who knows contact failure modes.

**Auto-switch to `*STATIC, RIKS` for genuine post-buckling.** Currently only
*suggested* in the damping caveat, because changing the procedure changes what
analysis is being run. If it can be done with the user's consent — or detected
reliably enough to justify it — that would close a real gap.

**Validate damping with an energy check.** The cleanest fix for the
converged-to-the-unstable-branch problem: extract `ALLSD` (stabilization energy)
and `ALLIE` (internal energy) in `scripts_py27/extract_odb.py` and have the loop
verify the ratio automatically instead of relying on the user to check. Turns a
written caveat into an enforced one.

**More parametric shapes.** `scripts_py27/build_from_spec.py::make_parametric_part`
has block, beam, plate, cylinder, notched bar and L-bracket. Plates with holes,
tubes, and stiffened panels would all be useful. Keep new shapes axis-aligned so
the `xmin`…`zmax` face selectors stay intuitive, and note that the face-selection
bounding box must come from mesh nodes, not vertices — curved shapes have
almost no vertices.

## Larger pieces

**Explicit dynamics (`*DYNAMIC, EXPLICIT`).** Currently everything targets
Abaqus/Standard implicit. Explicit needs its own failure taxonomy — mass
scaling, stable time increment, energy balance, hourglassing — and its own fix
rules. This is the prerequisite for crash work.

**Other solvers.** The loop architecture (parse output → classify → patch input
→ retry) is solver-agnostic; the *rules* are not. `STABILIZE` and time-increment
refinement are Abaqus keyword semantics. A second solver needs its own failure
taxonomy written by someone who knows that solver's output.

**Thermal and coupled analyses.** The spec schema and CAE builder are
structural-only today.

## Licence

By contributing you agree your work is licensed under **AGPL-3.0-or-later**, the
same as the project.
