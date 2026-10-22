import torch

from native_runner.training.fair_final.adapter import stable_card_rows
from native_runner.training.fair_final.learning import conditional_teacher_kl as sequential_kl
from native_runner.training.fair_final.policy import JointPolicy


class DelayBatchPolicy(JointPolicy):
    def __init__(self, logits, legality, delay_logits, *, temperatures=(1., 1., 1., 1.)):
        super().__init__(logits, legality, temperatures=temperatures)
        self.delay_logits = delay_logits

    def delay_conditionals(self, card, cells, *, raw=False):
        masks = torch.stack([self.legality.mask((1, card, cell)) for cell in cells])
        logits = self.delay_logits(card, cells)
        masks = masks.to(logits.device)
        if logits.shape != masks.shape or not bool(torch.isfinite(logits[masks]).all()):
            raise ValueError("batched delay logits differ from legal prefixes")
        temperature = 1. if raw else self.temperatures[3]
        return (logits / temperature).masked_fill(~masks, -torch.inf).log_softmax(-1)


def conditional_teacher_kl(teacher, student, *, cell_chunk=64):
    if not isinstance(teacher, DelayBatchPolicy) or not isinstance(student, DelayBatchPolicy):
        return sequential_kl(teacher, student, cell_chunk=cell_chunk)
    if type(cell_chunk) is not int or cell_chunk < 1:
        raise ValueError("positive cell chunk required")
    if (teacher.legality.card_ids != student.legality.card_ids
            or not torch.equal(teacher.legality.plays.cpu(), student.legality.plays.cpu())
            or not torch.equal(teacher.legality.modes.cpu(), student.legality.modes.cpu())):
        raise ValueError("teacher and student prefixes have different legality")

    def kl(teacher_log, student_log, mask):
        teacher_log = teacher_log.detach().to(student_log.device)
        mask = mask.to(student_log.device)
        return (teacher_log.exp()[mask] * (teacher_log[mask] - student_log[mask])).sum()

    def conditional(prefix):
        teacher_log = teacher.conditionals(prefix)
        return kl(teacher_log, student.conditionals(prefix, raw=True), teacher.prefix_mask(prefix)), teacher_log.detach().exp()

    total, modes = conditional(())
    if not bool(teacher.prefix_mask()[1]):
        return total
    card_kl, cards = conditional((1,))
    total = total + modes[1] * card_kl
    for card in torch.where(teacher.prefix_mask((1,)))[0].tolist():
        cell_kl, cells = conditional((1, card))
        total = total + modes[1] * cards[card] * cell_kl
        legal_cells = torch.where(teacher.prefix_mask((1, card)))[0].tolist()
        for offset in range(0, len(legal_cells), cell_chunk):
            selected = legal_cells[offset:offset + cell_chunk]
            teacher_log = teacher.delay_conditionals(card, selected).detach()
            student_log = student.delay_conditionals(card, selected, raw=True)
            for index, cell in enumerate(selected):
                delay_kl = kl(teacher_log[index], student_log[index], teacher.legality.plays[card, cell])
                total = total + modes[1] * cards[card] * cells[cell] * delay_kl
    return total


def source_policy(source, frame, legality):
    policy = source.policy
    if tuple(policy.event_config.delay_offset_ms) != tuple(range(0, 6001, 100)):
        raise ValueError("source adapter requires the audited 61-bin delay vocabulary")
    if policy.config.ability_actions or not policy.backbone.config.snapshot_only:
        raise ValueError("source adapter requires stateless card-only A")
    device = next(policy.parameters()).device
    frame = frame.to_model_input(device, float_dtype=torch.float32)
    if frame.batch_size != 1:
        raise ValueError("source adapter takes one FAIR decision")
    rows = stable_card_rows(frame, policy.backbone.catalog, legality.card_ids)
    context = policy.encode(frame, policy.initial_state(1, device=device),
        episode_start=torch.ones(1, dtype=torch.bool, device=device))
    conditioned = {}

    def conditioning(card):
        if card not in conditioned:
            card_row = torch.tensor([rows[card]], device=device)
            conditioned[card] = policy._cell_conditioning(context, card_row)
        return conditioned[card]

    def delays(card, cells):
        keys, _, features = conditioning(card)
        embeddings = keys[0].flatten(1).transpose(0, 1)[cells]
        return policy.delay_head(torch.cat((features.expand(len(cells), -1), embeddings), dim=-1))[:, :31]

    def logits(prefix):
        if not prefix:
            return policy.gate_head(context.policy_context)[0]
        if prefix == (1,):
            return policy.card_logits(context)[0, list(rows)]
        if len(prefix) == 2:
            keys, query, _ = conditioning(prefix[1])
            return policy._score_cells(query, keys)[0]
        return delays(prefix[1], [prefix[2]])[0]

    return DelayBatchPolicy(logits, legality, delays), source.wdl_head(context.value_context)


def student_policies(student, frame, legalities, *, temperatures=(1., 1., 1., 1.)):
    if any(parameter.requires_grad for parameter in student.encoder.parameters()):
        return student(frame, legalities, temperatures=temperatures)
    shared, encoded = student.encode(frame)
    if len(legalities) != shared.shape[0]:
        raise ValueError("one legal information state per batch row required")
    policies = []
    for index, legality in enumerate(legalities):
        rows = list(stable_card_rows(frame, student.encoder.catalog, legality.card_ids, index=index))
        policies.append(student_policy(student.policy_head, shared[index],
            encoded.own_card_memory[index, rows], encoded.spatial_memory[index], legality,
            temperatures=temperatures))
    return policies, student.wdl_head(shared)


def student_policy(head, shared, cards, spatial, legality, *, temperatures=(1., 1., 1., 1.)):
    if shared.shape != (512,) or cards.ndim != 2 or spatial.ndim != 3:
        raise ValueError("conditional heads expect one encoded decision")
    keys = head.cell_key(spatial.unsqueeze(0)).squeeze(0).flatten(1).transpose(0, 1)
    features = {}

    def conditioning(card):
        if card not in features:
            features[card] = torch.cat((shared, cards[card]))
        return features[card]

    def delays(card, cells):
        return head.delay(torch.cat((conditioning(card).expand(len(cells), -1), keys[cells]), dim=-1))

    def logits(prefix):
        if not prefix:
            return head.mode(shared)
        if prefix == (1,):
            return cards @ head.card_query(shared) / cards.shape[-1] ** .5
        if len(prefix) == 2:
            return keys @ head.cell_query(conditioning(prefix[1])) / keys.shape[-1] ** .5
        return delays(prefix[1], [prefix[2]])[0]

    return DelayBatchPolicy(logits, legality, delays, temperatures=temperatures)
