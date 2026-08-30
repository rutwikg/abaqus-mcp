# -*- coding: utf-8 -*-
# Py2.7 -- runs under `abaqus python` (no CAE license needed). Opens a job's
# .odb and extracts headline results per step (last frame): peak von Mises
# stress, peak displacement, peak equivalent plastic strain, and the net
# reaction force. Writes results.json for the Py3 caller.
#
# Usage: abaqus python extract_odb.py <job>.odb
import json
import os
import sys


def find_odb(argv):
    for a in argv[1:]:
        if a.lower().endswith(".odb"):
            return a
    # Fall back to the only .odb in the current directory.
    odbs = [f for f in os.listdir(".") if f.lower().endswith(".odb")]
    return odbs[0] if odbs else None


def max_by(values, attr):
    """Return (max_value, label) for a field-value attribute, or (None, None)."""
    best = None
    best_label = None
    for v in values:
        try:
            x = getattr(v, attr)
        except Exception:
            continue
        if x is None:
            continue
        if best is None or x > best:
            best = x
            best_label = getattr(v, "elementLabel", None) or getattr(v, "nodeLabel", None)
    return best, best_label


def extract(odb_path):
    from odbAccess import openOdb
    odb = openOdb(odb_path, readOnly=True)
    out = {"odb": odb_path, "steps": []}
    try:
        for step_name in odb.steps.keys():
            step = odb.steps[step_name]
            if len(step.frames) == 0:
                continue
            frame = step.frames[-1]
            fo = frame.fieldOutputs
            entry = {"step": step_name,
                     "frame_time": float(getattr(frame, "frameValue", 0.0))}

            if "S" in fo:
                mises, elem = max_by(fo["S"].values, "mises")
                if mises is not None:
                    entry["max_von_mises"] = mises
                    entry["max_von_mises_element"] = elem
            if "U" in fo:
                umag, node = max_by(fo["U"].values, "magnitude")
                if umag is not None:
                    entry["max_displacement"] = umag
                    entry["max_displacement_node"] = node
            if "PE" in fo:
                # Equivalent plastic strain is often available as PEEQ; if only
                # PE tensor is present, report its max principal magnitude proxy.
                pass
            if "PEEQ" in fo:
                peeq, node = max_by(fo["PEEQ"].values, "data")
                if peeq is not None:
                    entry["max_equiv_plastic_strain"] = peeq
                    entry["yielded"] = peeq > 1e-8
            if "RF" in fo:
                rx = ry = rz = 0.0
                for v in fo["RF"].values:
                    d = v.data
                    rx += float(d[0]); ry += float(d[1])
                    if len(d) > 2:
                        rz += float(d[2])
                mag = (rx * rx + ry * ry + rz * rz) ** 0.5
                entry["net_reaction_force"] = [rx, ry, rz]
                entry["net_reaction_magnitude"] = mag
            out["steps"].append(entry)
        out["status"] = "ok"
    finally:
        odb.close()
    return out


def main():
    odb_path = find_odb(sys.argv)
    try:
        if not odb_path:
            raise RuntimeError("no .odb found")
        res = extract(odb_path)
    except Exception as exc:
        import traceback
        res = {"status": "error", "message": str(exc),
               "traceback": traceback.format_exc()}
    with open("results.json", "w") as f:
        json.dump(res, f, indent=2)


main()
