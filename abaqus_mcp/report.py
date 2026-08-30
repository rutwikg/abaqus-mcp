"""Combined job report: one structured view over .sta + .msg + .dat + stdout.

This is the object the agent/fix-engine reasons over after every run. It fuses
the three parsers into a single status verdict plus a flat, categorized list of
diagnostics, and can render an LLM-friendly summary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .parsers import Diagnostic, JobStatus
from .parsers.dat import DatReport, parse_dat
from .parsers.msg import MsgReport, parse_msg
from .parsers.sta import StaReport, parse_sta


@dataclass
class JobReport:
    job_name: str
    job_dir: Path
    status: JobStatus
    sta: StaReport
    msg: MsgReport
    dat: DatReport
    returncode: Optional[int] = None
    wallclock_s: Optional[float] = None
    stdout_tail: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == JobStatus.COMPLETED

    @property
    def diagnostics(self) -> List[Diagnostic]:
        """All errors/warnings across files, input-deck errors first."""
        return list(self.dat.diagnostics) + list(self.msg.diagnostics)

    @property
    def errors(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.kind == "error"]

    @property
    def categories(self) -> List[str]:
        """Distinct diagnostic categories present (for rule matching)."""
        seen = []
        for d in self.diagnostics:
            if d.category not in seen:
                seen.append(d.category)
        return seen

    @property
    def error_categories(self) -> List[str]:
        """Distinct categories among *error*-level diagnostics only.

        Fix rules should key on these -- warnings (e.g. an incidental negative
        eigenvalue) routinely appear in healthy runs and must not trigger fixes.
        """
        seen = []
        for d in self.errors:
            if d.category not in seen:
                seen.append(d.category)
        return seen

    def summary(self, max_diag: int = 12) -> str:
        lines = [
            "Job: %s" % self.job_name,
            "Status: %s" % self.status.value.upper(),
        ]
        if self.sta.last_increment:
            li = self.sta.last_increment
            lines.append(
                "Progress: step %d, increment %d, total time %.4g"
                % (li.step, li.inc, li.total_time)
            )
        if self.msg.num_cutbacks:
            lines.append("Cutbacks: %d" % self.msg.num_cutbacks)
        errs = self.errors
        warns = [d for d in self.diagnostics if d.kind == "warning"]
        lines.append("Diagnostics: %d error(s), %d warning(s)" % (len(errs), len(warns)))
        shown = (errs + warns)[:max_diag]
        for d in shown:
            loc = ""
            if d.node is not None:
                loc += " node=%d" % d.node
            if d.element is not None:
                loc += " elem=%d" % d.element
            if d.dof is not None:
                loc += " dof=%d" % d.dof
            lines.append("  - [%s/%s]%s %s" % (d.kind, d.category, loc, d.text[:120]))
        return "\n".join(lines)


def _resolve_status(
    sta: StaReport, msg: MsgReport, dat: DatReport, returncode: Optional[int]
) -> JobStatus:
    # A successful .sta is authoritative.
    if sta.status == JobStatus.COMPLETED:
        return JobStatus.COMPLETED
    # Input-deck errors mean the solver never really ran.
    if dat.errors:
        return JobStatus.ABORTED
    if sta.status == JobStatus.ABORTED:
        return JobStatus.ABORTED
    # Solver/analysis errors reported in .msg also mean the job aborted, even if
    # the .sta was never written (e.g. failure before the first increment).
    if msg.errors or msg.num_error_messages > 0:
        return JobStatus.ABORTED
    if sta.status == JobStatus.RUNNING:
        # Process finished but .sta has no terminal line -> treat as aborted
        # if the process returned nonzero, else it may still be running.
        if returncode is not None and returncode != 0:
            return JobStatus.ABORTED
        return JobStatus.RUNNING
    if returncode is not None and returncode != 0:
        return JobStatus.ABORTED
    return sta.status


def build_report(
    job_name: str,
    job_dir: Path,
    returncode: Optional[int] = None,
    wallclock_s: Optional[float] = None,
    stdout_tail: str = "",
) -> JobReport:
    job_dir = Path(job_dir)
    sta = parse_sta(job_dir / (job_name + ".sta"))
    msg = parse_msg(job_dir / (job_name + ".msg"))
    dat = parse_dat(job_dir / (job_name + ".dat"))
    status = _resolve_status(sta, msg, dat, returncode)
    return JobReport(
        job_name=job_name,
        job_dir=job_dir,
        status=status,
        sta=sta,
        msg=msg,
        dat=dat,
        returncode=returncode,
        wallclock_s=wallclock_s,
        stdout_tail=stdout_tail,
    )
