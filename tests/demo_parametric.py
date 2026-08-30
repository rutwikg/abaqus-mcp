"""Phase-4b demo: parametric geometry (no CAD file) -> build/mesh -> autonomous
run. Builds a notched bar in tension straight from a described shape."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abaqus_mcp.authoring import build_and_run_spec
from abaqus_mcp.spec import example_parametric_spec


def main():
    shape = sys.argv[1] if len(sys.argv) > 1 else "notched_bar"
    spec = example_parametric_spec()
    if shape != "notched_bar":
        # quick alternate shapes for spot-checking the builders
        presets = {
            "block": {"type": "parametric", "shape": "block",
                      "params": {"lx": 40.0, "ly": 10.0, "lz": 10.0}},
            "cylinder": {"type": "parametric", "shape": "cylinder",
                         "params": {"radius": 10.0, "height": 40.0}},
            "l_bracket": {"type": "parametric", "shape": "l_bracket",
                          "params": {"arm1": 60.0, "arm2": 60.0, "width": 20.0,
                                     "thickness": 10.0}},
        }
        spec["geometry"] = presets[shape]
        spec["model_name"] = shape
        if shape == "cylinder":
            # Cylinder axis is z; use the flat end faces, not x-faces.
            spec["bcs"][0]["face"] = {"select": "zmin"}
            spec["loads"][0]["face"] = {"select": "zmax"}

    out = build_and_run_spec(spec, job_name="param_" + shape, max_iters=4)
    print("=" * 72)
    print("BUILD ok:", out["build"]["ok"], "| stats:", out["build"].get("stats"))
    if not out["build"]["ok"]:
        print("errors:", out["build"]["errors"])
        print("traceback:", out["build"].get("traceback", "")[-1500:])
        return
    print("=" * 72)
    print(out["run"])
    print("PIPELINE SUCCEEDED:", out["succeeded"])


if __name__ == "__main__":
    main()
