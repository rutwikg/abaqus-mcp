"""Drive the Abaqus solver: stage a deck, run it headless, collect a report.

Windows note: the launcher is ``abaqus.bat``. CreateProcess cannot execute a
.bat directly, so we always invoke through ``cmd /c``. We run with
``interactive`` so the subprocess lifetime equals the analysis lifetime, which
makes completion detection trivial (process exit) while we still read the
progress files afterwards.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Union

from .config import CONFIG, AbaqusConfig
from .report import JobReport, build_report

# Files Abaqus leaves behind that must be cleared before re-running a job name.
_STALE_ARTIFACTS = (".lck", ".023", ".mdl", ".stt", ".prt", ".sim", ".odb_f")


class SolverNotFound(RuntimeError):
    pass


def stage_deck(inp_path: Union[str, Path], job_name: str, cfg: AbaqusConfig = CONFIG) -> Path:
    """Copy an .inp into a clean per-job run directory. Returns the staged path."""
    src = Path(inp_path)
    if not src.is_file():
        raise FileNotFoundError("Input deck not found: %s" % src)
    job_dir = cfg.job_dir(job_name)
    dst = job_dir / (job_name + ".inp")
    # The deck may already be the staged file (e.g. exported here by the CAE
    # builder); copying it onto itself would raise SameFileError.
    if src.resolve() != dst.resolve():
        shutil.copyfile(src, dst)
    return dst


def _clear_stale(job_dir: Path, job_name: str) -> None:
    """Remove lock/intermediate files from a previous run of this job name."""
    lck = job_dir / (job_name + ".lck")
    if lck.exists():
        try:
            lck.unlink()
        except OSError:
            pass
    for ext in _STALE_ARTIFACTS:
        f = job_dir / (job_name + ext)
        if f.exists():
            try:
                f.unlink()
            except OSError:
                pass


def _build_command(cfg: AbaqusConfig, job_name: str, cpus: int) -> List[str]:
    args = [
        cfg.command,
        "job=%s" % job_name,
        "interactive",
        "ask_delete=OFF",
    ]
    if cpus and cpus > 1:
        args.append("cpus=%d" % cpus)
    # Route through cmd so the .bat launcher executes.
    return ["cmd", "/c"] + args


def run_job(
    job_name: str,
    cpus: Optional[int] = None,
    timeout_s: Optional[int] = None,
    cfg: AbaqusConfig = CONFIG,
) -> JobReport:
    """Run an already-staged job to completion and return its report.

    Assumes ``<job_name>.inp`` exists in ``cfg.job_dir(job_name)`` (use
    ``stage_deck`` first). Blocks until the solver exits or the timeout fires.
    """
    if not cfg.available():
        raise SolverNotFound(
            "Abaqus command '%s' not found. Set ABAQUS_AGENT_COMMAND." % cfg.command
        )
    job_dir = cfg.job_dir(job_name)
    if not (job_dir / (job_name + ".inp")).is_file():
        raise FileNotFoundError(
            "No staged deck %s.inp in %s (call stage_deck first)." % (job_name, job_dir)
        )

    _clear_stale(job_dir, job_name)
    cpus = cpus or cfg.default_cpus
    timeout_s = timeout_s or cfg.job_timeout_s
    cmd = _build_command(cfg, job_name, cpus)

    start = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(job_dir),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        returncode = proc.returncode
        stdout = (proc.stdout or "") + (proc.stderr or "")
    except subprocess.TimeoutExpired as e:
        timed_out = True
        returncode = None
        stdout = (e.stdout or "") + "\n[runner] TIMEOUT after %ss\n" % timeout_s
        # Best-effort: kill any lingering solver by removing the lock so a
        # follow-up run can proceed.
        _clear_stale(job_dir, job_name)
    wallclock = time.time() - start

    stdout_tail = "\n".join(stdout.splitlines()[-40:])
    report = build_report(
        job_name=job_name,
        job_dir=job_dir,
        returncode=returncode,
        wallclock_s=wallclock,
        stdout_tail=stdout_tail,
    )
    if timed_out:
        # Surface the timeout in the report status downstream via stdout tail;
        # status stays whatever the partial .sta indicated (usually RUNNING).
        pass
    return report


def run_deck(
    inp_path: Union[str, Path],
    job_name: Optional[str] = None,
    cpus: Optional[int] = None,
    timeout_s: Optional[int] = None,
    cfg: AbaqusConfig = CONFIG,
) -> JobReport:
    """Convenience: stage an arbitrary .inp then run it."""
    src = Path(inp_path)
    job_name = job_name or src.stem
    stage_deck(src, job_name, cfg)
    return run_job(job_name, cpus=cpus, timeout_s=timeout_s, cfg=cfg)
