"""Parsers for Abaqus text output files (.sta, .msg, .dat, .log).

These turn the solver's human-readable output into structured objects the
agent can reason over. Shared data types live here; the file-specific logic
lives in the sibling modules.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class JobStatus(str, Enum):
    """High-level outcome of a job, derived primarily from the .sta file."""

    NOT_STARTED = "not_started"       # no .sta yet
    RUNNING = "running"               # .sta exists, no terminal line yet
    COMPLETED = "completed"           # "COMPLETED SUCCESSFULLY"
    ABORTED = "aborted"               # "HAS NOT BEEN COMPLETED" / errors
    UNKNOWN = "unknown"               # file present but unrecognised terminal state


@dataclass
class Increment:
    """One row of the .sta increment table."""

    step: int
    inc: int
    attempt: int
    severe_discon_iters: int
    equil_iters: int
    total_iters: int
    total_time: float
    step_time: float
    inc_of_time: float
    raw: str = ""


@dataclass
class Diagnostic:
    """A single WARNING/ERROR extracted from .msg or .dat.

    ``kind`` is 'warning' or 'error'. ``category`` is a coarse machine label
    (e.g. 'numerical_singularity', 'negative_eigenvalue', 'excessive_distortion')
    used by the fix rules; ``text`` is the full original message.
    """

    kind: str
    category: str
    text: str
    node: Optional[int] = None
    element: Optional[int] = None
    dof: Optional[int] = None
    source: str = ""  # which file it came from


@dataclass
class StaReport:
    status: JobStatus
    increments: List[Increment] = field(default_factory=list)
    final_line: str = ""
    raw: str = ""

    @property
    def last_increment(self) -> Optional[Increment]:
        return self.increments[-1] if self.increments else None


@dataclass
class MsgReport:
    completed: bool = False
    num_error_messages: int = 0
    num_warning_messages: int = 0
    num_cutbacks: int = 0
    diagnostics: List[Diagnostic] = field(default_factory=list)
    raw: str = ""

    @property
    def errors(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.kind == "error"]

    @property
    def warnings(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.kind == "warning"]


@dataclass
class DatReport:
    diagnostics: List[Diagnostic] = field(default_factory=list)
    raw: str = ""

    @property
    def errors(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.kind == "error"]

    @property
    def warnings(self) -> List[Diagnostic]:
        return [d for d in self.diagnostics if d.kind == "warning"]
