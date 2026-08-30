"""End-to-end demo: run three deliberately-broken decks through the
autonomous correct-and-retry loop and print the narrative for each."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from abaqus_mcp.loop import autocorrect_run

MODELS = Path(__file__).resolve().parent / "models"

CASES = [
    ("broken_typo", "Input-deck typo (undefined material/set)"),
    ("broken_singular", "Rigid-body singularity"),
    ("broken_convergence", "Convergence failure (forced single increment)"),
]


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else None
    for stem, label in CASES:
        if only and only not in stem:
            continue
        print("\n" + "=" * 72)
        print("CASE:", label)
        print("=" * 72)
        result = autocorrect_run(
            MODELS / (stem + ".inp"),
            job_name="ac_" + stem,
            max_iters=5,
        )
        print(result.narrative())


if __name__ == "__main__":
    main()
