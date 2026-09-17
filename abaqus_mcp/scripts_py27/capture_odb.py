# -*- coding: utf-8 -*-
# Py2.7 -- runs under `abaqus viewer noGUI`. Opens a job's .odb, plots a field
# on the deformed shape and writes a PNG.
#
# Run under `abaqus viewer`. NOTE: on this licence server that still checks out
# a "cae" token -- viewer is not always separately licensed, so do not assume
# rendering is free. It is cheaper than CAE only where a viewer seat exists.
#
# Arguments arrive in capture_args.json in the working directory, because
# `abaqus viewer` mangles `--` style argv the same way `abaqus cae` does.
from __future__ import with_statement

import json
import os
import traceback

from abaqus import *
from abaqusConstants import *
import visualization

RESULT = "capture_result.json"
ARGS = "capture_args.json"

VIEWS = {
    "iso": "Iso", "front": "Front", "back": "Back",
    "left": "Left", "right": "Right", "top": "Top", "bottom": "Bottom",
}


def write(status, message, **extra):
    d = {"status": status, "message": message}
    d.update(extra)
    with open(RESULT, "w") as f:
        json.dump(d, f, indent=2)


def main():
    with open(ARGS) as f:
        args = json.load(f)

    odb_path = str(args["odb"])
    out_png = str(args.get("out", "capture"))
    variable = str(args.get("variable", "S"))
    invariant = str(args.get("invariant", "MISES"))
    view = str(args.get("view", "iso")).lower()
    frame_idx = args.get("frame", -1)
    step_name = args.get("step")
    scale = float(args.get("deformation_scale", 1.0))
    width = int(args.get("width", 1600))
    height = int(args.get("height", 1200))

    # printToFile writes NOTHING, silently, if the filename contains a dot.
    # Strip any extension the caller supplied.
    base = os.path.splitext(out_png)[0]

    odb = session.openOdb(name=odb_path, readOnly=True)
    vp = session.viewports[session.currentViewportName]
    vp.setValues(displayedObject=odb)
    # No maximize() and no annotation fiddling: under noGUI those touch GUI
    # state that does not exist and the kernel dies with an access violation
    # rather than a Python error.
    vp.setValues(width=200, height=150)

    steps = odb.steps.keys()
    if not steps:
        raise RuntimeError("odb has no steps")
    sname = step_name if step_name in steps else steps[-1]
    frames = odb.steps[sname].frames
    if len(frames) == 0:
        raise RuntimeError("step '%s' has no frames" % sname)
    fi = int(frame_idx)
    if fi < 0:
        fi = len(frames) + fi
    fi = max(0, min(fi, len(frames) - 1))
    vp.odbDisplay.setFrame(step=steps.index(sname), frame=fi)

    plotted = None
    fo = frames[fi].fieldOutputs
    if variable.upper() in ("NONE", ""):
        vp.odbDisplay.display.setValues(plotState=(DEFORMED,))
    else:
        vp.odbDisplay.display.setValues(plotState=(CONTOURS_ON_DEF,))

        def show(name):
            """Plot `name`, supplying the invariant its type requires.

            A tensor needs 'Mises' and a vector needs 'Magnitude' -- and Abaqus
            wants that exact capitalisation. Asking for a tensor with no
            invariant fails with "requires specification of an invariant or a
            component", so the type has to drive the call.
            """
            f = fo[name]
            tname = str(f.type)
            pos = NODAL if name in ("U", "V", "A", "RF") else INTEGRATION_POINT
            if "TENSOR" in tname:
                vp.odbDisplay.setPrimaryVariable(
                    variableLabel=name, outputPosition=pos,
                    refinement=(INVARIANT, "Mises"))
            elif "VECTOR" in tname:
                vp.odbDisplay.setPrimaryVariable(
                    variableLabel=name, outputPosition=pos,
                    refinement=(INVARIANT, "Magnitude"))
            else:
                vp.odbDisplay.setPrimaryVariable(variableLabel=name,
                                                 outputPosition=pos)
            return name

        if variable in fo:
            plotted = show(variable)
        else:
            # Fall back to whatever IS in this frame rather than failing.
            for cand in ("S", "PEEQ", "U", "SDEG", "STATUS"):
                if cand in fo:
                    plotted = show(cand)
                    break

    vp.odbDisplay.commonOptions.setValues(deformationScaling=UNIFORM,
                                          uniformScaleFactor=scale)
    # setViewpoint raises TypeError; assigning a stored view is the way.
    vp.view.setValues(session.views[VIEWS.get(view, "Iso")])
    vp.view.fitView()

    session.printOptions.setValues(vpDecorations=OFF, reduceColors=False)
    session.pngOptions.setValues(imageSize=(width, height))
    session.printToFile(fileName=base, format=PNG,
                        canvasObjects=(vp,))
    odb.close()

    png = base + ".png"
    write("ok", "captured", png=os.path.abspath(png),
          exists=os.path.exists(png),
          bytes=(os.path.getsize(png) if os.path.exists(png) else 0),
          step=sname, frame=fi, frames_available=len(frames),
          variable_plotted=plotted, fields_available=sorted(fo.keys()))


try:
    main()
except Exception as exc:
    write("error", str(exc), traceback=traceback.format_exc())
