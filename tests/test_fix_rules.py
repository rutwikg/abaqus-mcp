"""Solver-independent unit tests for the fix rules and .inp editor.

These verify the deck-patching logic directly against synthetic reports, so the
rules (including the singularity/stabilization fix that a live single-element
deck won't reliably trigger) are covered without touching the solver.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abaqus_mcp.fixes import DEFAULT_RULES, choose_and_apply
from abaqus_mcp.inp import Deck
from abaqus_mcp.parsers import Diagnostic, JobStatus, MsgReport, StaReport
from abaqus_mcp.parsers.dat import DatReport
from abaqus_mcp.report import JobReport

BASE_DECK = """\
*HEADING
t
*ELEMENT, TYPE=C3D8, ELSET=CUBE
1, 1,2,3,4,5,6,7,8
*NSET, NSET=FIXED
1
*SOLID SECTION, ELSET=CUBE, MATERIAL=%s
*MATERIAL, NAME=STEEL
*ELASTIC
210000., 0.3
*STEP, NLGEOM=NO
*STATIC
1.0, 1.0
*BOUNDARY
%s, 1, 1
*END STEP
"""


def make_report(deck, status, err_diags):
    msg = MsgReport(diagnostics=list(err_diags), num_error_messages=len(err_diags))
    return JobReport(
        job_name="t",
        job_dir=Path("."),
        status=status,
        sta=StaReport(status=status),
        msg=msg,
        dat=DatReport(),
    )


def err(category):
    return Diagnostic(kind="error", category=category, text=category, source="msg")


def test_deck_name_repair():
    deck = Deck.parse(BASE_DECK % ("STEELX", "FIXEDD"))  # both mistyped
    report = make_report(deck, JobStatus.ABORTED, [err("undefined_set")])
    action = choose_and_apply(report, deck, {}, DEFAULT_RULES)
    txt = deck.render()
    assert action is not None and action.rule == "deck_name_repair", action
    assert "STEELX" not in txt and "MATERIAL=STEEL" in txt, txt
    assert "FIXEDD" not in txt, txt
    print("OK deck_name_repair:", action.description)


def test_singularity_stabilization():
    deck = Deck.parse(BASE_DECK % ("STEEL", "FIXED"))
    report = make_report(deck, JobStatus.ABORTED, [err("numerical_singularity")])
    action = choose_and_apply(report, deck, {}, DEFAULT_RULES)
    txt = deck.render()
    assert action is not None and action.rule == "rigid_body_stabilization", action
    assert "STABILIZE" in txt.upper(), txt
    print("OK singularity:", action.description)


def test_convergence_refinement():
    deck = Deck.parse(BASE_DECK % ("STEEL", "FIXED"))
    report = make_report(deck, JobStatus.ABORTED, [err("min_time_increment")])
    action = choose_and_apply(report, deck, {}, DEFAULT_RULES)
    txt = deck.render()
    assert action is not None and action.rule == "convergence_refinement", action
    assert "INC=100000" in txt, txt          # increment cap raised
    assert "1.0, 1.0\n" not in txt           # original controls changed
    print("OK convergence:", action.description)


def test_warning_does_not_trigger():
    # A numerical_singularity that is only a WARNING (job completed) -> no fix.
    deck = Deck.parse(BASE_DECK % ("STEEL", "FIXED"))
    warn = Diagnostic(kind="warning", category="numerical_singularity", text="w")
    report = JobReport(
        job_name="t", job_dir=Path("."), status=JobStatus.COMPLETED,
        sta=StaReport(status=JobStatus.COMPLETED),
        msg=MsgReport(diagnostics=[warn]), dat=DatReport(),
    )
    action = choose_and_apply(report, deck, {}, DEFAULT_RULES)
    assert action is None, "must not fix a successful job"
    print("OK warning-does-not-trigger")


if __name__ == "__main__":
    test_deck_name_repair()
    test_singularity_stabilization()
    test_convergence_refinement()
    test_warning_does_not_trigger()
    print("\nAll fix-rule unit tests passed.")
