# -*- coding: utf-8 -*-
# Py2.7 -- runs inside `abaqus cae noGUI`. Reads a simulation spec (JSON),
# imports the CAD geometry, meshes it, applies materials / BCs / loads, and
# writes a flat .inp deck. All outcomes (success or a full traceback) are
# written to build_result.json so the Py3 caller can react.
#
# Inputs are read from files in the current working directory (spec.json and
# build_args.json), NOT from argv -- Abaqus mangles script command-line args,
# so file-passing is the reliable channel.
from __future__ import with_statement
import json
import traceback

RESULT = "build_result.json"
SPEC_FILE = "spec.json"
ARGS_FILE = "build_args.json"


def write_result(status, message, **extra):
    data = {"status": status, "message": message}
    data.update(extra)
    with open(RESULT, "w") as f:
        json.dump(data, f, indent=2)


def to_str(obj):
    """Recursively convert unicode -> bytes str; Abaqus' Py2.7 API rejects
    unicode strings that json.load produces."""
    if isinstance(obj, unicode):  # noqa: F821 (Py2)
        return obj.encode("utf-8")
    if isinstance(obj, dict):
        return dict((to_str(k), to_str(v)) for k, v in obj.items())
    if isinstance(obj, list):
        return [to_str(v) for v in obj]
    return obj


def read_job_name():
    try:
        with open(ARGS_FILE) as f:
            return to_str(json.load(f).get("job_name", "model"))
    except Exception:
        return "model"


def overall_bbox(part):
    """Rough (lo, hi) bbox from vertices -- used only for a pre-mesh seed-size
    estimate. Degenerate for vertex-less shapes (e.g. cylinders); callers that
    need an authoritative box use node_bbox after meshing."""
    xs, ys, zs = [], [], []
    for v in part.vertices:
        (x, y, z) = v.pointOn[0]
        xs.append(x); ys.append(y); zs.append(z)
    if not xs:
        return (0.0, 0.0, 0.0), (0.0, 0.0, 0.0)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def node_bbox(part):
    """Authoritative (lo, hi) bbox from mesh node coordinates -- works for any
    geometry including curved faces. Requires the part to be meshed."""
    xs, ys, zs = [], [], []
    for n in part.nodes:
        (x, y, z) = n.coordinates
        xs.append(x); ys.append(y); zs.append(z)
    return (min(xs), min(ys), min(zs)), (max(xs), max(ys), max(zs))


def face_bbox_for_selector(lo, hi, face):
    """Return getByBoundingBox args (x0,y0,z0,x1,y1,z1) for a face selector."""
    (x0, y0, z0), (x1, y1, z1) = lo, hi
    ext = max(x1 - x0, y1 - y0, z1 - z0, 1.0)
    tol = max(1e-4, 1e-3 * ext)
    if "box" in face:
        b = face["box"]
        return (b[0] - tol, b[1] - tol, b[2] - tol, b[3] + tol, b[4] + tol, b[5] + tol)
    sel = face["select"]
    # Start with the full box, then squeeze the selected side into a thin slab.
    a = [x0 - tol, y0 - tol, z0 - tol, x1 + tol, y1 + tol, z1 + tol]
    if sel == "xmin": a[3] = x0 + tol
    elif sel == "xmax": a[0] = x1 - tol
    elif sel == "ymin": a[4] = y0 + tol
    elif sel == "ymax": a[1] = y1 - tol
    elif sel == "zmin": a[5] = z0 + tol
    elif sel == "zmax": a[2] = z1 - tol
    return tuple(a)


def make_part(mdb, model, spec):
    """Return a solid Part named PART, either imported from CAD or built
    parametrically from geometry.shape + geometry.params."""
    from abaqusConstants import THREE_D, DEFORMABLE_BODY, OFF
    geom = spec["geometry"]
    gtype = geom.get("type", "step")
    if gtype == "parametric":
        return make_parametric_part(model, geom["shape"],
                                    dict(geom.get("params", {})))
    gfile = geom["file"]
    if gtype == "iges":
        from abaqusConstants import DEFAULT
        geomfile = mdb.openIges(gfile, msbo=False, trimCurve=DEFAULT,
                                scaleFromFile=OFF)
    else:
        geomfile = mdb.openStep(gfile, scaleFromFile=OFF)
    return model.PartFromGeometryFile(
        name="PART", geometryFile=geomfile, combine=False,
        dimensionality=THREE_D, type=DEFORMABLE_BODY)


def make_parametric_part(model, shape, params):
    """Build an axis-aligned solid so the xmin..zmax face selectors are intuitive.
    Extrusion is along +z; the base profile lies in the xy-plane at z=0."""
    from abaqusConstants import THREE_D, DEFORMABLE_BODY, CLOCKWISE

    def f(key):
        return float(params[key])

    p = model.Part(name="PART", dimensionality=THREE_D, type=DEFORMABLE_BODY)

    if shape in ("block", "beam"):
        lx, ly, lz = f("lx"), f("ly"), f("lz")
        s = model.ConstrainedSketch(name="__s", sheetSize=2 * max(lx, ly, lz))
        s.rectangle(point1=(0.0, 0.0), point2=(lx, ly))
        p.BaseSolidExtrude(sketch=s, depth=lz)
    elif shape == "plate":
        lx, ly, t = f("lx"), f("ly"), f("thickness")
        s = model.ConstrainedSketch(name="__s", sheetSize=2 * max(lx, ly))
        s.rectangle(point1=(0.0, 0.0), point2=(lx, ly))
        p.BaseSolidExtrude(sketch=s, depth=t)
    elif shape == "cylinder":
        r, h = f("radius"), f("height")
        s = model.ConstrainedSketch(name="__s", sheetSize=4 * r)
        s.CircleByCenterPerimeter(center=(0.0, 0.0), point1=(r, 0.0))
        p.BaseSolidExtrude(sketch=s, depth=h)
    elif shape == "notched_bar":
        L, W, t, r = f("length"), f("width"), f("thickness"), f("notch_radius")
        s = model.ConstrainedSketch(name="__s", sheetSize=2 * max(L, W))
        s.Line(point1=(0.0, 0.0), point2=(L, 0.0))
        s.Line(point1=(L, 0.0), point2=(L, W))
        s.Line(point1=(L, W), point2=(L / 2.0 + r, W))
        # Semicircular notch dipping into the bar from the top edge midpoint.
        s.ArcByCenterEnds(center=(L / 2.0, W), point1=(L / 2.0 + r, W),
                          point2=(L / 2.0 - r, W), direction=CLOCKWISE)
        s.Line(point1=(L / 2.0 - r, W), point2=(0.0, W))
        s.Line(point1=(0.0, W), point2=(0.0, 0.0))
        p.BaseSolidExtrude(sketch=s, depth=t)
    elif shape == "l_bracket":
        a1, a2, w, t = f("arm1"), f("arm2"), f("width"), f("thickness")
        s = model.ConstrainedSketch(name="__s", sheetSize=2 * max(a1, a2))
        s.Line(point1=(0.0, 0.0), point2=(a1, 0.0))
        s.Line(point1=(a1, 0.0), point2=(a1, w))
        s.Line(point1=(a1, w), point2=(w, w))
        s.Line(point1=(w, w), point2=(w, a2))
        s.Line(point1=(w, a2), point2=(0.0, a2))
        s.Line(point1=(0.0, a2), point2=(0.0, 0.0))
        p.BaseSolidExtrude(sketch=s, depth=t)
    else:
        raise RuntimeError("Unknown parametric shape: %s" % shape)
    return p


def build(spec_file, job_name):
    from abaqus import mdb
    from abaqusConstants import (
        THREE_D, DEFORMABLE_BODY, OFF, ON, STANDARD, TET, FREE, GENERAL,
    )
    # Importing the core CAE modules registers the geometry loaders
    # (mdb.openStep/openIges/openAcis) and the *Step/*BC/*Load builders.
    import part          # noqa: F401
    import assembly      # noqa: F401
    import step          # noqa: F401
    import interaction   # noqa: F401
    import load          # noqa: F401
    import mesh
    import regionToolset  # noqa: F401

    with open(spec_file) as f:
        spec = to_str(json.load(f))

    model_name = spec.get("model_name", "Model-1")
    model = mdb.Model(name=model_name)

    # -- geometry (import CAD or build parametric) ----------------------
    part = make_part(mdb, model, spec)
    if len(part.cells) == 0:
        raise RuntimeError("Geometry has no solid cells (not a closed solid?)")

    # -- mesh -----------------------------------------------------------
    meshcfg = spec.get("mesh", {})
    size = float(meshcfg.get("size", 0.0))
    if size <= 0:
        (lo, hi) = overall_bbox(part)
        size = 0.08 * max(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2], 1.0)
    elem_code_name = str(meshcfg.get("element_type", "C3D10")).upper()
    elem_code = getattr(__import__("abaqusConstants"), elem_code_name, None)
    if elem_code is None:
        from abaqusConstants import C3D10
        elem_code = C3D10
        elem_code_name = "C3D10"
    part.setMeshControls(regions=part.cells, elemShape=TET, technique=FREE)
    part.setElementType(regions=(part.cells,),
                        elemTypes=(mesh.ElemType(elemCode=elem_code, elemLibrary=STANDARD),))
    part.seedPart(size=size, deviationFactor=0.1, minSizeFactor=0.1)
    part.generateMesh()
    n_el = len(part.elements)
    if n_el == 0:
        raise RuntimeError("Meshing produced 0 elements (seed size too large?)")

    # -- material + section --------------------------------------------
    for m in spec["materials"]:
        mat = model.Material(name=m["name"])
        el = m["elastic"]
        mat.Elastic(table=((float(el["E"]), float(el["nu"])),))
        if m.get("density") is not None:
            mat.Density(table=((float(m["density"]),),))
        if m.get("plastic"):
            mat.Plastic(table=tuple(tuple(float(v) for v in row) for row in m["plastic"]))
    sec_mat = spec.get("section", {}).get("material", spec["materials"][0]["name"])
    model.HomogeneousSolidSection(name="SEC", material=sec_mat, thickness=None)
    part.Set(name="ALL", cells=part.cells)
    part.SectionAssignment(region=part.sets["ALL"], sectionName="SEC")

    # -- assembly -------------------------------------------------------
    asm = model.rootAssembly
    inst = asm.Instance(name="PART-1", part=part, dependent=ON)
    (lo, hi) = node_bbox(part)  # authoritative box for face selection

    def faces_for(face_sel):
        args = face_bbox_for_selector(lo, hi, face_sel)
        f = inst.faces.getByBoundingBox(*args)
        return f

    # -- steps ----------------------------------------------------------
    step_names = []
    prev = "Initial"
    for st in spec["steps"]:
        name = st.get("name", "Step-%d" % (len(step_names) + 1))
        model.StaticStep(
            name=name, previous=prev,
            nlgeom=ON if st.get("nlgeom") else OFF,
            initialInc=float(st.get("initial_inc", 0.1)),
            timePeriod=float(st.get("period", 1.0)),
            minInc=float(st.get("min_inc", 1e-5)),
            maxInc=float(st.get("max_inc", 1.0)),
            maxNumInc=int(st.get("max_num_inc", 100000)),
        )
        step_names.append(name)
        prev = name
    first_step = step_names[0]

    # -- boundary conditions -------------------------------------------
    for i, bc in enumerate(spec.get("bcs", []) or []):
        faces = faces_for(bc["face"])
        setname = "BCSET_%d" % i
        asm.Set(name=setname, faces=faces)
        dofs = set(int(d) for d in bc["dof"])
        kw = {}
        if 1 in dofs: kw["u1"] = 0.0
        if 2 in dofs: kw["u2"] = 0.0
        if 3 in dofs: kw["u3"] = 0.0
        model.DisplacementBC(
            name=bc.get("name", "BC-%d" % i),
            createStepName=bc.get("step", first_step),
            region=asm.sets[setname], **kw
        )

    # -- loads ----------------------------------------------------------
    for i, ld in enumerate(spec.get("loads", []) or []):
        faces = faces_for(ld["face"])
        surfname = "LOADSURF_%d" % i
        asm.Surface(name=surfname, side1Faces=faces)
        surf = asm.surfaces[surfname]
        step = ld.get("step", first_step)
        if ld["type"] == "pressure":
            model.Pressure(name=ld.get("name", "LOAD-%d" % i),
                           createStepName=step, region=surf,
                           magnitude=float(ld["magnitude"]))
        elif ld["type"] == "traction":
            v = ld["vector"]
            model.SurfaceTraction(
                name=ld.get("name", "LOAD-%d" % i), createStepName=step,
                region=surf, magnitude=1.0, traction=GENERAL,
                directionVector=((0.0, 0.0, 0.0), (float(v[0]), float(v[1]), float(v[2]))),
            )

    # -- field output + export -----------------------------------------
    try:
        model.FieldOutputRequest(name="F-Output-1", createStepName=first_step,
                                 variables=("S", "E", "U", "PE", "PEEQ", "RF"))
    except Exception:
        pass
    job = mdb.Job(name=job_name, model=model_name)
    job.writeInput(consistencyChecking=OFF)

    write_result("ok", "built and exported deck",
                 inp=job_name + ".inp", elements=n_el,
                 element_type=elem_code_name, mesh_size=size,
                 bbox_low=list(lo), bbox_high=list(hi))


def main():
    job_name = read_job_name()
    try:
        build(SPEC_FILE, job_name)
    except Exception as exc:  # noqa
        write_result("error", str(exc), traceback=traceback.format_exc())


main()
