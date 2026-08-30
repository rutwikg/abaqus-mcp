"""Solver-independent unit tests for the fix rules and .inp editor.

These verify the deck-patching logic directly against synthetic reports, so the
rules (including the singularity/stabilization fix that a live single-element
deck won't reliably trigger) are covered without touching the solver.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abaqus_mcp.fixes import DEFAULT_RULES, choose_and_apply, diagnose
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


def test_unknown_keyword_repair():
    # *SOLD SECTION is a typo for *SOLID SECTION.
    deck = Deck.parse(BASE_DECK.replace("*SOLID SECTION", "*SOLD SECTION")
                      % ("STEEL", "FIXED"))
    report = make_report(deck, JobStatus.ABORTED, [err("unknown_keyword")])
    action = choose_and_apply(report, deck, {}, DEFAULT_RULES)
    txt = deck.render()
    assert action is not None and action.rule == "unknown_keyword_repair", action
    assert "*SOLID SECTION" in txt and "*SOLD SECTION" not in txt, txt
    # The parameters must survive the keyword rewrite untouched.
    assert "ELSET=CUBE" in txt and "MATERIAL=STEEL" in txt, txt
    print("OK unknown_keyword_repair:", action.description)


def test_unfamiliar_keyword_is_left_alone():
    # A keyword we don't know, but that isn't a near-miss of one we do, must
    # NOT be rewritten -- guessing here would corrupt a valid deck.
    deck = Deck.parse(BASE_DECK.replace("*HEADING", "*SUBSTRUCTURE GENERATE")
                      % ("STEEL", "FIXED"))
    report = make_report(deck, JobStatus.ABORTED, [err("unknown_keyword")])
    action = choose_and_apply(report, deck, {}, DEFAULT_RULES)
    assert action is None or action.rule != "unknown_keyword_repair", action
    assert "*SUBSTRUCTURE GENERATE" in deck.render()
    print("OK unfamiliar-keyword-left-alone")


def test_duplicate_definition_removed():
    dup = BASE_DECK % ("STEEL", "FIXED")
    dup = dup.replace("*NSET, NSET=FIXED\n1\n", "*NSET, NSET=FIXED\n1\n" * 2)
    deck = Deck.parse(dup)
    assert sum(1 for b in deck.blocks if b.keyword == "NSET") == 2
    report = make_report(deck, JobStatus.ABORTED, [err("duplicate")])
    action = choose_and_apply(report, deck, {}, DEFAULT_RULES)
    assert action is not None and action.rule == "duplicate_definition", action
    assert sum(1 for b in deck.blocks if b.keyword == "NSET") == 1
    print("OK duplicate_definition:", action.description)


def test_conflicting_definition_is_not_removed():
    # Same name, DIFFERENT content -> a real conflict, not a redundant copy.
    conflict = BASE_DECK % ("STEEL", "FIXED")
    conflict = conflict.replace("*NSET, NSET=FIXED\n1\n",
                                "*NSET, NSET=FIXED\n1\n*NSET, NSET=FIXED\n2\n")
    deck = Deck.parse(conflict)
    report = make_report(deck, JobStatus.ABORTED, [err("duplicate")])
    action = choose_and_apply(report, deck, {}, DEFAULT_RULES)
    assert action is None or action.rule != "duplicate_definition", action
    assert sum(1 for b in deck.blocks if b.keyword == "NSET") == 2
    print("OK conflicting-definition-kept")


def test_unfixable_failure_gives_guidance():
    # missing_material has no safe auto-fix: guessing a modulus would produce a
    # deck that converges to a meaningless answer.
    deck = Deck.parse(BASE_DECK % ("STEEL", "FIXED"))
    report = make_report(deck, JobStatus.ABORTED, [err("missing_section")])
    action = choose_and_apply(report, deck, {}, DEFAULT_RULES)
    assert action is None, "must not invent a section: %r" % (action,)
    guidance = diagnose(report)
    assert guidance and "missing_section" in guidance[0], guidance
    print("OK unfixable-gives-guidance:", guidance[0][:60] + "...")


if __name__ == "__main__":
    test_deck_name_repair()
    test_singularity_stabilization()
    test_convergence_refinement()
    test_warning_does_not_trigger()
    test_unknown_keyword_repair()
    test_unfamiliar_keyword_is_left_alone()
    test_duplicate_definition_removed()
    test_conflicting_definition_is_not_removed()
    test_unfixable_failure_gives_guidance()
    print("\nAll fix-rule unit tests passed.")
