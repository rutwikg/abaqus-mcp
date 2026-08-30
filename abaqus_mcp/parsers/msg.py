"""Parse the Abaqus/Standard ``.msg`` message file.

The .msg file carries per-increment convergence diagnostics and the terminal
ANALYSIS SUMMARY. Multi-line ``***WARNING``/``***ERROR`` blocks are collapsed to
single diagnostics and tagged with a coarse ``category`` the fix rules key on.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Union

from . import Diagnostic, MsgReport

# Ordered (category, compiled-pattern) pairs. First match wins, so put the
# most specific categories first.
_CATEGORY_PATTERNS = [
    ("numerical_singularity", re.compile(r"NUMERICAL SINGULARITY", re.I)),
    ("zero_pivot", re.compile(r"ZERO PIVOT", re.I)),
    ("negative_eigenvalue", re.compile(r"NEGATIVE EIGENVALUE", re.I)),
    ("excessive_distortion", re.compile(r"DISTORT(S|ION)\s+(EXCESSIVELY|EXCESSIVE)|EXCESSIVE DISTORTION", re.I)),
    ("min_time_increment", re.compile(r"TIME INCREMENT REQUIRED IS LESS THAN THE MINIMUM", re.I)),
    ("too_many_attempts", re.compile(r"TOO MANY ATTEMPTS", re.I)),
    ("plasticity", re.compile(r"PLASTICITY|EXCESSIVE PLASTIC", re.I)),
    ("overconstraint", re.compile(r"OVERCONSTRAINT|OVER-?CONSTRAINED", re.I)),
    ("contact", re.compile(r"CONTACT", re.I)),
    ("convergence", re.compile(r"CONVERGENCE|DIVERG", re.I)),
    ("negative_jacobian", re.compile(r"NEGATIVE (JACOBIAN|VOLUME)", re.I)),
]

_NODE_RE = re.compile(r"NODE\s+(\d+)", re.I)
_ELEM_RE = re.compile(r"ELEMENT\s+(\d+)", re.I)
_DOF_RE = re.compile(r"D\.?O\.?F\.?\s*(\d+)", re.I)

_MARKER_RE = re.compile(r"\*\*\*(WARNING|ERROR)\b", re.I)
_ERR_COUNT_RE = re.compile(r"(\d+)\s+ERROR MESSAGES", re.I)
_WARN_COUNT_RE = re.compile(r"(\d+)\s+WARNING MESSAGES DURING ANALYSIS", re.I)
_CUTBACK_RE = re.compile(r"(\d+)\s+CUTBACKS? IN AUTOMATIC INCREMENTATION", re.I)


def _categorize(text: str) -> str:
    for category, pat in _CATEGORY_PATTERNS:
        if pat.search(text):
            return category
    return "other"


def _extract_int(pat: re.Pattern, text: str):
    m = pat.search(text)
    return int(m.group(1)) if m else None


def _collect_blocks(lines: List[str]) -> List[tuple]:
    """Group each ***WARNING/***ERROR with its indented continuation lines."""
    blocks = []
    current_kind = None
    current_lines: List[str] = []
    for line in lines:
        m = _MARKER_RE.search(line)
        if m:
            if current_kind is not None:
                blocks.append((current_kind, current_lines))
            current_kind = m.group(1).lower()
            current_lines = [line.strip()]
        elif current_kind is not None:
            # Continuation lines are indented and non-empty; a blank line or a
            # left-justified line ends the block.
            if line.strip() and (line.startswith(" ") and not line.strip().startswith("***")):
                # Heuristic: continuations are deeply indented wraps of the msg.
                if len(line) - len(line.lstrip()) >= 6:
                    current_lines.append(line.strip())
                    continue
            blocks.append((current_kind, current_lines))
            current_kind = None
            current_lines = []
    if current_kind is not None:
        blocks.append((current_kind, current_lines))
    return blocks


def parse_msg_text(text: str) -> MsgReport:
    lines = text.splitlines()
    report = MsgReport(raw=text)

    report.completed = "THE ANALYSIS HAS BEEN COMPLETED" in text.upper()
    report.num_error_messages = _extract_int(_ERR_COUNT_RE, text) or 0
    report.num_warning_messages = _extract_int(_WARN_COUNT_RE, text) or 0
    report.num_cutbacks = _extract_int(_CUTBACK_RE, text) or 0

    for kind, block_lines in _collect_blocks(lines):
        joined = " ".join(block_lines)
        # Strip the leading marker for cleaner text.
        clean = _MARKER_RE.sub("", joined, count=1).lstrip(": ").strip()
        report.diagnostics.append(
            Diagnostic(
                kind=kind,
                category=_categorize(joined),
                text=clean,
                node=_extract_int(_NODE_RE, joined),
                element=_extract_int(_ELEM_RE, joined),
                dof=_extract_int(_DOF_RE, joined),
                source="msg",
            )
        )
    return report


def parse_msg(path: Union[str, Path]) -> MsgReport:
    p = Path(path)
    if not p.is_file():
        return MsgReport()
    return parse_msg_text(p.read_text(errors="replace"))
