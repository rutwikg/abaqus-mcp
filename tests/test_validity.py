"""Solver-independent tests for the physical-validity gate.

The gate's whole purpose is to disagree with a green solver status, so these
feed it energy summaries shaped exactly like the ones extract_odb produces and
check it reaches the verdict an analyst would.

Reference numbers come from real runs on this machine (see
tests/models/hourglass_demo.inp), all of which Abaqus reported as COMPLETED:

    coarse C3D8R, relax stiffness, 1 elem through thickness -> ALLAE/ALLIE 99.9%
    refined C3D8R, enhanced,       4 elem through thickness -> ALLAE/ALLIE 31.0%
    refined C3D8 fully integrated, 4 elem through thickness -> ALLAE/ALLIE  0.0%
    shock_rupture (blast, tearing plate)                    -> ALLAE/ALLIE  0.7%
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abaqus_mcp.validity import ValidityThresholds, assess


def results_with(**energy):
    return {"ok": True, "steps": [{"step": "Step-1", "energy": energy}]}


def test_clean_run_is_valid():
    # shock_rupture: hourglassing negligible, energy conserved.
    r = results_with(max_ALLAE_over_ALLIE=0.0073, ETOTAL_drift_over_ALLIE=0.0012)
    v = assess(r)
    assert v.valid, v.summary()
    assert not v.failures
    print("OK clean run accepted")


def test_hourglassing_is_rejected():
    # The coarse C3D8R cantilever. Solver said COMPLETED; this must not.
    r = results_with(max_ALLAE_over_ALLIE=0.999, max_ALLAE_over_ALLIE_time=0.001731)
    v = assess(r)
    assert not v.valid, "99.9% artificial energy must be rejected"
    assert "artificial_energy" in v.failures
    assert v.remedy and "mesh" in v.remedy.lower()
    print("OK hourglassing rejected:", v.checks[0][:70])


def test_refinement_alone_still_rejected():
    # 31% is a 3x improvement and still not believable -- the gate must not be
    # satisfied by mere progress.
    v = assess(results_with(max_ALLAE_over_ALLIE=0.310))
    assert not v.valid, "31% artificial energy must still fail"
    print("OK partial improvement still rejected")


def test_borderline_warns_without_failing():
    v = assess(results_with(max_ALLAE_over_ALLIE=0.07))
    assert v.valid, "7% is borderline, not a failure"
    assert any("borderline" in c for c in v.checks), v.checks
    print("OK borderline warns but passes")


def test_missing_energy_history_is_reported_not_guessed():
    # Absence of evidence must read as 'not checked'. Silently passing and
    # silently failing are both worse than saying so.
    v = assess({"ok": True, "steps": [{"step": "Step-1"}]})
    assert v.valid
    assert any("NOT CHECKED" in c for c in v.checks), v.checks
    print("OK missing history reported honestly")


def test_kinetic_energy_only_judged_when_quasi_static():
    # shock_rupture is a blast: ALLKE/ALLIE of 2.5 is physically correct and
    # must not be flagged. The same number in a quasi-static crush is not.
    r = results_with(final_ALLKE_over_ALLIE=2.5)
    assert assess(r).valid, "a genuine impact must not be failed on kinetic energy"
    v = assess(r, quasi_static=True)
    assert not v.valid and "kinetic_energy" in v.failures
    print("OK kinetic check applies only to quasi-static")


def test_energy_drift_is_rejected():
    v = assess(results_with(max_ALLAE_over_ALLIE=0.01, ETOTAL_drift_over_ALLIE=0.078))
    assert not v.valid and "energy_conservation" in v.failures
    print("OK energy-conservation failure rejected")


def test_thresholds_are_overridable():
    strict = ValidityThresholds(artificial_fail=0.01)
    assert not assess(results_with(max_ALLAE_over_ALLIE=0.02), strict).valid
    assert assess(results_with(max_ALLAE_over_ALLIE=0.02)).valid
    print("OK thresholds overridable")


if __name__ == "__main__":
    test_clean_run_is_valid()
    test_hourglassing_is_rejected()
    test_refinement_alone_still_rejected()
    test_borderline_warns_without_failing()
    test_missing_energy_history_is_reported_not_guessed()
    test_kinetic_energy_only_judged_when_quasi_static()
    test_energy_drift_is_rejected()
    test_thresholds_are_overridable()
    print("\nAll validity-gate tests passed.")
