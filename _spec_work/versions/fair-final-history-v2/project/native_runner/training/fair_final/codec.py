from .policy import FullAction


class ActionCodec:
    schema = "fair-final-stable-card-cell-delay.v1"

    def __init__(self, card_ids):
        self.card_ids = tuple(card_ids)
        if (len(self.card_ids) != 8 or len(set(self.card_ids)) != 8
                or any(type(card) is not int or card <= 0 for card in self.card_ids)):
            raise ValueError("action codec requires eight stable card identities")

    def encode(self, action):
        if not isinstance(action, FullAction):
            raise ValueError("complete action required")
        if action.card_id is None:
            return 0
        return 1 + (self.card_ids.index(action.card_id) * 576 + action.cell) * 31 + action.delay_slot

    def decode(self, identity):
        if type(identity) is not int or not 0 <= identity < 1 + 8 * 576 * 31:
            raise ValueError("action identity is outside the fixed vocabulary")
        if identity == 0:
            return FullAction()
        position, delay = divmod(identity - 1, 31)
        card, cell = divmod(position, 576)
        return FullAction(self.card_ids[card], cell, delay)
