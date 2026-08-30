"""Failure -> fix rules that patch an .inp deck between solver runs.

Each rule inspects a :class:`JobReport` plus the current :class:`Deck` and, when
it recognises a failure it knows how to remedy, mutates the deck in place and
returns a :class:`FixAction` describing what it did and why. Rules escalate:
the same rule applied on a later attempt reaches for a stronger remedy.

The rules are deliberately conservative and physics-preserving -- they change
*numerical controls* (increment size, stabilization) or repair *deck
consistency* (mistyped names), never the intended loads/materials/BCs.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from .inp import Block, Deck
from .report import JobReport


@dataclass
class FixAction:
    rule: str
    description: str
    details: Dict[str, str] = field(default_factory=dict)


# --------------------------------------------------------------------------
# helpers for editing the (implicit) static step
# --------------------------------------------------------------------------

_SOLVER_STEP_KEYWORDS = ("STATIC", "VISCO", "COUPLED TEMPERATURE-DISPLACEMENT", "SOILS")


def _numeric_data_line(block: Block) -> Optional[int]:
    """Index of the first data line that looks like increment controls."""
    for i, dl in enumerate(block.data_lines):
        toks = [t for t in dl.replace(",", " ").split() if t]
        if toks and all(_is_number(t) for t in toks):
            return i
    return None


def _is_number(tok: str) -> bool:
    try:
        float(tok)
        return True
    except ValueError:
        return False


def _get_solver_step(deck: Deck) -> Optional[Block]:
    for kw in _SOLVER_STEP_KEYWORDS:
        b = deck.first(kw)
        if b is not None:
            return b
    return None


def _scale_increment_controls(
    deck: Deck, init_factor: float, min_factor: float
) -> Optional[Dict[str, str]]:
    """Shrink the initial and minimum time increment of the solver step.

    Returns a details dict (old/new data line) or None if there is no step.
    Also raises the step's max-increment cap so smaller increments still finish.
    """
    step = deck.first("STEP")
    solver = _get_solver_step(deck)
    if solver is None or step is None:
        return None

    # Raise the increment cap generously.
    step.set_param("INC", "100000")

    idx = _numeric_data_line(solver)
    if idx is None:
        # No explicit controls: install conservative ones (period assumed 1.0).
        new_line = "0.01, 1.0, 1e-08, 0.1"
        solver.data_lines.insert(0, new_line)
        return {"old_controls": "(defaults)", "new_controls": new_line}

    old = solver.data_lines[idx]
    toks = [t for t in old.replace(",", " ").split() if t]
    # Pad to 4: init, period, min, max
    vals = [float(t) for t in toks]
    while len(vals) < 4:
        if len(vals) == 1:
            vals.append(vals[0])          # period = init
        elif len(vals) == 2:
            vals.append(vals[1] * 1e-5)   # min
        elif len(vals) == 3:
            vals.append(vals[1])          # max = period
    init, period, tmin, tmax = vals[0], vals[1], vals[2], vals[3]
    new_init = max(init * init_factor, period * 1e-4)
    new_min = min(tmin * min_factor, new_init * 1e-3)
    new_max = min(tmax, period * 0.25)
    new_line = "%g, %g, %g, %g" % (new_init, period, new_min, new_max)
    solver.data_lines[idx] = new_line
    return {"old_controls": old.strip(), "new_controls": new_line}


def _add_stabilization(deck: Deck, factor: float) -> Optional[Dict[str, str]]:
    solver = _get_solver_step(deck)
    if solver is None:
        return None
    prev = solver.param("STABILIZE")
    solver.set_param("STABILIZE", "%g" % factor)
    if not solver.has_param("ALLSDTOL"):
        solver.set_param("ALLSDTOL", "0.05")
    return {"previous_stabilize": str(prev), "new_stabilize": "%g" % factor}


# --------------------------------------------------------------------------
# deck-consistency: fix dangling (mistyped) references
# --------------------------------------------------------------------------

_LOAD_BC_KEYWORDS = ("BOUNDARY", "CLOAD", "DLOAD", "DSLOAD", "TEMPERATURE")
_SECTION_KEYWORDS = ("SOLID SECTION", "SHELL SECTION", "BEAM SECTION", "MEMBRANE SECTION")

# Keywords we recognise. Not the whole Abaqus manual -- just enough that a
# deck this agent writes or repairs parses cleanly, and that a typo in a common
# keyword has something to match against. An unlisted-but-valid keyword is
# harmless: UnknownKeywordRule only rewrites when a close match exists, so a
# genuinely unfamiliar keyword scores low and is left alone.
ABAQUS_KEYWORDS = frozenset([
    # structure
    "HEADING", "PREPRINT", "SYSTEM", "INCLUDE", "PARAMETER", "RESTART",
    "PART", "END PART", "ASSEMBLY", "END ASSEMBLY", "INSTANCE", "END INSTANCE",
    # mesh + sets
    "NODE", "ELEMENT", "NSET", "ELSET", "SURFACE", "NCOPY", "NGEN", "ELGEN",
    # sections
    "SOLID SECTION", "SHELL SECTION", "BEAM SECTION", "MEMBRANE SECTION",
    "CONNECTOR SECTION", "GASKET SECTION", "BEAM GENERAL SECTION",
    # materials
    "MATERIAL", "ELASTIC", "PLASTIC", "DENSITY", "EXPANSION", "DAMPING",
    "CONDUCTIVITY", "SPECIFIC HEAT", "LATENT HEAT", "DEPVAR", "USER MATERIAL",
    "HYPERELASTIC", "VISCOELASTIC", "CREEP",
    "DAMAGE INITIATION", "DAMAGE EVOLUTION",
    # steps + procedures
    "STEP", "END STEP", "STATIC", "DYNAMIC", "VISCO", "BUCKLE", "FREQUENCY",
    "MODAL DYNAMIC", "HEAT TRANSFER", "COUPLED TEMPERATURE-DISPLACEMENT",
    "SOILS", "STEADY STATE DYNAMICS", "CONTROLS", "SOLVER CONTROLS", "MONITOR",
    # loads + BCs
    "BOUNDARY", "CLOAD", "DLOAD", "DSLOAD", "TEMPERATURE", "FIELD", "AMPLITUDE",
    "INITIAL CONDITIONS", "ORIENTATION", "DISTRIBUTION", "TRANSFORM",
    # interactions + constraints
    "TIE", "CONTACT PAIR", "CONTACT", "SURFACE INTERACTION", "SURFACE BEHAVIOR",
    "FRICTION", "CONTACT INCLUSIONS", "CONTACT EXCLUSIONS", "CONTACT CONTROLS",
    "RIGID BODY", "COUPLING", "KINEMATIC", "DISTRIBUTING", "MPC", "EQUATION",
    # output
    "OUTPUT", "NODE OUTPUT", "ELEMENT OUTPUT", "CONTACT OUTPUT", "ENERGY OUTPUT",
    "NODE PRINT", "EL PRINT", "NODE FILE", "EL FILE", "PRINT",
    # explicit-dynamics controls
    "BULK VISCOSITY", "MASS SCALING", "FIXED MASS SCALING",
    "VARIABLE MASS SCALING",
])


def _referenced_names(deck: Deck) -> Dict[str, List[Block]]:
    """Collect (name -> blocks referencing it) for materials and sets."""
    refs: Dict[str, Dict[str, List[Block]]] = {"material": {}, "set": {}}

    def add(cat: str, name: Optional[str], block: Block):
        if not name:
            return
        refs[cat].setdefault(name.upper(), []).append(block)

    for b in deck.blocks:
        if b.is_comment:
            continue
        if b.keyword in _SECTION_KEYWORDS:
            add("material", b.param("MATERIAL"), b)
            add("set", b.param("ELSET"), b)
        if b.keyword in _LOAD_BC_KEYWORDS:
            for dl in b.data_lines:
                first = dl.split(",")[0].strip()
                if first and not _is_number(first):
                    add("set", first, b)
    return refs


# --------------------------------------------------------------------------
# rules
# --------------------------------------------------------------------------


class FixRule:
    name = "rule"
    priority = 0  # lower runs first

    def applicable(self, report: JobReport, deck: Deck) -> bool:
        raise NotImplementedError

    def apply(
        self, report: JobReport, deck: Deck, attempt: int
    ) -> Optional[FixAction]:
        raise NotImplementedError


class DeckNameRepairRule(FixRule):
    """Correct mistyped set/material names by fuzzy-matching defined names."""

    name = "deck_name_repair"
    priority = 0

    def _dangling(self, deck: Deck):
        defined = deck.defined_names()
        defined_sets = defined["nset"] | defined["elset"]
        defined_mats = defined["material"]
        refs = _referenced_names(deck)
        problems = []  # (category, bad_name, candidates)
        for name in refs["material"]:
            if name not in defined_mats:
                problems.append(("material", name, defined_mats))
        for name in refs["set"]:
            if name not in defined_sets:
                problems.append(("set", name, defined_sets))
        return problems

    def applicable(self, report: JobReport, deck: Deck) -> bool:
        if report.succeeded:
            return False
        return bool(self._dangling(deck))

    def apply(self, report, deck, attempt):
        fixed = []
        for cat, bad, candidates in self._dangling(deck):
            match = difflib.get_close_matches(bad, list(candidates), n=1, cutoff=0.5)
            if match:
                n = deck.rename_token(bad, match[0])
                fixed.append("%s '%s' -> '%s' (%d refs)" % (cat, bad, match[0], n))
        if not fixed:
            return None
        return FixAction(
            rule=self.name,
            description="Repaired mistyped references: " + "; ".join(fixed),
            details={"fixes": "; ".join(fixed)},
        )


class SingularityRule(FixRule):
    """Add automatic stabilization for rigid-body / zero-pivot singularities."""

    name = "rigid_body_stabilization"
    priority = 1

    def applicable(self, report: JobReport, deck: Deck) -> bool:
        if report.succeeded:
            return False
        # Key on error-level causes so a merely-warned singularity in an
        # otherwise-healthy run does not trigger a spurious fix.
        cats = report.error_categories
        return ("numerical_singularity" in cats or "zero_pivot" in cats) and (
            _get_solver_step(deck) is not None
        )

    def apply(self, report, deck, attempt):
        factor = [2e-4, 1e-3, 5e-3][min(attempt, 2)]
        details = _add_stabilization(deck, factor)
        if details is None:
            return None
        return FixAction(
            rule=self.name,
            description=(
                "Added automatic stabilization (STABILIZE=%g) to suppress the "
                "singular/under-constrained mode." % factor
            ),
            details=details,
        )


class ConvergenceRule(FixRule):
    """Refine increment controls (and stabilize) for convergence failures."""

    name = "convergence_refinement"
    priority = 2

    _CONV_CATS = {
        "convergence",
        "min_time_increment",
        "too_many_attempts",
        "plasticity",
        "excessive_distortion",
        "contact",
    }

    def applicable(self, report: JobReport, deck: Deck) -> bool:
        if report.succeeded:
            return False
        if _get_solver_step(deck) is None:
            return False
        if set(report.error_categories) & self._CONV_CATS:
            return True
        # Aborted with cutbacks but no clear category still points here.
        return report.msg.num_cutbacks > 0 and not report.errors

    def apply(self, report, deck, attempt):
        if attempt == 0:
            details = _scale_increment_controls(deck, init_factor=0.1, min_factor=0.01)
            desc = "Reduced initial/minimum time increment and raised the increment cap."
        elif attempt == 1:
            details = _add_stabilization(deck, 2e-4)
            desc = "Added automatic stabilization to aid convergence."
        else:
            details = _scale_increment_controls(deck, init_factor=0.05, min_factor=0.005)
            d2 = _add_stabilization(deck, 1e-3)
            if details is None:
                details = d2
            desc = "Further reduced increments and increased stabilization."
        if details is None:
            return None
        return FixAction(rule=self.name, description=desc, details=details)


class UnknownKeywordRule(FixRule):
    """Fuzzy-correct a mistyped *KEYWORD against the known Abaqus keyword set.

    Abaqus aborts at input-parse time on an unrecognised keyword, so this fires
    before anything is solved. Purely a spelling repair: the parameters and data
    lines are left untouched, so the intended physics is preserved.
    """

    name = "unknown_keyword_repair"
    priority = 0

    # Deliberately stricter than the 0.5 used for user-chosen set/material
    # names. Keywords come from a fixed vocabulary, so a real typo scores high;
    # a low score means we do not understand the deck and should not guess.
    _CUTOFF = 0.8

    def _unknown(self, deck: Deck):
        """Return [(bad_keyword, best_match)] for keywords we can repair."""
        out = []
        for b in deck.blocks:
            if b.is_comment or not b.keyword or b.keyword in ABAQUS_KEYWORDS:
                continue
            match = difflib.get_close_matches(
                b.keyword, ABAQUS_KEYWORDS, n=1, cutoff=self._CUTOFF
            )
            if match:
                out.append((b, match[0]))
        return out

    def applicable(self, report: JobReport, deck: Deck) -> bool:
        if report.succeeded:
            return False
        return bool(self._unknown(deck))

    def apply(self, report, deck, attempt):
        fixed = []
        for block, good in self._unknown(deck):
            fixed.append("*%s -> *%s" % (block.keyword, good))
            block.keyword = good
        if not fixed:
            return None
        return FixAction(
            rule=self.name,
            description="Corrected misspelled keyword(s): " + "; ".join(fixed),
            details={"fixes": "; ".join(fixed)},
        )


class DuplicateDefinitionRule(FixRule):
    """Drop exact-duplicate definition blocks.

    Only *identical* repeats are removed -- same keyword, same parameters, same
    data. A redundant copy cannot carry information, so dropping it is safe.
    Two blocks that define the same name *differently* are a genuine conflict
    the author has to resolve, and are left alone for the diagnosis path.
    """

    name = "duplicate_definition"
    priority = 0

    _DEFINITION_KEYWORDS = ("MATERIAL", "NSET", "ELSET", "SURFACE", "AMPLITUDE")

    def _duplicates(self, deck: Deck) -> List[Block]:
        seen: Dict[str, Block] = {}
        dupes: List[Block] = []
        for b in deck.blocks:
            if b.is_comment or b.keyword not in self._DEFINITION_KEYWORDS:
                continue
            key = b.render()
            if key in seen:
                dupes.append(b)
            else:
                seen[key] = b
        return dupes

    def applicable(self, report: JobReport, deck: Deck) -> bool:
        if report.succeeded:
            return False
        return bool(self._duplicates(deck))

    def apply(self, report, deck, attempt):
        dupes = self._duplicates(deck)
        if not dupes:
            return None
        labels = []
        for b in dupes:
            name = b.param("NAME") or b.param(b.keyword) or "?"
            labels.append("*%s %s" % (b.keyword, name))
            deck.blocks.remove(b)
        return FixAction(
            rule=self.name,
            description="Removed %d exact-duplicate definition(s): %s"
            % (len(dupes), "; ".join(labels)),
            details={"removed": "; ".join(labels)},
        )


DEFAULT_RULES: List[FixRule] = [
    UnknownKeywordRule(),
    DuplicateDefinitionRule(),
    DeckNameRepairRule(),
    SingularityRule(),
    ConvergenceRule(),
]


# --------------------------------------------------------------------------
# diagnosis for failures that CANNOT be safely auto-repaired
# --------------------------------------------------------------------------

# Categories where inventing a fix would mean inventing physics. Guessing an
# elastic modulus or a shell thickness to make a job run produces a deck that
# converges to a meaningless answer -- worse than failing. For these we stop
# and say precisely what the author must supply.
UNFIXABLE_GUIDANCE: Dict[str, str] = {
    "missing_material": (
        "A section references a material that is not defined, and no similarly "
        "named material exists to correct it to. Add a *MATERIAL block (with at "
        "least *ELASTIC) for it -- its properties cannot be guessed."
    ),
    "missing_section": (
        "Elements have no section assignment. Add a *SOLID SECTION / *SHELL "
        "SECTION (etc.) binding the element set to a material; the thickness "
        "and material choice are modelling decisions, not defaults."
    ),
    "element_definition": (
        "Element connectivity is invalid -- the mesh itself is malformed. "
        "Re-generate the mesh rather than patching the deck."
    ),
    "negative_jacobian": (
        "Elements are inverted or badly shaped (negative Jacobian). This is a "
        "meshing failure; re-mesh, ideally with a smaller seed size or a "
        "different element order."
    ),
    "overconstraint": (
        "Conflicting constraints act on the same degrees of freedom (e.g. a "
        "*TIE and a *BOUNDARY on the same nodes). Removing one changes the "
        "intended model, so this needs an explicit decision."
    ),
}


def diagnose(report: JobReport) -> List[str]:
    """Actionable guidance for report categories that have no safe auto-fix."""
    seen = []
    for cat in report.error_categories or report.categories:
        if cat in UNFIXABLE_GUIDANCE and cat not in seen:
            seen.append(cat)
    return ["%s: %s" % (c, UNFIXABLE_GUIDANCE[c]) for c in seen]


def choose_and_apply(
    report: JobReport,
    deck: Deck,
    attempt_counts: Dict[str, int],
    rules: Optional[List[FixRule]] = None,
) -> Optional[FixAction]:
    """Pick the highest-priority applicable rule and apply it to ``deck``.

    ``attempt_counts`` tracks how many times each rule has already fired so the
    rule can escalate. Mutates ``deck`` and ``attempt_counts`` in place.
    """
    rules = rules or DEFAULT_RULES
    for rule in sorted(rules, key=lambda r: r.priority):
        if rule.applicable(report, deck):
            n = attempt_counts.get(rule.name, 0)
            action = rule.apply(report, deck, attempt=n)
            if action is not None:
                attempt_counts[rule.name] = n + 1
                return action
    return None
