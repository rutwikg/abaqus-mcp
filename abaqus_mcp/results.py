"""Extract headline results from a finished job's .odb.

Invokes the Py2.7 extractor under `abaqus python` (no CAE license token needed),
which reads the ODB and returns peak stress/displacement/plastic-strain and net
reaction force per step. Pure Py3 here; the extractor talks back via results.json.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from .config import CONFIG, AbaqusConfig
from .runner import SolverNotFound

_EXTRACTOR = Path(__file__).resolve().parent / "scripts_py27" / "extract_odb.py"


def extract_results(
    job_name: str, cfg: AbaqusConfig = CONFIG, timeout_s: int = 600
) -> Dict[str, Any]:
    """Return {ok, steps:[...], error?} for a job's .odb."""
    if not cfg.available():
        raise SolverNotFound("Abaqus command '%s' not found." % cfg.command)
    job_dir = cfg.job_dir(job_name)
    odb = job_dir / (job_name + ".odb")
    if not odb.is_file():
        return {"ok": False, "error": "no .odb for job '%s'" % job_name, "steps": []}

    shutil.copyfile(_EXTRACTOR, job_dir / "extract_odb.py")
    result_file = job_dir / "results.json"
    if result_file.exists():
        result_file.unlink()

    cmd = ["cmd", "/c", cfg.command, "python", "extract_odb.py", odb.name]
    proc = subprocess.run(cmd, cwd=str(job_dir), capture_output=True, text=True,
                          timeout=timeout_s)
    if not result_file.exists():
        tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).splitlines()[-20:])
        return {"ok": False, "error": "extractor produced no output", "log": tail,
                "steps": []}
    res = json.loads(result_file.read_text())
    if res.get("status") != "ok":
        return {"ok": False, "error": res.get("message", "extract failed"),
                "traceback": res.get("traceback", ""), "steps": []}
    return {"ok": True, "steps": res.get("steps", [])}


def format_results(res: Dict[str, Any]) -> str:
    """Human/LLM-friendly rendering of extracted results."""
    if not res.get("ok"):
        return "Results unavailable: %s" % res.get("error", "unknown")
    lines = []
    for st in res["steps"]:
        lines.append("Step '%s' (t=%.3g):" % (st.get("step", "?"),
                                              st.get("frame_time", 0.0)))
        if "max_von_mises" in st:
            lines.append("  max von Mises stress: %.4g  (element %s)"
                         % (st["max_von_mises"], st.get("max_von_mises_element")))
        if "max_displacement" in st:
            lines.append("  max displacement mag: %.4g  (node %s)"
                         % (st["max_displacement"], st.get("max_displacement_node")))
        if "max_equiv_plastic_strain" in st:
            lines.append("  max equiv. plastic strain (PEEQ): %.4g  -> %s"
                         % (st["max_equiv_plastic_strain"],
                            "YIELDED" if st.get("yielded") else "elastic"))
        if "net_reaction_magnitude" in st:
            rf = st.get("net_reaction_force", [])
            lines.append("  net reaction force: |R|=%.4g  R=%s"
                         % (st["net_reaction_magnitude"],
                            ", ".join("%.4g" % c for c in rf)))
    return "\n".join(lines) if lines else "No step results found in the .odb."
