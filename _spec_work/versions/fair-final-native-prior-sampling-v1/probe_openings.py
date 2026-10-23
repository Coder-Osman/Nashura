import argparse
import json
from pathlib import Path
import sys
import time

from prior_sampling import read_rows, sha, validate_rows
from native_runner.match_factory import MatchConfig, NATIVE_FINALIZATION_DEADLINE_TICK, tournament_level_kwargs
from native_runner.training.fair_final.native import FinalBattleEnv
from native_runner.training.fair_search.belief import initial_draw
from native_runner.training.fair_search.config import HOG_DECK
from native_runner.training.fair_search.transport import create_search_native


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("settings", type=Path)
    parser.add_argument("variant", choices=("primary", "repeat"))
    args = parser.parse_args()
    settings = json.loads(args.settings.read_text())
    sys.path.insert(0, settings["puct_root"])
    for name, expected in settings["runtime_sha256"].items():
        if sha(Path(settings["project"]) / name) != expected:
            raise ValueError("opening probe frozen runtime differs")
    for name, expected in settings["source_sha256"].items():
        if sha(Path(settings["root"]) / name) != expected:
            raise ValueError("opening probe source differs")
    selected = settings["seeds"] if args.variant == "primary" else settings["seeds"][:settings["repeat_count"]]
    output = Path(settings["root"]) / args.variant
    started = time.monotonic()
    native = None
    try:
        with (output / "rows.jsonl").open("x") as handle:
            for ordinal, seed in enumerate(selected):
                if time.monotonic() - started > settings["timeout_seconds"] - 120:
                    raise TimeoutError("opening sample deadline reached")
                native = create_search_native(settings["port"], transport="compact-delta")
                try:
                    environment = FinalBattleEnv(native=native)
                    environment.reset(match_config=MatchConfig(deck0=HOG_DECK, deck1=HOG_DECK,
                        seed=seed, end_tick=NATIVE_FINALIZATION_DEADLINE_TICK, **tournament_level_kwargs()),
                        options=dict(render_mode="headless", decision_ticks=1, event_driven_decisions=False,
                            allow_initial_remaining_runtime_rejections=True))
                    raw = environment.raw_observation
                    players = []
                    for owner in (0, 1):
                        player = next(player for player in raw["players"] if player["owner"] == owner)
                        cards, slots = initial_draw(player)
                        players.append(dict(owner=owner, cards=list(cards), slots=list(slots)))
                    row = dict(ordinal=ordinal, seed=seed, tick=int(raw["tick"]), players=players)
                    validate_rows([dict(row, ordinal=0)], [seed], HOG_DECK)
                    handle.write(json.dumps(row, sort_keys=True) + "\n")
                    handle.flush()
                finally:
                    native.close_transport()
                    native = None
                if ordinal % 16 == 0 or ordinal + 1 == len(selected):
                    print(json.dumps(dict(completed=ordinal + 1, seconds=time.monotonic() - started)), flush=True)
        validate_rows(read_rows(output / "rows.jsonl"), selected, HOG_DECK)
        report = dict(schema="fair-final-native-opening-probe.v1", status="complete",
            rows_sha256=sha(output / "rows.jsonl"), rows=len(selected), deck=list(HOG_DECK),
            seconds=time.monotonic() - started, variant=args.variant,
            runtime_sha256=settings["runtime_sha256"], source_sha256=settings["source_sha256"],
            training_admissible=False, gates_closed=False)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    finally:
        if native is not None:
            native.close_transport()


if __name__ == "__main__":
    main()
