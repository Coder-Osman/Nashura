from native_runner.training.fair_final.policy import FullAction


def threshold_actions(information):
    actions = {}
    for card_index, card_id in enumerate(information.legality.card_ids):
        boundaries = {}
        for cell in range(576):
            earliest = next((delay for delay in range(1, 31)
                if information.legality.plays[card_index, cell, delay]), None)
            if earliest is not None and earliest > 1:
                boundaries.setdefault(earliest, []).append(cell)
        for earliest, cells in sorted(boundaries.items()):
            for cell in sorted({cells[0], cells[-1]}):
                for delay in (earliest, earliest + 1):
                    if delay <= 30:
                        action = FullAction(card_id, cell, delay)
                        if information.legality.legal(action):
                            actions[(card_id, cell, delay)] = action
    return list(actions.values())


def trials(information):
    emitted = False
    for owner in (0, 1):
        for action in threshold_actions(information[owner]):
            emitted = True
            yield dict(kind="earliest-legal-delay-and-next",
                actions={owner: action, 1 - owner: FullAction()})
    if not emitted:
        yield dict(kind="both-WAIT-baseline", actions={0: FullAction(), 1: FullAction()})
