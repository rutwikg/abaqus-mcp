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


def test_negative_eigenvalue_damping():
    deck = Deck.parse(BASE_DECK % ("STEEL", "FIXED"))
    report = make_report(deck, JobStatus.ABORTED, [err("negative_eigenvalue")])
    action = choose_and_apply(report, deck, {}, DEFAULT_RULES)
    txt = deck.render().upper()
    assert action is not None and action.rule == "instability_damping", action
    assert "STABILIZE" in txt, txt
    print("OK instability_damping:", action.description)


def test_negative_eigenvalue_escalates():
    # Repeated firing must reach for progressively heavier damping.
    deck = Deck.parse(BASE_DECK % ("STEEL", "FIXED"))
    report = make_report(deck, JobStatus.ABORTED, [err("negative_eigenvalue")])
    counts = {}
    factors = []
    for _ in range(3):
        action = choose_and_apply(report, deck, counts, DEFAULT_RULES)
        factors.append(float(action.details["new_stabilize"]))
    assert factors == sorted(factors) and factors[0] < factors[-1], factors
    print("OK instability escalation:", factors)


def test_negative_eigenvalue_warning_ignored():
    # A healthy buckling run emits negative-eigenvalue WARNINGS every step.
    # Damping those would silently corrupt a converged result.
    deck = Deck.parse(BASE_DECK % ("STEEL", "FIXED"))
    warn = Diagnostic(kind="warning", category="negative_eigenvalue", text="w")
    report = JobReport(
        job_name="t", job_dir=Path("."), status=JobStatus.COMPLETED,
        sta=StaReport(status=JobStatus.COMPLETED),
        msg=MsgReport(diagnostics=[warn]), dat=DatReport(),
    )
    action = choose_and_apply(report, deck, {}, DEFAULT_RULES)
    assert action is None, "must not damp a completed run: %r" % (action,)
    print("OK negative-eigenvalue-warning-ignored")


def test_negative_eigenvalue_warning_on_failed_job_fires():
    # The real-solver case: Abaqus reports the eigenvalues as WARNINGS and the
    # error as a downstream convergence failure. Damping must still win over
    # generic increment refinement, since it is the targeted remedy.
    deck = Deck.parse(BASE_DECK % ("STEEL", "FIXED"))
    diags = [
        Diagnostic(kind="warning", category="negative_eigenvalue", text="w"),
        Diagnostic(kind="error", category="too_many_attempts", text="e"),
    ]
    report = make_report(deck, JobStatus.ABORTED, diags)
    action = choose_and_apply(report, deck, {}, DEFAULT_RULES)
    assert action is not None and action.rule == "instability_damping", action
    print("OK negative-eigenvalue-warning-on-failure:", action.rule)


def test_damping_fix_carries_a_fidelity_caveat():
    # Damping can converge onto the unstable branch, so a "success" produced
    # this way must not be reported as an unqualified one.
    deck = Deck.parse(BASE_DECK % ("STEEL", "FIXED"))
    report = make_report(deck, JobStatus.ABORTED, [err("negative_eigenvalue")])
    action = choose_and_apply(report, deck, {}, DEFAULT_RULES)
    assert action.caveat, "damping must declare its fidelity cost"
    assert "RIKS" in action.caveat, action.caveat
    # Deck-consistency repairs change no physics and must stay caveat-free.
    deck2 = Deck.parse(BASE_DECK % ("STEELX", "FIXED"))
    r2 = make_report(deck2, JobStatus.ABORTED, [err("undefined_set")])
    a2 = choose_and_apply(r2, deck2, {}, DEFAULT_RULES)
    assert a2.caveat is None, "a spelling repair needs no caveat"
    print("OK damping-caveat present, name-repair caveat-free")


def test_overconstraint_guidance_names_locations():
    deck = Deck.parse(BASE_DECK % ("STEEL", "FIXED"))
    diags = [
        Diagnostic(kind="error", category="overconstraint",
                   text="overconstraint", node=17, dof=2, source="msg"),
        Diagnostic(kind="error", category="overconstraint",
                   text="overconstraint", node=42, dof=1, source="msg"),
    ]
    report = make_report(deck, JobStatus.ABORTED, diags)
    action = choose_and_apply(report, deck, {}, DEFAULT_RULES)
    assert action is None, "overconstraint must not be auto-resolved: %r" % (action,)
    guidance = diagnose(report)
    assert guidance, "expected guidance"
    assert "node 17 (DOF 2)" in guidance[0], guidance[0]
    assert "node 42 (DOF 1)" in guidance[0], guidance[0]
    print("OK overconstraint-guidance:", guidance[0][-60:])


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
    test_negative_eigenvalue_damping()
    test_negative_eigenvalue_escalates()
    test_negative_eigenvalue_warning_ignored()
    test_negative_eigenvalue_warning_on_failed_job_fires()
    test_damping_fix_carries_a_fidelity_caveat()
    test_overconstraint_guidance_names_locations()
    print("\nAll fix-rule unit tests passed.")
