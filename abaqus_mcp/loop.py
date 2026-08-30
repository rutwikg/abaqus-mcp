"""The autonomous run/diagnose/fix/retry loop.

Stage a deck, run it, and if it fails, let the fix rules patch the deck and
resubmit -- up to ``max_iters`` times. Every iteration's deck and report are
kept so the whole self-correction history is auditable.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Union

from .config import CONFIG, AbaqusConfig
from .fixes import FixAction, FixRule, choose_and_apply, diagnose
from .inp import Deck
from .report import JobReport
from .runner import run_job, stage_deck


@dataclass
class Attempt:
    iteration: int
    status: str
    report: JobReport
    fix: Optional[FixAction] = None
    deck_snapshot: Optional[str] = None  # path to this iteration's deck


@dataclass
class LoopResult:
    job_name: str
    succeeded: bool
    attempts: List[Attempt] = field(default_factory=list)
    final_report: Optional[JobReport] = None
    working_deck: Optional[str] = None
    stopped_reason: str = ""

    def narrative(self) -> str:
        lines = ["Autonomous run of '%s': %s in %d attempt(s)." % (
            self.job_name,
            "SUCCEEDED" if self.succeeded else "FAILED",
            len(self.attempts),
        )]
        for a in self.attempts:
            lines.append("")
            lines.append("--- Attempt %d: %s ---" % (a.iteration, a.status.upper()))
            if a.report is not None:
                # A compact view of the diagnostics that drove the decision.
                errs = a.report.errors
                if errs:
                    lines.append("  errors: " + "; ".join(
                        "[%s] %s" % (d.category, d.text[:80]) for d in errs[:4]))
                cats = [c for c in a.report.categories]
                if cats:
                    lines.append("  categories: " + ", ".join(cats))
            if a.fix is not None:
                lines.append("  FIX -> %s" % a.fix.description)
        if not self.succeeded:
            lines.append("")
            lines.append("Stopped: %s" % self.stopped_reason)
        return "\n".join(lines)


def autocorrect_run(
    inp_path: Union[str, Path],
    job_name: Optional[str] = None,
    max_iters: int = 5,
    cpus: Optional[int] = None,
    timeout_s: Optional[int] = None,
    rules: Optional[List[FixRule]] = None,
    cfg: AbaqusConfig = CONFIG,
) -> LoopResult:
    src = Path(inp_path)
    job_name = job_name or src.stem
    staged = stage_deck(src, job_name, cfg)
    job_dir = staged.parent

    result = LoopResult(job_name=job_name, succeeded=False, working_deck=str(staged))
    attempt_counts: Dict[str, int] = {}

    for i in range(max_iters):
        # Snapshot the deck actually being run this iteration.
        snap = job_dir / ("%s.iter%d.inp" % (job_name, i))
        shutil.copyfile(staged, snap)

        report = run_job(job_name, cpus=cpus, timeout_s=timeout_s, cfg=cfg)
        attempt = Attempt(
            iteration=i,
            status=report.status.value,
            report=report,
            deck_snapshot=str(snap),
        )
        result.attempts.append(attempt)
        result.final_report = report

        if report.succeeded:
            result.succeeded = True
            result.stopped_reason = "converged"
            break

        # Try to find and apply a fix for the next iteration.
        deck = Deck.load(staged)
        action = choose_and_apply(report, deck, attempt_counts, rules)
        if action is None:
            guidance = diagnose(report)
            if guidance:
                # The failure is understood, but repairing it would mean
                # inventing physics. Say what the author must supply instead.
                result.stopped_reason = (
                    "no safe automatic fix -- this needs a modelling decision:\n  "
                    + "\n  ".join(guidance)
                )
            else:
                result.stopped_reason = (
                    "no applicable fix for the remaining diagnostics "
                    "(categories: %s)" % ", ".join(report.categories or ["none"])
                )
            break
        attempt.fix = action
        deck.save(staged)
    else:
        result.stopped_reason = "reached max_iters=%d without converging" % max_iters

    return result
