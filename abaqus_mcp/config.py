"""Environment configuration: locate the Abaqus command and manage run dirs.

Everything the rest of the engine needs to know about *this machine* is resolved
here, so the other modules stay platform-agnostic. Defaults are discovered but
every value can be overridden with an ABAQUS_AGENT_* environment variable.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

# Known install roots to probe when the command is not already on PATH.
# Newest first so we prefer the most recent release if several are present.
_WINDOWS_COMMAND_CANDIDATES = [
    r"C:\SIMULIA\Commands\abaqus.bat",
    r"C:\SIMULIA\Commands\abq2024.bat",
    r"C:\SIMULIA\Commands\abq2023.bat",
    r"C:\SIMULIA\Commands\abq2022.bat",
]


def _discover_command() -> str:
    """Return the Abaqus launcher command (name or absolute path)."""
    override = os.environ.get("ABAQUS_AGENT_COMMAND")
    if override:
        return override
    for name in ("abaqus", "abq2024", "abq2023", "abq2022"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in _WINDOWS_COMMAND_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    # Fall back to the bare name; the caller will get a clear error on first use.
    return "abaqus"


def _default_runs_dir() -> Path:
    override = os.environ.get("ABAQUS_AGENT_RUNS_DIR")
    if override:
        return Path(override)
    # A ``runs`` directory beside wherever the client launched us. Deliberately
    # NOT relative to this file: once installed as a wheel (uvx/pipx) that path
    # points inside site-packages, or into uv's ephemeral cache, and job output
    # would vanish between runs. Set ABAQUS_AGENT_RUNS_DIR to pin it.
    return Path.cwd() / "runs"


@dataclass(frozen=True)
class AbaqusConfig:
    """Resolved settings for driving Abaqus on this machine."""

    command: str = field(default_factory=_discover_command)
    runs_dir: Path = field(default_factory=_default_runs_dir)
    # Solver parallelism defaults; overridable per-job at submit time.
    default_cpus: int = int(os.environ.get("ABAQUS_AGENT_CPUS", "6"))
    # Hard wall-clock ceiling for a single solver invocation (seconds).
    job_timeout_s: int = int(os.environ.get("ABAQUS_AGENT_JOB_TIMEOUT", "3600"))

    def job_dir(self, job_name: str) -> Path:
        """Directory that holds all files for ``job_name`` (created on demand)."""
        d = self.runs_dir / job_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def available(self) -> bool:
        """True if the resolved command actually exists / is on PATH."""
        if os.path.sep in self.command or (os.altsep and os.altsep in self.command):
            return Path(self.command).is_file()
        return shutil.which(self.command) is not None


# Module-level singleton for convenience; callers may construct their own.
CONFIG = AbaqusConfig()
