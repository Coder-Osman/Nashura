from dataclasses import asdict

import torch
from torch import nn

from ..az_joint.model import SemanticEncoder, ResidualBlock
from ..v4.config import ModelConfigV4
from ..v4.factory import production_semantic_bundle
from .adapter import stable_card_rows
from .policy import JointPolicy


class ConditionalHeads(nn.Module):
    def __init__(self, *, card_width=256, spatial_width=64):
        super().__init__()
        self.mode = nn.Sequential(nn.Linear(512, 256), nn.SiLU(), nn.Linear(256, 2))
        self.card_query = nn.Linear(512, card_width)
        self.cell_query = nn.Sequential(nn.Linear(512 + card_width, 256), nn.SiLU(), nn.Linear(256, 128))
        self.cell_key = nn.Conv2d(spatial_width, 128, 1)
        self.delay = nn.Sequential(nn.Linear(512 + card_width + 128, 256), nn.SiLU(), nn.Linear(256, 31))

    def logits_for(self, shared, cards, spatial):
        if shared.shape != (512,) or cards.ndim != 2 or spatial.ndim != 3:
            raise ValueError("conditional heads expect one encoded decision")
        keys = self.cell_key(spatial.unsqueeze(0)).squeeze(0).flatten(1).transpose(0, 1)

        def logits(prefix):
            if not prefix:
                return self.mode(shared)
            if prefix == (1,):
                return cards @ self.card_query(shared) / cards.shape[-1] ** .5
            selected = cards[prefix[1]]
            features = torch.cat((shared, selected))
            if len(prefix) == 2:
                return keys @ self.cell_query(features) / keys.shape[-1] ** .5
            return self.delay(torch.cat((features, keys[prefix[2]])))

        return logits


class ConditionalPolicyValue(nn.Module):
    def __init__(self, *, bundle=None, encoder_config=None):
        super().__init__()
        self.encoder_config = encoder_config or ModelConfigV4(snapshot_only=True)
        if not self.encoder_config.snapshot_only:
            raise ValueError("FAIR final model has no persistent recurrent state")
        self.encoder = SemanticEncoder(bundle or production_semantic_bundle(), self.encoder_config)
        self.trunk = nn.Sequential(nn.Linear(self.encoder_config.core_input_width, 512), nn.SiLU(),
                                   ResidualBlock(), ResidualBlock(), nn.LayerNorm(512))
        self.policy_head = ConditionalHeads(card_width=self.encoder_config.token_dim,
                                           spatial_width=self.encoder_config.spatial_hidden_channels)
        self.wdl_head = nn.Sequential(nn.Linear(512, 256), nn.SiLU(), nn.Linear(256, 3))

    @property
    def architecture(self):
        return dict(schema='fair-final-conditional-model.v1', encoder=asdict(self.encoder_config),
            mask_semantics=JointPolicy.mask_semantics, value_order=['WIN', 'DRAW', 'LOSS'],
            catalogs={name: getattr(self.encoder, name).catalog_id for name in
                      ('catalog', 'ability_catalog', 'entity_archetype_catalog', 'effect_catalog', 'mechanic_profile_catalog')})

    def copy_encoder(self, source):
        backbone = source.policy.backbone
        if asdict(backbone.config) != asdict(self.encoder_config):
            raise ValueError("source encoder semantic configuration differs")
        for name, identity in self.architecture['catalogs'].items():
            if getattr(backbone, name).catalog_id != identity:
                raise ValueError("source catalog identity differs")
        supplied = backbone.state_dict()
        for name, tensor in self.encoder.state_dict().items():
            if name not in supplied or supplied[name].shape != tensor.shape or supplied[name].dtype != tensor.dtype:
                raise ValueError('source encoder mapping differs: ' + name)
        self.encoder.load_state_dict({name: supplied[name] for name in self.encoder.state_dict()}, strict=True)
        return {f'encoder.{name}': f'policy.backbone.{name}' for name in self.encoder.state_dict()}

    def encode(self, frame):
        frame = frame.to_model_input(next(self.parameters()).device, float_dtype=torch.float32)
        encoded = self.encoder(frame)
        shared = self.trunk(torch.cat([getattr(encoded, name) for name in ('scene_state', 'spatial_summary',
            'scalar_summary', 'event_summary', 'previous_action_summary', 'candidate_summary')], dim=-1))
        return shared, encoded

    def forward(self, frame, legalities, *, temperatures=(1., 1., 1., 1.)):
        shared, encoded = self.encode(frame)
        if len(legalities) != shared.shape[0]:
            raise ValueError("one legal information state per batch row required")
        policies = []
        for index, legality in enumerate(legalities):
            rows = list(stable_card_rows(frame, self.encoder.catalog, legality.card_ids, index=index))
            policies.append(JointPolicy(self.policy_head.logits_for(shared[index],
                encoded.own_card_memory[index, rows], encoded.spatial_memory[index]), legality,
                temperatures=temperatures))
        return policies, self.wdl_head(shared)

    def value_only(self, frame):
        shared, _ = self.encode(frame)
        return self.wdl_head(shared)
