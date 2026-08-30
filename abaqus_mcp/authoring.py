"""Model authoring: turn a validated simulation spec into a runnable .inp.

Invokes the Py2.7 CAE builder (`scripts_py27/build_from_spec.py`) under
`abaqus cae noGUI`, then optionally hands the exported deck to the autonomous
run/fix loop. The CAE step is the only place we touch the Abaqus kernel Python;
it communicates back purely through files (build_result.json + the .inp).
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import CONFIG, AbaqusConfig
from .loop import LoopResult, autocorrect_run
from .runner import SolverNotFound
from .spec import validate_spec

_BUILDER = Path(__file__).resolve().parent / "scripts_py27" / "build_from_spec.py"


def build_deck_from_spec(
    spec: Dict[str, Any],
    job_name: Optional[str] = None,
    cfg: AbaqusConfig = CONFIG,
    timeout_s: int = 1800,
) -> Dict[str, Any]:
    """Build and export an .inp from a spec. Returns a result dict:

    {ok: bool, inp: <path or None>, errors: [...], stats: {...}, log: <tail>}
    """
    errors = validate_spec(spec)
    if errors:
        return {"ok": False, "inp": None, "errors": errors, "stats": {}, "log": ""}

    if not cfg.available():
        raise SolverNotFound(
            "Abaqus command '%s' not found. Set ABAQUS_AGENT_COMMAND." % cfg.command
        )

    job_name = job_name or spec.get("model_name", "model")
    work = cfg.job_dir(job_name)

    # For CAD geometry, resolve the file path to absolute so CAE finds it from
    # the job cwd. Parametric geometry has no file.
    spec = json.loads(json.dumps(spec))  # deep copy
    if spec["geometry"].get("type") in ("step", "iges"):
        gfile = Path(spec["geometry"]["file"])
        if not gfile.is_absolute():
            gfile = (Path.cwd() / gfile).resolve()
        if not gfile.is_file():
            return {"ok": False, "inp": None,
                    "errors": ["geometry.file not found: %s" % gfile],
                    "stats": {}, "log": ""}
        spec["geometry"]["file"] = str(gfile)

    # Stage spec + builder into the job dir; invoke by bare name so the space
    # in the project path never reaches the Abaqus command line.
    (work / "spec.json").write_text(json.dumps(spec, indent=2))
    (work / "build_args.json").write_text(json.dumps({"job_name": job_name}))
    shutil.copyfile(_BUILDER, work / "build_from_spec.py")
    result_file = work / "build_result.json"
    if result_file.exists():
        result_file.unlink()

    cmd = ["cmd", "/c", cfg.command, "cae", "noGUI=build_from_spec.py"]
    start = time.time()
    proc = subprocess.run(cmd, cwd=str(work), capture_output=True, text=True,
                          timeout=timeout_s)
    elapsed = time.time() - start
    log_tail = "\n".join(((proc.stdout or "") + (proc.stderr or "")).splitlines()[-40:])

    if not result_file.exists():
        return {"ok": False, "inp": None,
                "errors": ["CAE builder produced no result file (see log)"],
                "stats": {"elapsed_s": elapsed}, "log": log_tail}

    res = json.loads(result_file.read_text())
    if res.get("status") != "ok":
        return {"ok": False, "inp": None,
                "errors": [res.get("message", "CAE build failed")],
                "traceback": res.get("traceback", ""),
                "stats": {"elapsed_s": elapsed}, "log": log_tail}

    inp_path = work / res["inp"]
    stats = {k: res[k] for k in ("elements", "element_type", "mesh_size",
                                 "bbox_low", "bbox_high") if k in res}
    stats["elapsed_s"] = round(elapsed, 1)
    return {"ok": True, "inp": str(inp_path), "errors": [], "stats": stats,
            "log": log_tail}


def build_and_run_spec(
    spec: Dict[str, Any],
    job_name: Optional[str] = None,
    max_iters: int = 5,
    cpus: int = 1,
    cfg: AbaqusConfig = CONFIG,
) -> Dict[str, Any]:
    """Full pipeline: build the deck from a spec, then autonomously run+fix it."""
    build = build_deck_from_spec(spec, job_name=job_name, cfg=cfg)
    if not build["ok"]:
        return {"phase": "build", "build": build, "run": None,
                "succeeded": False}
    result: LoopResult = autocorrect_run(
        build["inp"], job_name=job_name or spec.get("model_name", "model"),
        max_iters=max_iters, cpus=cpus, cfg=cfg,
    )
    return {
        "phase": "run",
        "build": build,
        "run": result.narrative(),
        "succeeded": result.succeeded,
    }
