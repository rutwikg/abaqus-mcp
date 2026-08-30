"""End-to-end Phase-4 demo: CAD (STEP) -> spec -> CAE build/mesh -> autonomous
run. Proves the whole authoring+solve pipeline on the L-bracket fixture."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abaqus_mcp.authoring import build_and_run_spec
from abaqus_mcp.spec import example_spec

MODELS = Path(__file__).resolve().parent / "models"


def main():
    spec = example_spec(step_file=str(MODELS / "bracket.step"))
    print("SPEC:\n", json.dumps(spec, indent=2)[:600], "...\n")
    out = build_and_run_spec(spec, job_name="cad_bracket", max_iters=4)
    print("=" * 72)
    print("BUILD ok:", out["build"]["ok"])
    print("BUILD stats:", out["build"].get("stats"))
    if not out["build"]["ok"]:
        print("BUILD errors:", out["build"]["errors"])
        print("traceback:", out["build"].get("traceback", "")[-1500:])
        print("log tail:\n", out["build"]["log"])
        return
    print("=" * 72)
    print(out["run"])
    print("=" * 72)
    print("PIPELINE SUCCEEDED:", out["succeeded"])


if __name__ == "__main__":
    main()
