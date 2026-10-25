import json
from pathlib import Path
import sys

import reservation_base
from cases import trials
from native_runner.training.fair_final.episode import atomic_json


def main():
    reservation_base.trials = trials
    reservation_base.main()
    output = Path(sys.argv[sys.argv.index("--output") + 1])
    report = json.loads(output.read_text())
    report.update(boundary_protocol="fair-final-earliest-legal-delay.v1",
        general_pending_gate_closed=False, gates_closed=False,
        exclusions=["all-spatial-cells", "all-trajectories", "global-unreachability",
            "pending-belief-reconstruction", "complete-game-search"])
    atomic_json(output, report)


if __name__ == "__main__":
    main()
