"""Parse the Abaqus ``.dat`` data file for input-processing diagnostics.

The .dat file is where the *input file processor* reports problems with the deck
itself -- undefined sets, bad element definitions, missing material data, etc.
These are distinct from solver/convergence issues in .msg and are usually the
first thing to fix. ``***ERROR`` here almost always means the job never ran.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Union

from . import Diagnostic, DatReport

_MARKER_RE = re.compile(r"\*\*\*(WARNING|ERROR)\b", re.I)
_ELEM_RE = re.compile(r"ELEMENT\s+(\d+)", re.I)
_NODE_RE = re.compile(r"NODE\s+(\d+)", re.I)

# Coarse categories for input-deck problems the fix rules can act on.
_CATEGORY_PATTERNS = [
    ("undefined_set", re.compile(r"(SET|NSET|ELSET|SURFACE)\s+\S+.*(NOT.*DEFINED|HAS NOT BEEN DEFINED|IS UNKNOWN)", re.I)),
    ("missing_material", re.compile(r"MATERIAL.*(NOT.*DEFINED|MISSING|NO ELASTIC)", re.I)),
    ("missing_section", re.compile(r"(SECTION|PROPERT).*(NOT.*(DEFINED|ASSIGNED)|MISSING)", re.I)),
    ("unknown_keyword", re.compile(r"(UNKNOWN|UNRECOGNIZED|ILLEGAL).*(KEYWORD|PARAMETER|OPTION)", re.I)),
    ("element_definition", re.compile(r"ELEMENT.*(INCORRECT|INVALID|CONNECTIVITY|DEFINITION)", re.I)),
    ("duplicate", re.compile(r"DUPLICATE", re.I)),
]


def _categorize(text: str) -> str:
    for category, pat in _CATEGORY_PATTERNS:
        if pat.search(text):
            return category
    return "input_other"


def _extract_int(pat: re.Pattern, text: str):
    m = pat.search(text)
    return int(m.group(1)) if m else None


def _collect_blocks(lines: List[str]) -> List[tuple]:
    blocks = []
    kind = None
    buf: List[str] = []
    for line in lines:
        m = _MARKER_RE.search(line)
        if m:
            if kind is not None:
                blocks.append((kind, buf))
            kind = m.group(1).lower()
            buf = [line.strip()]
        elif kind is not None:
            if line.strip() and len(line) - len(line.lstrip()) >= 6 and not line.strip().startswith("***"):
                buf.append(line.strip())
            else:
                blocks.append((kind, buf))
                kind = None
                buf = []
    if kind is not None:
        blocks.append((kind, buf))
    return blocks


def parse_dat_text(text: str) -> DatReport:
    report = DatReport(raw=text)
    for kind, block_lines in _collect_blocks(text.splitlines()):
        joined = " ".join(block_lines)
        clean = _MARKER_RE.sub("", joined, count=1).lstrip(": ").strip()
        report.diagnostics.append(
            Diagnostic(
                kind=kind,
                category=_categorize(joined),
                text=clean,
                node=_extract_int(_NODE_RE, joined),
                element=_extract_int(_ELEM_RE, joined),
                source="dat",
            )
        )
    return report


def parse_dat(path: Union[str, Path]) -> DatReport:
    p = Path(path)
    if not p.is_file():
        return DatReport()
    return parse_dat_text(p.read_text(errors="replace"))
