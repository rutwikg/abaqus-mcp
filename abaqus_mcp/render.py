"""Render a picture of a job's results.

Until now this agent could describe results but never show them: every tool
returned text. A contour plot is often the fastest way for a person to see that
a model is wrong -- a load on the wrong face or a mesh that never bonded is
obvious in a picture and invisible in a peak-stress number.

Runs under ``abaqus viewer``. Be aware that this is not necessarily free:
on a licence server with no separate viewer seat, `abaqus viewer` still checks
out a ``cae`` token. Verified here -- the first render took a "cae" licence.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, Optional

from .config import CONFIG, AbaqusConfig
from .runner import SolverNotFound

_CAPTURE = Path(__file__).resolve().parent / "scripts_py27" / "capture_odb.py"

VIEWS = ("iso", "front", "back", "left", "right", "top", "bottom")


def render_odb(
    job_name: str,
    variable: str = "S",
    invariant: str = "MISES",
    view: str = "iso",
    frame: int = -1,
    step: Optional[str] = None,
    deformation_scale: float = 1.0,
    width: int = 1600,
    height: int = 1200,
    out_name: Optional[str] = None,
    cfg: AbaqusConfig = CONFIG,
    timeout_s: int = 600,
) -> Dict[str, Any]:
    """Write a PNG of ``job_name``'s results. Returns a result dict."""
    job_dir = cfg.runs_dir / job_name
    odb = job_dir / (job_name + ".odb")
    if not odb.is_file():
        return {"ok": False, "error": "no .odb for job %r at %s" % (job_name, odb)}
    if not cfg.available():
        raise SolverNotFound(
            "Abaqus command '%s' not found. Set ABAQUS_AGENT_COMMAND." % cfg.command)

    if view.lower() not in VIEWS:
        return {"ok": False,
                "error": "view must be one of %s" % ", ".join(VIEWS)}

    # printToFile silently writes nothing when the filename contains a dot, so
    # the base name is kept dot-free and the extension added afterwards.
    base = out_name or ("%s_%s_%s" % (job_name, variable.lower(), view.lower()))
    base = base.replace(".", "_")

    args = {"odb": odb.name, "out": base, "variable": variable,
            "invariant": invariant, "view": view.lower(), "frame": frame,
            "deformation_scale": deformation_scale,
            "width": width, "height": height}
    if step:
        args["step"] = step
    (job_dir / "capture_args.json").write_text(json.dumps(args, indent=2))
    shutil.copyfile(_CAPTURE, job_dir / "capture_odb.py")
    res_file = job_dir / "capture_result.json"
    if res_file.exists():
        res_file.unlink()

    # Bare script name with cwd set to the job dir: a path containing a space
    # does not survive the Abaqus command line.
    cmd = ["cmd", "/c", cfg.command, "viewer", "noGUI=capture_odb.py"]
    proc = subprocess.run(cmd, cwd=str(job_dir), capture_output=True,
                          text=True, timeout=timeout_s)
    log = "\n".join(((proc.stdout or "") + (proc.stderr or "")).splitlines()[-30:])

    if not res_file.exists():
        return {"ok": False, "error": "viewer produced no result file", "log": log}
    res = json.loads(res_file.read_text())
    if res.get("status") != "ok":
        return {"ok": False, "error": res.get("message", "capture failed"),
                "traceback": res.get("traceback", ""), "log": log}
    if not res.get("exists") or not res.get("bytes"):
        return {"ok": False,
                "error": "viewer reported success but wrote no image "
                         "(printToFile writes nothing if the name has a dot)",
                "log": log}
    res["ok"] = True
    return res


def format_render(res: Dict[str, Any]) -> str:
    if not res.get("ok"):
        return "Render failed: %s" % res.get("error", "unknown")
    lines = [
        "Wrote %s (%.0f kB)" % (res["png"], res.get("bytes", 0) / 1024.0),
        "step '%s', frame %s of %s" % (res.get("step"), res.get("frame"),
                                       res.get("frames_available")),
    ]
    if res.get("variable_plotted"):
        lines.append("plotted: %s" % res["variable_plotted"])
    if res.get("fields_available"):
        lines.append("fields in this frame: %s"
                     % ", ".join(res["fields_available"]))
    return "\n".join(lines)
