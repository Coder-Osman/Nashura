from dataclasses import asdict
import hashlib
import json

import torch

from ..fair_search.contracts import semantic_digest
from ..fair_search.model import load_checkpoint
from .codec import JointActionCodec


A_SHA256 = "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
A_STATE_SHA256 = "a28248484e59bade485bcad4c856e9cb1dc3deb3c430df75b35586d1b611a714"


def parameter_digest(state):
    digest = hashlib.sha256()
    for name, tensor in sorted(state.items()):
        tensor = tensor.detach().cpu().contiguous()
        digest.update(json.dumps([name, str(tensor.dtype), list(tensor.shape)], separators=(",", ":")).encode())
        digest.update(tensor.reshape(-1).view(torch.uint8).numpy().tobytes())
    return digest.hexdigest()


class ASourceAdapter:
    def __init__(self, source, identity, *, expected_state=A_STATE_SHA256):
        if identity != A_SHA256 or parameter_digest(source.state_dict()) != expected_state:
            raise ValueError("frozen A source identity differs")
        self.model = source.eval().requires_grad_(False)
        self.identity = identity
        self.versions = self._versions()
        offsets = tuple(source.policy.event_config.delay_offset_ms)
        if offsets[:31] != tuple(range(0, 3001, 100)):
            raise ValueError("A delay vocabulary is not compatible")
        self.manifest = dict(schema="a-source-adapter.v1", source_sha256=identity,
            state_sha256=expected_state, event_config=asdict(source.policy.event_config),
            minimum_delay_ms=100, maximum_delay_ms=3000, wait_ticks=60,
            decode="legacy-cascade-greedy", value="complete-source-branch-wdl",
            preprocessing="snapshot-owner17-zero-model-relative.v1")
        self.sha256 = semantic_digest(self.manifest)

    @classmethod
    def load(cls, path, *, device="cpu"):
        model, _, identity = load_checkpoint(path, expected_sha256=A_SHA256, device=device)
        return cls(model, identity)

    def _versions(self):
        return tuple((name, tensor._version, id(tensor)) for name, tensor in
                     (*self.model.named_parameters(), *self.model.named_buffers()))

    def validate(self):
        if (self._versions() != self.versions or any(module.training for module in self.model.modules())
                or any(parameter.requires_grad for parameter in self.model.parameters())):
            raise ValueError("A source changed during migration")

    @torch.inference_mode()
    def predict(self, frame, masks):
        self.validate()
        JointActionCodec.legal_mask(masks)
        device = next(self.model.parameters()).device
        mapped = {name: tensor.to(device) for name, tensor in masks.items()}
        delays = torch.zeros((*mapped["delay_mask"].shape[:2],
                              self.model.policy.event_config.delay_bin_count), dtype=torch.bool, device=device)
        delays[..., :31] = mapped["delay_mask"]
        delays[..., 0] = False
        mapped["delay_mask"] = delays
        context, logits = self.model.encode(frame)
        action = self.model.policy.select_action(context, **mapped, sample=False, validate=True).action
        identifiers = []
        for index in range(frame.batch_size):
            if not bool(action.act[index]):
                identifiers.append(0)
            else:
                identifiers.append(JointActionCodec.encode(int(action.card_row[index]), int(action.cell[index]),
                                                           int(action.delay_bin[index]) * 100))
        legal = JointActionCodec.legal_mask(masks)
        if not all(bool(legal[index, identifier]) for index, identifier in enumerate(identifiers)):
            raise ValueError("adapted A action is illegal under the three-second contract")
        return identifiers, logits.softmax(-1).cpu()
