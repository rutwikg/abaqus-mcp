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
from typing import Any, Dict, List, Optional

from .config import CONFIG, AbaqusConfig
from .loop import LoopResult, autocorrect_run
from .meshfix import is_mesh_failure, mesh_failure_categories, refine_mesh
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
    max_remesh: int = 2,
) -> Dict[str, Any]:
    """Full pipeline: build the deck from a spec, then autonomously run+fix it.

    Two nested loops. The inner one (:func:`autocorrect_run`) patches the
    keyword deck. The outer one here handles failures the deck cannot express:
    a negative Jacobian or a badly distorted element is a property of the
    *mesh*, so the remedy is to refine the spec and build again rather than to
    keep editing solver controls. ``max_remesh`` bounds that outer loop -- each
    pass costs a full CAE build plus a solve.
    """
    job = job_name or spec.get("model_name", "model")
    current = spec
    remesh_history: List[Dict[str, str]] = []
    caveats: List[str] = []
    narratives: List[str] = []

    for attempt in range(max_remesh + 1):
        build = build_deck_from_spec(current, job_name=job, cfg=cfg)
        if not build["ok"]:
            # A CAE build failure is usually meshing giving up on the geometry,
            # which is exactly what refinement addresses -- so retry rather
            # than bailing on the first failure as this used to.
            refined = refine_mesh(spec, attempt)
            if refined is None:
                return {"phase": "build", "build": build, "run": None,
                        "succeeded": False, "remeshes": remesh_history,
                        "caveats": [c for c in caveats if c]}
            current, fix = refined
            remesh_history.append({"trigger": "build_failure", **fix.details})
            caveats.append(fix.caveat)
            narratives.append("Build failed; %s" % fix.description)
            continue

        result: LoopResult = autocorrect_run(
            build["inp"], job_name=job, max_iters=max_iters, cpus=cpus, cfg=cfg,
        )
        narratives.append(result.narrative())
        caveats.extend(result.caveats)

        if result.succeeded or not is_mesh_failure(result.final_report):
            return {
                "phase": "run",
                "build": build,
                "run": "\n\n".join(narratives),
                "succeeded": result.succeeded,
                "remeshes": remesh_history,
                "caveats": [c for c in caveats if c],
            }

        # Mesh-level failure: refine the spec and rebuild.
        refined = refine_mesh(spec, attempt)
        if refined is None:
            narratives.append(
                "Mesh failure persists (%s) but refinement is exhausted -- the "
                "geometry itself likely needs attention."
                % ", ".join(mesh_failure_categories(result.final_report))
            )
            return {"phase": "run", "build": build,
                    "run": "\n\n".join(narratives), "succeeded": False,
                    "remeshes": remesh_history,
                    "caveats": [c for c in caveats if c]}
        current, fix = refined
        remesh_history.append({
            "trigger": ",".join(mesh_failure_categories(result.final_report)),
            **fix.details,
        })
        caveats.append(fix.caveat)
        narratives.append("REMESH -> %s" % fix.description)

    return {"phase": "run", "build": None, "run": "\n\n".join(narratives),
            "succeeded": False, "remeshes": remesh_history,
            "caveats": [c for c in caveats if c]}
