"""The structured *simulation spec* -- the deterministic core of model authoring.

An LLM (or a human) fills in this JSON-serialisable spec; the Py2.7 CAE builder
(`scripts_py27/build_from_spec.py`) turns it into a meshed model and exports a
flat .inp, which the self-correcting loop then runs. Keeping the spec explicit
and validated means the unreliable step (free-form authoring) is bounded by a
schema, while the reliable step (geometry/mesh/deck generation) is code.

Face selectors
--------------
Loads and BCs attach to faces chosen by a selector, evaluated against the part's
overall bounding box so the same spec works regardless of exact CAD coordinates:
    {"select": "xmin"|"xmax"|"ymin"|"ymax"|"zmin"|"zmax"}
    {"box": [xmin, ymin, zmin, xmax, ymax, zmax]}   # explicit region
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

AXIS_SELECTORS = {"xmin", "xmax", "ymin", "ymax", "zmin", "zmax"}
STEP_TYPES = {"static"}
LOAD_TYPES = {"pressure", "traction"}
GEOMETRY_TYPES = {"step", "iges", "parametric"}

# Parametric shapes and their required params. All are axis-aligned so the
# xmin..zmax face selectors line up with intuitive faces.
PARAM_SHAPES = {
    "block": ["lx", "ly", "lz"],          # rectangular block (also 'beam')
    "beam": ["lx", "ly", "lz"],
    "plate": ["lx", "ly", "thickness"],   # thin plate, thickness along z
    "cylinder": ["radius", "height"],     # axis along z
    "notched_bar": ["length", "width", "thickness", "notch_radius"],
    "l_bracket": ["arm1", "arm2", "width", "thickness"],
}


def _err(errors: List[str], cond: bool, msg: str) -> None:
    if not cond:
        errors.append(msg)


def _positive(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def _validate_face(errors: List[str], where: str, face: Any) -> None:
    if not isinstance(face, dict):
        errors.append("%s: face must be an object" % where)
        return
    if "select" in face:
        _err(errors, face["select"] in AXIS_SELECTORS,
             "%s: face.select must be one of %s" % (where, sorted(AXIS_SELECTORS)))
    elif "box" in face:
        _err(errors, isinstance(face["box"], list) and len(face["box"]) == 6,
             "%s: face.box must be [xmin,ymin,zmin,xmax,ymax,zmax]" % where)
    else:
        errors.append("%s: face needs 'select' or 'box'" % where)


def validate_spec(spec: Dict[str, Any]) -> List[str]:
    """Return a list of human-readable problems; empty list means valid."""
    errors: List[str] = []
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]

    geom = spec.get("geometry")
    _err(errors, isinstance(geom, dict), "geometry is required")
    if isinstance(geom, dict):
        gtype = geom.get("type")
        _err(errors, gtype in GEOMETRY_TYPES,
             "geometry.type must be one of %s" % sorted(GEOMETRY_TYPES))
        if gtype == "parametric":
            shape = geom.get("shape")
            _err(errors, shape in PARAM_SHAPES,
                 "geometry.shape must be one of %s" % sorted(PARAM_SHAPES))
            params = geom.get("params", {})
            if shape in PARAM_SHAPES:
                for req in PARAM_SHAPES[shape]:
                    _err(errors, isinstance(params, dict) and req in params
                         and _positive(params[req]),
                         "geometry.params.%s must be a positive number for shape '%s'"
                         % (req, shape))
        elif gtype in ("step", "iges"):
            _err(errors, bool(geom.get("file")), "geometry.file (path) is required")

    mats = spec.get("materials")
    _err(errors, isinstance(mats, list) and len(mats) >= 1,
         "at least one material is required")
    mat_names = set()
    if isinstance(mats, list):
        for i, mat in enumerate(mats):
            if not isinstance(mat, dict):
                errors.append("materials[%d] must be an object" % i)
                continue
            _err(errors, bool(mat.get("name")), "materials[%d].name required" % i)
            mat_names.add(mat.get("name"))
            el = mat.get("elastic")
            _err(errors, isinstance(el, dict) and "E" in el and "nu" in el,
                 "materials[%d].elastic needs E and nu" % i)

    section = spec.get("section", {})
    if section:
        _err(errors, section.get("material") in mat_names,
             "section.material must name a defined material")

    steps = spec.get("steps")
    _err(errors, isinstance(steps, list) and len(steps) >= 1,
         "at least one step is required")
    if isinstance(steps, list):
        for i, st in enumerate(steps):
            _err(errors, st.get("type", "static") in STEP_TYPES,
                 "steps[%d].type unsupported (only 'static' so far)" % i)

    for i, bc in enumerate(spec.get("bcs", []) or []):
        _validate_face(errors, "bcs[%d]" % i, bc.get("face"))
        _err(errors, isinstance(bc.get("dof"), list) and bc.get("dof"),
             "bcs[%d].dof must be a non-empty list, e.g. [1,2,3]" % i)

    for i, ld in enumerate(spec.get("loads", []) or []):
        _validate_face(errors, "loads[%d]" % i, ld.get("face"))
        _err(errors, ld.get("type") in LOAD_TYPES,
             "loads[%d].type must be one of %s" % (i, sorted(LOAD_TYPES)))
    return errors


def example_spec(step_file: str = "bracket.step") -> Dict[str, Any]:
    """A minimal, valid spec for the L-bracket fixture (fix zmin, pull zmax)."""
    return {
        "model_name": "bracket",
        "geometry": {"type": "step", "file": step_file},
        "mesh": {"size": 8.0, "element_type": "C3D10"},
        "materials": [
            {"name": "steel",
             "elastic": {"E": 210000.0, "nu": 0.3},
             "density": 7.85e-9,
             "plastic": [[250.0, 0.0], [350.0, 0.2]]}
        ],
        "section": {"material": "steel"},
        "steps": [
            {"name": "load", "type": "static", "nlgeom": False,
             "initial_inc": 0.2, "period": 1.0, "min_inc": 1e-5, "max_inc": 1.0}
        ],
        "bcs": [
            {"name": "fixed", "face": {"select": "zmin"}, "dof": [1, 2, 3]}
        ],
        "loads": [
            {"name": "pull", "type": "pressure",
             "face": {"select": "zmax"}, "magnitude": 50.0, "step": "load"}
        ],
    }


def example_parametric_spec() -> Dict[str, Any]:
    """A valid spec for a parametric notched bar in tension (no CAD file)."""
    return {
        "model_name": "notched_bar",
        "geometry": {
            "type": "parametric", "shape": "notched_bar",
            "params": {"length": 100.0, "width": 20.0, "thickness": 5.0,
                       "notch_radius": 4.0},
        },
        "mesh": {"size": 2.5, "element_type": "C3D10"},
        "materials": [
            {"name": "steel", "elastic": {"E": 210000.0, "nu": 0.3},
             "plastic": [[250.0, 0.0], [350.0, 0.2]]}
        ],
        "section": {"material": "steel"},
        "steps": [
            {"name": "pull", "type": "static", "nlgeom": False,
             "initial_inc": 0.1, "period": 1.0, "min_inc": 1e-5, "max_inc": 1.0}
        ],
        "bcs": [
            {"name": "fix", "face": {"select": "xmin"}, "dof": [1, 2, 3]}
        ],
        "loads": [
            {"name": "tension", "type": "pressure",
             "face": {"select": "xmax"}, "magnitude": -200.0, "step": "pull"}
        ],
    }


def dumps(spec: Dict[str, Any]) -> str:
    return json.dumps(spec, indent=2)
