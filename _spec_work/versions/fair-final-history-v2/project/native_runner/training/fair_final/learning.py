import torch
import torch.nn.functional as functional


def supervised_loss(policies, wdl_logits, targets, labels):
    if (len(policies) != len(targets) or wdl_logits.shape != (len(policies), 3)
            or labels.shape != (len(policies),) or not len(policies)):
        raise ValueError("mixed Full/Fast batch shapes differ")
    policy_rows = []
    for policy, target in zip(policies, targets):
        if target is None:
            continue
        if (not target or len({action.key for action, visits in target}) != len(target)
                or any(type(visits) is not int or visits <= 0 for action, visits in target)
                or sum(visits for action, visits in target) != 128):
            raise ValueError("only complete FULL N/128 targets enter policy loss")
        policy_rows.append(-sum(policy.log_prob(action) * (visits / 128) for action, visits in target))
    policy_loss = torch.stack(policy_rows).mean() if policy_rows else wdl_logits.sum() * 0
    value_loss = functional.cross_entropy(wdl_logits, labels)
    if not bool(torch.isfinite(policy_loss + value_loss)):
        raise ValueError("nonfinite policy or value loss")
    return dict(loss=policy_loss + value_loss, policy_loss=policy_loss, value_loss=value_loss,
                policy_rows=len(policy_rows), value_rows=len(policies))


def conditional_teacher_kl(teacher, student, *, cell_chunk=64):
    if type(cell_chunk) is not int or cell_chunk < 1:
        raise ValueError("positive cell chunk required")
    if (teacher.legality.card_ids != student.legality.card_ids
            or not torch.equal(teacher.legality.plays.cpu(), student.legality.plays.cpu())
            or not torch.equal(teacher.legality.modes.cpu(), student.legality.modes.cpu())):
        raise ValueError("teacher and student prefixes have different legality")

    def conditional(prefix):
        teacher_log = teacher.conditionals(prefix).detach()
        student_log = student.conditionals(prefix, raw=True)
        mask = teacher.prefix_mask(prefix).to(student_log.device)
        teacher_log = teacher_log.to(student_log.device)
        probability = teacher_log.exp()
        return (probability[mask] * (teacher_log[mask] - student_log[mask])).sum(), probability

    mode_kl, modes = conditional(())
    if not bool(teacher.prefix_mask()[1]):
        return mode_kl
    card_kl, cards = conditional((1,))
    total = mode_kl + modes[1] * card_kl
    for card in torch.where(teacher.prefix_mask((1,)))[0].tolist():
        cell_kl, cells = conditional((1, card))
        total = total + modes[1] * cards[card] * cell_kl
        legal_cells = torch.where(teacher.prefix_mask((1, card)))[0].tolist()
        for offset in range(0, len(legal_cells), cell_chunk):
            for cell in legal_cells[offset:offset + cell_chunk]:
                delay_kl, _ = conditional((1, card, cell))
                total = total + modes[1] * cards[card] * cells[cell] * delay_kl
    return total
