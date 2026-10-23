import hashlib
import zlib


THRESHOLDS = (110, 600, 1200)
BRANCH_TICKS = 60
MATCH_SEED = 78101981
EXECUTION_SEED = 78101991
PRIORITY = ((26000021, (3, 14)), (26000014, (3, 10)), (28000000, (3, 25)),
    (27000000, (8, 10)), (26000010, (3, 12)), (26000030, (3, 12)),
    (28000011, (3, 25)), (26000038, (3, 12)), (26000000, (3, 12)))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def choose(information):
    from native_runner.training.fair_final.policy import FullAction

    delay = 1 if information.tick % 2 == 0 else 30
    for card, target in PRIORITY:
        if card not in information.legality.card_ids:
            continue
        index = information.legality.card_ids.index(card)
        cells = information.legality.plays[index, :, delay].nonzero().flatten().tolist()
        if cells:
            cell = min(cells, key=lambda candidate: (
                (candidate % 18 - target[0]) ** 2 + (candidate // 18 - target[1]) ** 2, candidate))
            action = FullAction(card, cell, delay)
            if not information.legality.legal(action):
                raise ValueError("combat choice differs from complete legal mask")
            return action
    action = FullAction()
    if not information.legality.legal(action):
        raise ValueError("combat WAIT is illegal")
    return action


def mask_record(information):
    legality = information.legality
    content = bytes(legality.plays.flatten().tolist())
    return dict(card_ids=list(legality.card_ids), shape=list(legality.plays.shape),
        modes=legality.modes.tolist(), plays_zlib_hex=zlib.compress(content).hex(),
        plays_sha256=hashlib.sha256(content).hexdigest())


def validate_selection(row):
    action, mask = row["action"], row["mask"]
    cards, shape = mask["card_ids"], mask["shape"]
    if (len(cards) != len(set(cards)) or not cards or any(type(card) is not int or card <= 0 for card in cards)
            or len(shape) != 3 or any(type(value) is not int for value in shape)
            or shape[0] != len(cards) or not 1 <= shape[1] <= 576 or shape[2] != 31
            or len(mask["modes"]) != 2 or any(type(value) is not bool for value in mask["modes"])):
        raise ValueError("combat legal mask shape differs")
    size = shape[0] * shape[1] * shape[2]
    decoder = zlib.decompressobj()
    content = decoder.decompress(bytes.fromhex(mask["plays_zlib_hex"]), size + 1)
    if (len(content) != size or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail
            or set(content) - {0, 1} or any(content[::31])
            or hashlib.sha256(content).hexdigest() != mask["plays_sha256"]
            or mask["modes"][1] != bool(any(content))):
        raise ValueError("combat legal mask bytes differ")
    if set(action) != {"card_id", "cell", "delay_slot"}:
        raise ValueError("combat complete action differs")
    if action["card_id"] is None:
        legal = action["cell"] is None and action["delay_slot"] is None and mask["modes"][0]
    else:
        card, cell, delay = action["card_id"], action["cell"], action["delay_slot"]
        legal = (type(card) is int and card in cards and type(cell) is int and 0 <= cell < shape[1]
            and type(delay) is int and 1 <= delay <= 30
            and content[(cards.index(card) * shape[1] + cell) * 31 + delay] == 1)
    if not legal:
        raise ValueError("combat selected action is illegal")
