"""Solver-independent tests for the spec-level (mesh) repair layer.

These cover the decision logic only -- whether a failure is recognised as a
mesh problem and how the spec is refined -- so no CAE token or solver run is
needed.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abaqus_mcp.meshfix import (
    MESH_FAILURE_CATEGORIES,
    is_mesh_failure,
    mesh_failure_categories,
    refine_mesh,
)
from abaqus_mcp.parsers import Diagnostic, JobStatus, MsgReport, StaReport
from abaqus_mcp.parsers.dat import DatReport
from abaqus_mcp.report import JobReport
from abaqus_mcp.spec import example_parametric_spec


def report_with(diags, status=JobStatus.ABORTED):
    return JobReport(
        job_name="t", job_dir=Path("."), status=status,
        sta=StaReport(status=status),
        msg=MsgReport(diagnostics=list(diags),
                      num_error_messages=sum(d.kind == "error" for d in diags)),
        dat=DatReport(),
    )


def diag(category, kind="error"):
    return Diagnostic(kind=kind, category=category, text=category, source="msg")


def test_recognises_mesh_failures():
    for cat in MESH_FAILURE_CATEGORIES:
        assert is_mesh_failure(report_with([diag(cat)])), cat
    print("OK recognises:", sorted(MESH_FAILURE_CATEGORIES))


def test_distortion_warning_on_failed_job_counts():
    # Distortion is often a warning while the error is the downstream
    # convergence failure -- the same pattern as negative eigenvalues.
    r = report_with([diag("excessive_distortion", kind="warning"),
                     diag("too_many_attempts")])
    assert is_mesh_failure(r), r.categories
    print("OK distortion-warning-counts")


def test_convergence_failure_is_not_a_mesh_failure():
    r = report_with([diag("min_time_increment")])
    assert not is_mesh_failure(r), "must not remesh a pure convergence failure"
    print("OK convergence-is-not-mesh")


def test_success_is_never_a_mesh_failure():
    r = report_with([diag("excessive_distortion", kind="warning")],
                    status=JobStatus.COMPLETED)
    assert not is_mesh_failure(r), "a completed run must not trigger a remesh"
    print("OK success-never-remeshed")


def test_missing_report_is_safe():
    assert is_mesh_failure(None) is False
    assert mesh_failure_categories(None) == []
    print("OK none-report-safe")


def test_refine_ladder_is_monotonic_and_bounded():
    spec = example_parametric_spec()
    original = float(spec["mesh"]["size"])
    sizes = []
    for attempt in range(5):
        out = refine_mesh(spec, attempt)
        if out is None:
            break
        patched, fix = out
        sizes.append(patched["mesh"]["size"])
        # Refinement is measured against the ORIGINAL, so the caller's spec
        # must never be mutated in place.
        assert spec["mesh"]["size"] == original, "refine_mesh mutated its input"
    assert sizes == sorted(sizes, reverse=True), sizes
    assert all(s < original for s in sizes), (original, sizes)
    assert len(sizes) == 3, "ladder should exhaust, not refine forever: %r" % sizes
    print("OK refine ladder:", original, "->", sizes)


def test_refine_reports_element_growth():
    spec = example_parametric_spec()
    _, fix = refine_mesh(spec, 0)
    assert "approx_element_growth" in fix.details, fix.details
    # Halving the seed is ~8x the elements in 3D; the user should be told.
    assert fix.details["approx_element_growth"] == "8x", fix.details
    assert fix.caveat and "element count" in fix.caveat, fix.caveat
    print("OK growth reported:", fix.details["approx_element_growth"])


def test_refine_declines_without_a_usable_size():
    assert refine_mesh({"mesh": {}}, 0) is None
    assert refine_mesh({}, 0) is None
    assert refine_mesh({"mesh": {"size": "coarse"}}, 0) is None
    assert refine_mesh({"mesh": {"size": 0}}, 0) is None
    print("OK declines-without-size")


def _fake_pipeline(run_outcomes, build_ok=True):
    """Patch authoring's build+run with scripted outcomes.

    Returns (result, seeds_tried). ``run_outcomes`` is one entry per solve:
    either 'mesh_fail', 'other_fail' or 'ok'.
    """
    from abaqus_mcp import authoring
    from abaqus_mcp.loop import LoopResult

    seeds, calls = [], {"n": 0}

    def fake_build(spec, job_name=None, cfg=None, timeout_s=1800):
        seeds.append(spec["mesh"]["size"])
        if not build_ok:
            return {"ok": False, "inp": None, "errors": ["mesh failed"],
                    "stats": {}, "log": ""}
        return {"ok": True, "inp": "x.inp", "errors": [], "stats": {}, "log": ""}

    def fake_run(inp, job_name=None, max_iters=5, cpus=1, cfg=None):
        outcome = run_outcomes[min(calls["n"], len(run_outcomes) - 1)]
        calls["n"] += 1
        if outcome == "ok":
            rep = report_with([], status=JobStatus.COMPLETED)
            return LoopResult(job_name="t", succeeded=True, final_report=rep)
        diags = [diag("negative_jacobian")] if outcome == "mesh_fail" \
            else [diag("missing_material")]
        return LoopResult(job_name="t", succeeded=False,
                          final_report=report_with(diags))

    orig_build, orig_run = authoring.build_deck_from_spec, authoring.autocorrect_run
    authoring.build_deck_from_spec, authoring.autocorrect_run = fake_build, fake_run
    try:
        res = authoring.build_and_run_spec(example_parametric_spec(), job_name="t")
    finally:
        authoring.build_deck_from_spec = orig_build
        authoring.autocorrect_run = orig_run
    return res, seeds


def test_outer_loop_remeshes_then_succeeds():
    res, seeds = _fake_pipeline(["mesh_fail", "ok"])
    assert res["succeeded"], res
    assert len(seeds) == 2 and seeds[1] < seeds[0], seeds
    assert len(res["remeshes"]) == 1, res["remeshes"]
    assert res["remeshes"][0]["trigger"] == "negative_jacobian", res["remeshes"]
    print("OK outer-loop remesh -> success, seeds:", seeds)


def test_outer_loop_does_not_remesh_non_mesh_failure():
    # A missing material is not fixed by a finer mesh; rebuilding would just
    # burn a CAE token and a solve to fail identically.
    res, seeds = _fake_pipeline(["other_fail"])
    assert not res["succeeded"]
    assert len(seeds) == 1, "must not rebuild: %r" % seeds
    assert res["remeshes"] == [], res["remeshes"]
    print("OK non-mesh failure not remeshed")


def test_outer_loop_bounded_when_remeshing_never_helps():
    res, seeds = _fake_pipeline(["mesh_fail"])
    assert not res["succeeded"]
    assert len(seeds) <= 3, "outer loop must terminate: %r" % seeds
    assert "refinement is exhausted" in res["run"] or len(seeds) == 3, res["run"]
    # The ladder is a fraction of the ORIGINAL seed. Feeding the patched spec
    # back into refine_mesh compounds the factors and the element count blows
    # up (2.5 -> 1.25 -> 0.375 is ~296x the elements, not ~37x).
    original = float(example_parametric_spec()["mesh"]["size"])
    expected = [original, original * 0.5, original * 0.3]
    assert seeds == [round(s, 6) for s in expected], (seeds, expected)
    print("OK bounded remeshing, no compounding, seeds:", seeds)


def test_build_failure_triggers_refinement():
    res, seeds = _fake_pipeline(["ok"], build_ok=False)
    assert not res["succeeded"]
    # A CAE meshing failure is retried with a finer seed rather than bailing.
    assert len(seeds) > 1, "build failure should refine and retry: %r" % seeds
    print("OK build-failure refines, seeds:", seeds)


if __name__ == "__main__":
    test_recognises_mesh_failures()
    test_distortion_warning_on_failed_job_counts()
    test_convergence_failure_is_not_a_mesh_failure()
    test_success_is_never_a_mesh_failure()
    test_missing_report_is_safe()
    test_refine_ladder_is_monotonic_and_bounded()
    test_refine_reports_element_growth()
    test_refine_declines_without_a_usable_size()
    test_outer_loop_remeshes_then_succeeds()
    test_outer_loop_does_not_remesh_non_mesh_failure()
    test_outer_loop_bounded_when_remeshing_never_helps()
    test_build_failure_triggers_refinement()
    print("\nAll mesh-repair tests passed.")
