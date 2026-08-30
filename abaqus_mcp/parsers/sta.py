"""Parse the Abaqus/Standard ``.sta`` status file.

The .sta file is the primary progress signal: a header, then one row per
increment attempt, then a terminal line. We read it both for live monitoring
(latest increment) and for the final verdict.

Terminal lines seen in practice:
    "THE ANALYSIS HAS COMPLETED SUCCESSFULLY"   -> COMPLETED
    "THE ANALYSIS HAS NOT BEEN COMPLETED"       -> ABORTED
Abaqus/Explicit writes a differently shaped table but the same terminal lines,
so the status detection below is solver-agnostic; row parsing is best-effort.
"""

from __future__ import annotations

from pathlib import Path
from typing import Union

from . import Increment, JobStatus, StaReport

_SUCCESS = "THE ANALYSIS HAS COMPLETED SUCCESSFULLY"
_FAILED = "THE ANALYSIS HAS NOT BEEN COMPLETED"


def _try_int(tok: str) -> int:
    try:
        return int(tok)
    except ValueError:
        return -1


def _try_float(tok: str) -> float:
    try:
        return float(tok)
    except ValueError:
        return float("nan")


def _parse_row(line: str) -> Increment | None:
    """Parse a numeric increment row; return None for headers/blank lines."""
    toks = line.split()
    # A data row starts with the step number and has enough numeric columns.
    # Standard layout: STEP INC ATT SEVERE EQUIL TOTAL TOTALTIME STEPTIME INCTIME
    if len(toks) < 9:
        return None
    if not toks[0].lstrip("-").isdigit():
        return None
    # Guard against header fragments that happen to start with a digit.
    if _try_int(toks[1]) < 0:
        return None
    return Increment(
        step=_try_int(toks[0]),
        inc=_try_int(toks[1]),
        attempt=_try_int(toks[2]),
        severe_discon_iters=_try_int(toks[3]),
        equil_iters=_try_int(toks[4]),
        total_iters=_try_int(toks[5]),
        total_time=_try_float(toks[6]),
        step_time=_try_float(toks[7]),
        inc_of_time=_try_float(toks[8]),
        raw=line.rstrip(),
    )


def parse_sta_text(text: str) -> StaReport:
    upper = text.upper()
    if _SUCCESS in upper:
        status = JobStatus.COMPLETED
    elif _FAILED in upper:
        status = JobStatus.ABORTED
    elif text.strip():
        status = JobStatus.RUNNING
    else:
        status = JobStatus.NOT_STARTED

    increments = []
    final_line = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if "THE ANALYSIS HAS" in stripped.upper():
            final_line = stripped
            continue
        inc = _parse_row(line)
        if inc is not None:
            increments.append(inc)

    return StaReport(
        status=status,
        increments=increments,
        final_line=final_line,
        raw=text,
    )


def parse_sta(path: Union[str, Path]) -> StaReport:
    p = Path(path)
    if not p.is_file():
        return StaReport(status=JobStatus.NOT_STARTED)
    return parse_sta_text(p.read_text(errors="replace"))
