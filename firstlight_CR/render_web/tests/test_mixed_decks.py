import copy
import itertools
import json

import pytest

from native_runner.training.mixed_decks import DECKS as TRAINING_DECKS
from render_web.config import DECK_CARDS, MIXED_DECK_IDS, write_json
from render_web.generate_match import resolve_decks
from render_web.local_render import validate_trace
from render_web.tests.test_workbench import api, trace, checkpoint
from tools.league_decks import HOG_CYCLE_DECK


def test_options_use_training_decks_and_card_labels(api):
    client, _, _ = api
    decks = client.get("/api/options").json()["decks"]
    mixed = [d for d in decks if d["training_id"] is not None]
    assert [d["id"] for d in mixed] == list(MIXED_DECK_IDS)
    assert next(d for d in mixed if d["id"] == "recruits-hogs")["label"] == "卫队猪"
    for deck in mixed:
        assert tuple(c["id"] for c in deck["cards"]) == TRAINING_DECKS[deck["training_id"]]
        assert all(c["label"] for c in deck["cards"])


def test_latest_checkpoint_follows_mixed_training_not_runtime_source(api):
    client, store, config = api
    old = dict(checkpoint(), run=config.source_run, iteration=1250)
    mixed = dict(checkpoint("mixed"), run="event-mixed-deck-100r-20260920", iteration=1267)
    unavailable = dict(checkpoint("missing"), iteration=1268, available=False)
    store.replace_checkpoints([old, mixed, unavailable])
    assert client.get("/api/checkpoints").json()["latest"] == mixed["id"]


@pytest.mark.parametrize("top,bottom", itertools.product(MIXED_DECK_IDS, repeat=2))
@pytest.mark.parametrize("mode", ["selfplay", "versus"])
def test_mixed_pairings_pin_cards_and_keep_them_on_retry(api, top, bottom, mode):
    client, store, _ = api
    response = client.post("/api/jobs", json=dict(mode=mode, top="top", bottom="bottom",
                                                deck_top=top, deck_bottom=bottom, seed=123))
    assert response.status_code == 202
    job = response.json()
    request = job["request"]
    assert request["deck_cards"] == dict(top=list(DECK_CARDS[top]), bottom=list(DECK_CARDS[bottom]))
    assert resolve_decks(request) == {0: DECK_CARDS[top], 1: DECK_CARDS[bottom]}
    assert request["bottom"]["id"] == ("top" if mode == "selfplay" else "bottom")
    assert request["action_constraints"]["delay"]["minimum_ms"] == 100
    store.replace_checkpoints([])
    client.post(f"/api/jobs/{job['id']}/cancel")
    assert client.post(f"/api/jobs/{job['id']}/retry").json()["request"] == request


def test_legacy_hog_retry_preserves_frozen_slot_order():
    request = dict(deck_top="hog-cycle", deck_bottom="hog-cycle")
    assert HOG_CYCLE_DECK != TRAINING_DECKS["hog"]
    assert resolve_decks(request, {"hog-cycle": HOG_CYCLE_DECK}) == {0: HOG_CYCLE_DECK, 1: HOG_CYCLE_DECK}


@pytest.mark.parametrize("cards", [None, {}, {"top": []}, {"top": list(range(8)), "bottom": list(range(8))},
                                   {"top": [1] * 8, "bottom": list(range(1, 9))},
                                   {"top": [True, *range(2, 9)], "bottom": list(range(1, 9))}])
def test_invalid_snapshots_fail_before_generation(cards):
    with pytest.raises(ValueError, match="deck snapshot"):
        resolve_decks(dict(deck_cards=cards))


@pytest.fixture
def mixed_trace(trace):
    root, request = trace
    request.update(deck_top="giant", deck_bottom="royal-giant",
                   deck_cards=dict(top=list(DECK_CARDS["giant"]), bottom=list(DECK_CARDS["royal-giant"])))
    stats = json.loads((root / "stats.json").read_text())
    stats.update(decks=[request["deck_top"], request["deck_bottom"]], deck_cards=request["deck_cards"])
    write_json(root / "stats.json", stats)
    decks = resolve_decks(request)
    write_json(root / "render-match.json", dict(battle={f"deck{o}": dict(sp=[dict(d=c) for c in deck])
                                                        for o, deck in decks.items()}))
    initial = dict(players=[dict(owner=o, hand=[dict(cardId=c) for c in deck[:4]],
                                cycle=[dict(cardId=c) for c in deck[4:]]) for o, deck in decks.items()])
    write_json(root / "initial.json", initial)
    actions = json.loads((root / "native-actions.json").read_text())
    actions[0]["receipt"]["cardId"] = decks[0][0]
    write_json(root / "native-actions.json", actions)
    return root, request


def test_generated_cards_match_request_and_opening_deal(mixed_trace):
    root, request = mixed_trace
    stats, _ = validate_trace(root, request)
    assert stats["deck_cards"] == request["deck_cards"]


@pytest.mark.parametrize("filename,mutate", [
    ("stats.json", lambda x: x["deck_cards"].update(top=list(DECK_CARDS["golem-beatdown"]))),
    ("render-match.json", lambda x: x["battle"]["deck1"]["sp"][0].update(d=26000009)),
    ("initial.json", lambda x: x["players"][0]["hand"][0].update(cardId=26000009)),
    ("native-actions.json", lambda x: x[0]["receipt"].update(cardId=26000009)),
])
def test_reject_mismatched_decks_before_local_render(mixed_trace, filename, mutate):
    root, request = mixed_trace
    request = copy.deepcopy(request)
    data = json.loads((root / filename).read_text())
    mutate(data)
    write_json(root / filename, data)
    with pytest.raises(ValueError, match="牌表"):
        validate_trace(root, request)
