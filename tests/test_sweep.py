"""Solver-independent tests for the unattended sweep + parked-failure queue.

The sweep's central promise is that nothing disappears: every design ends as
valid, parked, or abandoned, each with a reason. These tests script the solver
outcomes and check that promise holds -- including when the runner itself
throws.
"""
import csv
import json
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abaqus_mcp import sweep as sw
from abaqus_mcp.config import AbaqusConfig
from abaqus_mcp.loop import Attempt, LoopResult
from abaqus_mcp.fixes import FixAction
from abaqus_mcp.parsers import JobStatus, MsgReport, StaReport
from abaqus_mcp.parsers.dat import DatReport
from abaqus_mcp.report import JobReport


def make_cfg(tmp):
    return AbaqusConfig(command="abaqus", runs_dir=Path(tmp))


def a_report(status=JobStatus.COMPLETED):
    return JobReport(job_name="t", job_dir=Path("."), status=status,
                     sta=StaReport(status=status), msg=MsgReport(), dat=DatReport())


def loop_result(succeeded, fix_desc=None, reason=""):
    fix = FixAction(rule="r", description=fix_desc) if fix_desc else None
    status = JobStatus.COMPLETED if succeeded else JobStatus.ABORTED
    return LoopResult(
        job_name="t", succeeded=succeeded,
        attempts=[Attempt(0, status.value, a_report(status), fix=fix)],
        final_report=a_report(status), stopped_reason=reason)


def patch(monkey):
    """Install scripted autocorrect_run / extract_results / assess."""
    sw.autocorrect_run = monkey["loop"]
    sw.extract_results = monkey.get("results", lambda job, cfg=None: {"steps": []})
    if "assess" in monkey:
        sw.assess = monkey["assess"]


class Verdict:
    def __init__(self, valid, failures=(), checks=(), remedy=None):
        self.valid = valid
        self.failures = list(failures)
        self.checks = list(checks)
        self.metrics = {}
        self.remedy = remedy


def run(tmp, designs, **kw):
    cfg = make_cfg(tmp)
    return sw.run_sweep("s", designs, cfg=cfg, **kw), cfg


def test_valid_design_is_not_parked():
    tmp = tempfile.mkdtemp()
    try:
        patch({"loop": lambda *a, **k: loop_result(True),
               "assess": lambda res, **k: Verdict(True, checks=["energy OK"])})
        state, cfg = run(tmp, [sw.Design("d1", {"t": 1.5}, inp="x.inp")])
        assert state["d1"].status == sw.STATUS_VALID, state["d1"]
        assert sw.list_parked("s", cfg) == []
        print("OK valid design not parked")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_failed_design_is_parked_with_diagnostics():
    tmp = tempfile.mkdtemp()
    try:
        patch({"loop": lambda *a, **k: loop_result(False, reason="no applicable fix")})
        state, cfg = run(tmp, [sw.Design("d2", {"t": 1.0}, inp="x.inp")])
        assert state["d2"].status == sw.STATUS_PARKED
        parked = sw.list_parked("s", cfg)
        assert [p["design_id"] for p in parked] == ["d2"], parked
        b = sw.get_parked("s", "d2", cfg)
        assert b["params"] == {"t": 1.0}
        assert "no applicable fix" in b["parked_because"]
        print("OK failed design parked:", b["parked_because"][:50])
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_converged_but_rejected_is_parked():
    # The hero case: the solver says COMPLETED and the sweep still parks it.
    tmp = tempfile.mkdtemp()
    try:
        patch({"loop": lambda *a, **k: loop_result(True),
               "assess": lambda res, **k: Verdict(
                   False, failures=["artificial_energy"],
                   checks=["ARTIFICIAL ENERGY 99.9% of internal energy"],
                   remedy="Refine the mesh.")})
        state, cfg = run(tmp, [sw.Design("d3", {"t": 2.0}, inp="x.inp")])
        assert state["d3"].status == sw.STATUS_PARKED, state["d3"]
        assert "artificial_energy" in state["d3"].reason
        b = sw.get_parked("s", "d3", cfg)
        assert b["succeeded"] is True, "the solver did succeed -- that is the point"
        assert b["validity"]["valid"] is False
        assert "Refine" in b["validity"]["remedy"]
        print("OK converged-but-rejected parked while succeeded=True")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_unbuildable_design_is_abandoned_not_skipped():
    tmp = tempfile.mkdtemp()
    try:
        patch({"loop": lambda *a, **k: loop_result(True),
               "assess": lambda res, **k: Verdict(True)})
        d = sw.Design("d4", {"t": 9.9})
        d.inp = None
        state, cfg = run(tmp, [d], build_fn=lambda design: None)
        assert state["d4"].status == sw.STATUS_ABANDONED
        assert state["d4"].reason, "abandonment must carry a reason"
        print("OK unbuildable abandoned with reason:", state["d4"].reason)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_runner_crash_leaves_a_record_not_a_gap():
    tmp = tempfile.mkdtemp()
    try:
        def boom(*a, **k):
            raise RuntimeError("solver host exploded")
        patch({"loop": boom})
        state, cfg = run(tmp, [sw.Design("d5", {"t": 1.1}, inp="x.inp")])
        assert state["d5"].status == sw.STATUS_PARKED
        assert "exploded" in state["d5"].reason
        b = sw.get_parked("s", "d5", cfg)
        assert "traceback" in b, "a crash must preserve the traceback"
        print("OK crash recorded, not silently dropped")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_resume_skips_terminal_and_retries_parked():
    tmp = tempfile.mkdtemp()
    try:
        calls = []

        def once(inp_path, job_name=None, **k):
            calls.append(job_name)
            return loop_result(job_name == "ok1")
        patch({"loop": once, "assess": lambda res, **k: Verdict(True)})
        designs = [sw.Design("ok1"), sw.Design("bad1")]
        for d in designs:
            d.inp = "x.inp"
        state, cfg = run(tmp, designs)
        assert state["ok1"].status == sw.STATUS_VALID
        assert state["bad1"].status == sw.STATUS_PARKED
        first = list(calls)

        # Re-run: the valid one must not be recomputed, the parked one must be.
        calls.clear()
        sw.run_sweep("s", designs, cfg=cfg)
        assert "ok1" not in calls, "terminal design must not re-run"
        assert "bad1" in calls, "parked design must be retried"
        print("OK resume: first pass %s, second pass %s" % (first, calls))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_recovered_design_is_unparked():
    tmp = tempfile.mkdtemp()
    try:
        outcome = {"ok": False}

        def flaky(inp_path, job_name=None, **k):
            return loop_result(outcome["ok"])
        patch({"loop": flaky, "assess": lambda res, **k: Verdict(True)})
        d = sw.Design("d6"); d.inp = "x.inp"
        state, cfg = run(tmp, [d])
        assert sw.get_parked("s", "d6", cfg) is not None

        outcome["ok"] = True
        sw.run_sweep("s", [d], cfg=cfg)
        assert sw.get_parked("s", "d6", cfg) is None, "stale parked file must go"
        assert sw.list_parked("s", cfg) == []
        print("OK recovered design unparked")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_results_table_includes_failures():
    # A table of only the successes is the sanitised record this prevents.
    tmp = tempfile.mkdtemp()
    try:
        def mixed(inp_path, job_name=None, **k):
            return loop_result(job_name != "bad")
        patch({"loop": mixed, "assess": lambda res, **k: Verdict(True)})
        designs = [sw.Design("good", {"t": 1.0}), sw.Design("bad", {"t": 2.0})]
        for d in designs:
            d.inp = "x.inp"
        state, cfg = run(tmp, designs)
        p = sw.sweep_dir("s", cfg) / "results.csv"
        rows = list(csv.DictReader(p.open(encoding="utf-8")))
        by_id = {r["design_id"]: r for r in rows}
        assert set(by_id) == {"good", "bad"}, by_id
        assert by_id["bad"]["status"] == sw.STATUS_PARKED
        assert by_id["bad"]["reason"], "a parked row must say why"
        assert by_id["good"]["t"] == "1.0", by_id["good"]
        print("OK results.csv carries parked rows too")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _park_a_design(tmp, deck_text):
    """Park one design whose deck is on disk, ready for a reasoned fix."""
    cfg = make_cfg(tmp)
    job = Path(tmp) / "d7"
    job.mkdir(parents=True, exist_ok=True)
    deck = job / "d7.inp"
    deck.write_text(deck_text, encoding="utf-8")
    patch({"loop": lambda *a, **k: loop_result(False, reason="stuck")})
    sw.run_sweep("s", [sw.Design("d7", {"t": 1.0}, inp=str(deck))], cfg=cfg)
    return cfg, deck


DECK = """*HEADING
demo
*STEP
*DYNAMIC, EXPLICIT
, 2.0e-3
*BOUNDARY
FIXED, ENCASTRE
*OUTPUT, FIELD
*NODE OUTPUT
U
*OUTPUT, HISTORY
*ENERGY OUTPUT
ALLIE, ALLAE
*END STEP
"""


def test_reasoned_fix_applies_and_unparks_on_success():
    tmp = tempfile.mkdtemp()
    try:
        cfg, deck = _park_a_design(tmp, DECK)
        patch({"loop": lambda *a, **k: loop_result(True),
               "assess": lambda res, **k: Verdict(True, checks=["OK"])})
        entry = sw.apply_reasoned_fix(
            "s", "d7",
            [{"find": ", 2.0e-3", "replace": ", 4.0e-3"}],
            "Step time was too short for the fold to develop.", cfg=cfg)
        assert entry["outcome"] == sw.STATUS_VALID, entry
        assert sw.get_parked("s", "d7", cfg) is None
        assert ", 4.0e-3" in deck.read_text(encoding="utf-8")
        assert deck.with_suffix(".prefix.inp").is_file(), "pre-fix snapshot missing"
        log = sw.fix_log("s", cfg)
        assert len(log) == 1 and log[0]["rationale"].startswith("Step time")
        print("OK reasoned fix applied, unparked, logged")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reasoned_fix_refuses_to_delete_output():
    # The failure mode worth guarding: making a run 'pass' by deleting the
    # output it would be judged on.
    tmp = tempfile.mkdtemp()
    try:
        cfg, deck = _park_a_design(tmp, DECK)
        try:
            sw.apply_reasoned_fix(
                "s", "d7",
                [{"find": "*ENERGY OUTPUT", "replace": ""}],
                "removing energy output", cfg=cfg)
        except sw.RefusedEdit as exc:
            assert "ENERGY OUTPUT" in str(exc), exc
            assert "*ENERGY OUTPUT" in deck.read_text(encoding="utf-8"), "deck was modified anyway"
            print("OK refused to delete energy output:", str(exc)[:60])
            return
        raise AssertionError("deleting *ENERGY OUTPUT must be refused")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_reasoned_fix_rejects_a_find_that_is_absent():
    tmp = tempfile.mkdtemp()
    try:
        cfg, deck = _park_a_design(tmp, DECK)
        try:
            sw.apply_reasoned_fix("s", "d7",
                                  [{"find": "*NOT IN THE DECK", "replace": "x"}],
                                  "hallucinated edit", cfg=cfg)
        except ValueError as exc:
            assert "does not appear" in str(exc)
            print("OK absent find rejected")
            return
        raise AssertionError("an edit that does not match must be rejected")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_failed_reasoned_fix_is_still_logged():
    # The fix log is the product. An attempt that did not work belongs in it.
    tmp = tempfile.mkdtemp()
    try:
        cfg, deck = _park_a_design(tmp, DECK)
        patch({"loop": lambda *a, **k: loop_result(False, reason="still stuck")})
        entry = sw.apply_reasoned_fix("s", "d7",
                                      [{"find": ", 2.0e-3", "replace": ", 3.0e-3"}],
                                      "try a longer step", cfg=cfg)
        assert entry["outcome"] == sw.STATUS_PARKED
        assert sw.get_parked("s", "d7", cfg) is not None, "must stay parked"
        assert len(sw.fix_log("s", cfg)) == 1
        print("OK unsuccessful fix still recorded")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    test_valid_design_is_not_parked()
    test_failed_design_is_parked_with_diagnostics()
    test_converged_but_rejected_is_parked()
    test_unbuildable_design_is_abandoned_not_skipped()
    test_runner_crash_leaves_a_record_not_a_gap()
    test_resume_skips_terminal_and_retries_parked()
    test_recovered_design_is_unparked()
    test_results_table_includes_failures()
    test_reasoned_fix_applies_and_unparks_on_success()
    test_reasoned_fix_refuses_to_delete_output()
    test_reasoned_fix_rejects_a_find_that_is_absent()
    test_failed_reasoned_fix_is_still_logged()
    print("\nAll sweep tests passed.")
