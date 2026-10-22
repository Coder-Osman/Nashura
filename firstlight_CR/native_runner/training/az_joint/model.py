from dataclasses import asdict, replace
from types import SimpleNamespace

import torch
from torch import nn

from ..v4 import components
from ..v4.config import ModelConfigV4
from ..v4.factory import production_semantic_bundle
from ..v4.model import UniversalCardPolicyV4
from .codec import JointActionCodec


class SemanticEncoder(nn.Module):
    encode_observation = UniversalCardPolicyV4.encode_observation

    def __init__(self, bundle, config):
        super().__init__()
        self.config = config
        self.catalog = bundle.card_catalog
        self.ability_catalog = bundle.ability_catalog
        self.entity_archetype_catalog = bundle.entity_archetype_catalog
        self.effect_catalog = bundle.effect_catalog
        self.mechanic_profile_catalog = bundle.mechanic_profile_catalog
        self.effect_encoder = components.EffectSemanticEncoder(config, self.effect_catalog)
        self.mechanic_profile_encoder = components.MechanicProfileEncoder(config, self.mechanic_profile_catalog)
        self.active_effect_encoder = components.ActiveEffectEncoder(config)
        self.card_encoder = components.GlobalCardEncoder(config, self.catalog, self.mechanic_profile_catalog)
        self.child_encoder = components.LocalChildSetEncoder(config, self.card_encoder, self.mechanic_profile_catalog)
        self.group_encoder = components.BattleGroupEncoder(config, self.card_encoder)
        self.tower_encoder = components.TowerEncoder(config, self.mechanic_profile_catalog)
        self.card_token_encoder = components.CardTokenEncoder(config, self.card_encoder)
        self.relation_transformer = components.RelationTransformerV4(config)
        self.spatial_encoder = components.SpatialScatterEncoder(config)
        self.scalar_encoder = components.GenericScalarEncoder(config)
        self.event_encoder = components.EventSetEncoder(config, self.card_encoder)
        self.previous_action_encoder = components.PreviousActionEncoder(config, self.card_encoder, self.mechanic_profile_catalog)
        self.candidate_encoder = components.CandidateEncoder(config, self.card_encoder,
            ability_features=self.ability_catalog.feature_tensor(config), mechanic_catalog=self.mechanic_profile_catalog)

    def forward(self, frame):
        scalars = frame.match_scalars.clone()
        scalars[:, 17] = 0
        return self.encode_observation(replace(frame, match_scalars=scalars), validate=True)


class ResidualBlock(nn.Module):
    def __init__(self):
        super().__init__()
        self.norm = nn.LayerNorm(512)
        self.layers = nn.Sequential(nn.Linear(512, 512), nn.SiLU(), nn.Linear(512, 512))

    def forward(self, inputs):
        return inputs + self.layers(self.norm(inputs))


class JointPolicyValue(nn.Module):
    def __init__(self, *, bundle=None, encoder_config=None):
        super().__init__()
        self.encoder_config = encoder_config or replace(ModelConfigV4(), snapshot_only=True)
        if not self.encoder_config.snapshot_only:
            raise ValueError("joint model requires stateless semantic encoding")
        self.encoder = SemanticEncoder(bundle or production_semantic_bundle(), self.encoder_config)
        self.trunk = nn.Sequential(nn.Linear(self.encoder_config.core_input_width, 512), nn.SiLU(),
                                   ResidualBlock(), ResidualBlock(), nn.LayerNorm(512))
        self.global_projection = nn.Linear(512, 128)
        self.card_projection = nn.Linear(self.encoder_config.token_dim, 128)
        self.spatial_projection = nn.Conv2d(self.encoder_config.spatial_hidden_channels, 128, 1)
        self.local_norm = nn.LayerNorm(128)
        self.joint_output = nn.Linear(128, JointActionCodec.delays)
        self.wait_output = nn.Linear(512, 1)
        self.wdl_head = nn.Sequential(nn.Linear(512, 256), nn.SiLU(), nn.Linear(256, 3))
        for output in (self.joint_output, self.wait_output, self.wdl_head[-1]):
            nn.init.zeros_(output.weight)
            nn.init.zeros_(output.bias)

    @property
    def architecture(self):
        return dict(schema="az-joint-model.v1", encoder=asdict(self.encoder_config),
                    catalogs={name: getattr(self.encoder, name).catalog_id for name in
                              ("catalog", "ability_catalog", "entity_archetype_catalog",
                               "effect_catalog", "mechanic_profile_catalog")})

    @classmethod
    def from_source(cls, source):
        backbone = source.policy.backbone
        bundle = SimpleNamespace(card_catalog=backbone.catalog, ability_catalog=backbone.ability_catalog,
            entity_archetype_catalog=backbone.entity_archetype_catalog,
            effect_catalog=backbone.effect_catalog, mechanic_profile_catalog=backbone.mechanic_profile_catalog)
        model = cls(bundle=bundle, encoder_config=backbone.config)
        expected = model.encoder.state_dict()
        supplied = backbone.state_dict()
        copied = {}
        for name, tensor in expected.items():
            if name not in supplied or tensor.shape != supplied[name].shape or tensor.dtype != supplied[name].dtype:
                raise ValueError("source semantic encoder mapping differs: " + name)
            copied[name] = supplied[name]
        model.encoder.load_state_dict(copied, strict=True)
        model.migration_mapping = {"encoder." + name: "policy.backbone." + name for name in copied}
        return model

    def encode(self, frame):
        frame = frame.to_model_input(next(self.parameters()).device, float_dtype=torch.float32)
        encoded = self.encoder(frame)
        combined = torch.cat([getattr(encoded, name) for name in ("scene_state", "spatial_summary",
            "scalar_summary", "event_summary", "previous_action_summary", "candidate_summary")], dim=-1)
        shared = self.trunk(combined)
        return shared, encoded

    def value_only(self, frame):
        shared, _ = self.encode(frame)
        return self.wdl_head(shared)

    @staticmethod
    def value(wdl_logits):
        probabilities = wdl_logits.softmax(-1)
        return probabilities[..., 0] - probabilities[..., 2]

    def forward(self, frame, masks):
        shared, encoded = self.encode(frame)
        global_features = self.global_projection(shared)[:, None, None, :]
        card_features = self.card_projection(encoded.own_card_memory[:, :8])[:, :, None, :]
        spatial = self.spatial_projection(encoded.spatial_memory).flatten(2).transpose(1, 2)[:, None, :, :]
        local = torch.nn.functional.silu(self.local_norm(global_features + card_features + spatial))
        plays = self.joint_output(local).flatten(1)
        legal = JointActionCodec.legal_mask({name: tensor.to(shared.device) for name, tensor in masks.items()})
        count = legal[:, 1:].sum(-1, keepdim=True).clamp_min(1).to(plays.dtype)
        logits = torch.cat((self.wait_output(shared), plays - count.log()), dim=-1)
        logits = logits.masked_fill(~legal, -torch.inf)
        return logits, self.wdl_head(shared)
