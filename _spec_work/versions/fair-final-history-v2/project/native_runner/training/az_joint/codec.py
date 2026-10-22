import torch


class JointActionCodec:
    cards = 8
    cells = 18 * 32
    delays = 31
    size = 1 + cards * cells * delays

    @classmethod
    def encode(cls, card, cell, delay_ms):
        if (type(card) is not int or not 0 <= card < cls.cards or type(cell) is not int
                or not 0 <= cell < cls.cells or type(delay_ms) is not int
                or not 100 <= delay_ms <= 3000 or delay_ms % 100):
            raise ValueError("joint action outside executable vocabulary")
        return 1 + (card * cls.cells + cell) * cls.delays + delay_ms // 100

    @classmethod
    def decode(cls, action_id):
        if type(action_id) is not int or not 0 <= action_id < cls.size:
            raise ValueError("invalid joint action ID")
        if action_id == 0:
            return None
        placement, delay = divmod(action_id - 1, cls.delays)
        card, cell = divmod(placement, cls.cells)
        if delay == 0:
            raise ValueError("zero delay is encoded but never executable")
        return card, cell, delay * 100

    @classmethod
    def to_key(cls, action_id):
        decoded = cls.decode(action_id)
        if decoded is None:
            return "wait"
        card, cell, delay = decoded
        return f"play:{card}:{cell}:{delay // 100}"

    @classmethod
    def from_key(cls, key):
        if key == "wait":
            return 0
        parts = key.split(":")
        if len(parts) != 4 or parts[0] != "play":
            raise ValueError("invalid joint action key")
        action_id = cls.encode(int(parts[1]), int(parts[2]), int(parts[3]) * 100)
        if cls.to_key(action_id) != key:
            raise ValueError("noncanonical joint action key")
        return action_id

    @classmethod
    def legal_mask(cls, masks):
        cards, cells, delays, gates = (masks[name] for name in
                                      ("card_mask", "cell_masks", "delay_mask", "gate_mask"))
        batch = cards.shape[0]
        if (cards.shape != (batch, cls.cards) or cells.shape != (batch, cls.cards, cls.cells)
                or delays.shape != (batch, cls.cards, cls.delays) or gates.shape != (batch, 2)
                or any(tensor.dtype != torch.bool for tensor in (cards, cells, delays, gates))):
            raise ValueError("joint legality tensor contract differs")
        plays = cards[:, :, None, None] & cells[:, :, :, None] & delays[:, :, None, :]
        plays = plays & gates[:, 1, None, None, None]
        plays[..., 0] = False
        if not bool(gates[:, 0].all()):
            raise ValueError("joint decisions must retain WAIT")
        return torch.cat((gates[:, :1], plays.flatten(1)), dim=1)
