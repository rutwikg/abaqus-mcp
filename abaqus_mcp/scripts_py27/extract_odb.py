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


# Whole-model energy histories. These are what decide whether a COMPLETED run
# is physically meaningful: a job can converge cleanly while hourglass energy
# carries the load, and Abaqus reports that as success.
ENERGY_VARS = ("ALLIE", "ALLKE", "ALLAE", "ALLPD", "ALLSE", "ALLDMD",
               "ALLWK", "ALLVD", "ALLFD", "ETOTAL")


def energy_histories(step):
    """{var: [(t, value), ...]} for the whole-model energy outputs."""
    series = {}
    try:
        regions = step.historyRegions
    except Exception:
        return series
    for rname in regions.keys():
        region = regions[rname]
        try:
            outputs = region.historyOutputs
        except Exception:
            continue
        for var in outputs.keys():
            if var not in ENERGY_VARS:
                continue
            data = [(float(t), float(v)) for t, v in outputs[var].data]
            # Whole-model energies live on the assembly region; if several
            # regions report the same variable, keep the longest series.
            if var not in series or len(data) > len(series[var]):
                series[var] = data
    return series


def energy_summary(series):
    """Final values plus the ratios an analyst actually checks.

    Ratios are evaluated only where ALLIE has grown past 5% of its peak.
    Early in an impact ALLIE is ~0, so an unguarded ALLAE/ALLIE is enormous
    and meaningless -- reporting that as 'invalid' would be a false alarm.
    """
    if not series:
        return {}
    final = {}
    for var, data in series.items():
        if data:
            final[var] = data[-1][1]

    out = {"final": final}
    allie = series.get("ALLIE") or []
    if not allie:
        return out
    peak_ie = max(abs(v) for _, v in allie) or 0.0
    out["peak_ALLIE"] = peak_ie
    if peak_ie <= 0.0:
        return out

    floor = 0.05 * peak_ie
    ie_at = dict(allie)
    for num in ("ALLAE", "ALLKE", "ALLPD", "ALLDMD"):
        data = series.get(num)
        if not data:
            continue
        worst_t, worst_r = None, 0.0
        for t, v in data:
            ie = ie_at.get(t)
            if ie is None or abs(ie) < floor:
                continue
            r = abs(v) / abs(ie)
            if r > worst_r:
                worst_r, worst_t = r, t
        out["max_%s_over_ALLIE" % num] = worst_r
        out["max_%s_over_ALLIE_time" % num] = worst_t
        if num in final and final.get("ALLIE"):
            out["final_%s_over_ALLIE" % num] = abs(final[num]) / abs(final["ALLIE"])

    etot = series.get("ETOTAL")
    if etot:
        # ETOTAL should stay near its initial value; drift is normalised by the
        # peak internal energy so it is comparable across models.
        drift = max(abs(v - etot[0][1]) for _, v in etot)
        out["ETOTAL_drift_over_ALLIE"] = drift / peak_ie
    return out


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
            series = energy_histories(step)
            if series:
                summary = energy_summary(series)
                if summary:
                    entry["energy"] = summary
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
