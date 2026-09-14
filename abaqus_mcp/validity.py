"""Physical-validity gate: is a COMPLETED run actually worth believing?

`report.succeeded` only means the solver reached the end of the step. In
explicit dynamics a job converges perfectly happily while artificial energy
carries the load, or while added mass supplies the momentum. Abaqus prints
COMPLETED either way, a novice ships the result, and an expert checks the
energy balance every single time.

This module encodes those checks. It is deliberately separate from `fixes`:
a fix rule answers "the job failed, what do I change?", whereas this answers
"the job passed, should I believe it?" -- and a negative answer re-opens a
loop that had already decided it was done.

Thresholds follow the conventional Abaqus/Explicit guidance. They are
defaults, not laws: `ValidityThresholds` is a dataclass so a caller with a
better-justified number can supply it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class ValidityThresholds:
    # Artificial (hourglass) strain energy. Above ~5% the deformation mode is
    # partly numerical; above 10% the result is not defensible.
    artificial_warn: float = 0.05
    artificial_fail: float = 0.10
    # Kinetic energy, for a crush that is meant to be quasi-static-ish. A real
    # impact is genuinely dynamic, so this only applies when the caller says so.
    kinetic_warn: float = 0.10
    kinetic_fail: float = 0.20
    # Total energy should be conserved; drift is normalised by peak ALLIE.
    etotal_drift_fail: float = 0.05


@dataclass
class ValidityVerdict:
    valid: bool
    checks: List[str] = field(default_factory=list)   # human-readable lines
    failures: List[str] = field(default_factory=list)  # machine labels
    metrics: Dict[str, float] = field(default_factory=dict)
    remedy: Optional[str] = None

    def summary(self) -> str:
        head = "PHYSICALLY VALID" if self.valid else "REJECTED -- converged but not believable"
        lines = [head]
        lines.extend("  " + c for c in self.checks)
        if self.remedy:
            lines.append("  remedy: " + self.remedy)
        return "\n".join(lines)


def _pct(x: float) -> str:
    return "%.1f%%" % (100.0 * x)


def assess(
    results: Dict[str, Any],
    thresholds: ValidityThresholds = ValidityThresholds(),
    quasi_static: bool = False,
) -> ValidityVerdict:
    """Judge extracted results (the dict from `extract_results`).

    Returns a verdict with `valid=False` when the run completed but the energy
    balance says the answer is contaminated. A run with no energy history is
    *not* failed -- absence of evidence is reported as such, because silently
    passing and silently failing are both worse than saying "not checked".
    """
    v = ValidityVerdict(valid=True)

    steps = [s for s in (results.get("steps") or []) if s.get("energy")]
    if not steps:
        v.checks.append(
            "energy balance NOT CHECKED -- no ALLIE/ALLAE history in the .odb. "
            "Request ALLIE ALLKE ALLAE ALLPD ALLSE ETOTAL as history output."
        )
        return v

    for s in steps:
        e = s["energy"]
        name = s.get("step", "?")

        ae = e.get("max_ALLAE_over_ALLIE")
        if ae is not None:
            v.metrics["%s.ALLAE/ALLIE" % name] = ae
            when = e.get("max_ALLAE_over_ALLIE_time")
            where = (" at t=%.4g" % when) if when is not None else ""
            if ae >= thresholds.artificial_fail:
                v.valid = False
                v.failures.append("artificial_energy")
                v.checks.append(
                    "[%s] ARTIFICIAL ENERGY %s of internal energy%s -- hourglassing "
                    "is carrying the load, not the material." % (name, _pct(ae), where)
                )
                v.remedy = (
                    "Refine the mesh, and use enhanced hourglass control "
                    "(hourglassControl=ENHANCED on the ElemType). Fully integrated "
                    "elements remove hourglassing entirely at higher cost."
                )
            elif ae >= thresholds.artificial_warn:
                v.checks.append(
                    "[%s] artificial energy %s of internal energy%s -- borderline; "
                    "the mode is probably right but the magnitudes are soft."
                    % (name, _pct(ae), where)
                )
            else:
                v.checks.append("[%s] artificial energy %s of internal energy -- OK"
                                % (name, _pct(ae)))

        ke = e.get("final_ALLKE_over_ALLIE")
        if ke is not None:
            v.metrics["%s.ALLKE/ALLIE" % name] = ke
            if quasi_static and ke >= thresholds.kinetic_fail:
                v.valid = False
                v.failures.append("kinetic_energy")
                v.checks.append(
                    "[%s] KINETIC ENERGY %s of internal energy at end of a "
                    "quasi-static step -- mass scaling is too aggressive or the "
                    "event is being driven too fast." % (name, _pct(ke))
                )
                if not v.remedy:
                    v.remedy = ("Reduce the mass-scaling factor or lengthen the step "
                                "time until ALLKE/ALLIE falls below 10%.")
            elif quasi_static and ke >= thresholds.kinetic_warn:
                v.checks.append("[%s] kinetic energy %s of internal energy -- "
                                "high for a quasi-static crush" % (name, _pct(ke)))

        drift = e.get("ETOTAL_drift_over_ALLIE")
        if drift is not None:
            v.metrics["%s.ETOTAL_drift" % name] = drift
            if drift >= thresholds.etotal_drift_fail:
                v.valid = False
                v.failures.append("energy_conservation")
                v.checks.append(
                    "[%s] TOTAL ENERGY drifted by %s of internal energy -- energy "
                    "is not conserved, so the solution is unreliable regardless of "
                    "what the status file says." % (name, _pct(drift))
                )
                if not v.remedy:
                    v.remedy = ("Check for unstable contact or excessive mass scaling; "
                                "reduce the stable time increment.")
    return v
