from dataclasses import asdict, dataclass, replace
from types import SimpleNamespace

import torch
from torch import nn

from ..v4 import components
from ..v4.config import ModelConfigV4
from ..v4.factory import production_semantic_bundle
from ..v4.model import UniversalCardPolicyV4, null_event_set, snapshot_match_scalars
from ..v4.components import assemble_global_tokens
from .codec import JointActionCodec


@dataclass(slots=True)
class SSPIEncodedObservation:
    scene_state: torch.Tensor
    token_memory: torch.Tensor
    token_mask: torch.Tensor
    own_card_memory: torch.Tensor
    group_memory: torch.Tensor
    child_memory: torch.Tensor
    contextual_child_memory: torch.Tensor
    spatial_memory: torch.Tensor
    spatial_summary: torch.Tensor
    scalar_summary: torch.Tensor
    hand_summary: torch.Tensor
    event_summary: torch.Tensor
    previous_action_summary: torch.Tensor
    candidate_memory: torch.Tensor
    candidate_summary: torch.Tensor
    decision_context: torch.Tensor
    tower_value_context: torch.Tensor


class ChildGroupContextFusion(nn.Module):
    """Return group-level relation context to each retained child without global child attention."""

    def __init__(self, child_dim: int, group_dim: int) -> None:
        super().__init__()
        self.group_projection = nn.Linear(group_dim, child_dim)
        self.gate = nn.Linear(2 * child_dim, child_dim)
        self.norm = nn.LayerNorm(child_dim)

    def forward(self, children, groups, child_group_index, child_mask):
        if children.ndim != 3 or groups.ndim != 3 or child_group_index.shape != children.shape[:2]:
            raise ValueError("child/group context tensors have incompatible shapes")
        safe_index = child_group_index.clamp(min=0, max=groups.shape[1] - 1)
        context = torch.gather(groups, 1, safe_index.unsqueeze(-1).expand(-1, -1, groups.shape[-1]))
        projected = self.group_projection(context)
        gate = torch.sigmoid(self.gate(torch.cat((children, projected), dim=-1)))
        result = self.norm(children + gate * projected)
        return result * child_mask.unsqueeze(-1)


class TowerValueEncoder(nn.Module):
    """Permutation-invariant value context from the six FAIR tower rows only."""

    def __init__(self, config: ModelConfigV4) -> None:
        super().__init__()
        width = 128
        self.feature = nn.Sequential(nn.Linear(config.tower_feature_dim, width), nn.SiLU(),
            nn.Linear(width, width))
        self.position = nn.Linear(6, width)
        self.tower_type = nn.Embedding(config.tower_type_count, width)
        self.owner_type = nn.Embedding(config.owner_type_count, width)
        self.norm = nn.LayerNorm(width)
        self.pool = nn.Sequential(nn.Linear(2 * width, 256), nn.SiLU(), nn.LayerNorm(256))

    def forward(self, towers):
        mask = towers.mask
        geometry = torch.cat((towers.position, towers.extent), dim=-1)
        memory = self.norm(self.feature(towers.features) + self.position(geometry)
            + self.tower_type(towers.tower_type) + self.owner_type(towers.owner_type))
        memory = memory * mask.unsqueeze(-1)
        count = mask.sum(dim=1, keepdim=True).clamp_min(1).to(memory.dtype)
        average = memory.sum(dim=1) / count
        maximum = memory.masked_fill(~mask.unsqueeze(-1), torch.finfo(memory.dtype).min).amax(dim=1)
        maximum = torch.where(mask.any(dim=1, keepdim=True), maximum, torch.zeros_like(maximum))
        return self.pool(torch.cat((average, maximum), dim=-1))


class SemanticEncoder(nn.Module):
    encode_observation = UniversalCardPolicyV4.encode_observation

    def __init__(self, bundle, config, *, cache_static_inference=False, sspi_fusion=False):
        super().__init__()
        self.sspi_fusion = sspi_fusion
        self.cache_static_inference = cache_static_inference
        self._clear_static_memory_cache()
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
        self.spatial_encoder = components.SpatialScatterEncoder(
            config, condition_dim=config.token_dim if sspi_fusion else 0
        )
        self.scalar_encoder = components.GenericScalarEncoder(config)
        self.event_encoder = components.EventSetEncoder(config, self.card_encoder)
        self.previous_action_encoder = components.PreviousActionEncoder(config, self.card_encoder, self.mechanic_profile_catalog)
        self.candidate_encoder = components.CandidateEncoder(config, self.card_encoder,
            ability_features=self.ability_catalog.feature_tensor(config), mechanic_catalog=self.mechanic_profile_catalog)
        if sspi_fusion:
            self.child_context_fusion = ChildGroupContextFusion(config.child_dim, config.group_dim)
            self.decision_context_encoder = nn.Sequential(
                nn.Linear(config.token_dim + config.scalar_summary_dim + 128, config.token_dim),
                nn.SiLU(), nn.Linear(config.token_dim, config.token_dim), nn.LayerNorm(config.token_dim),
            )
            self.tower_value_encoder = TowerValueEncoder(config)

    def forward(self, frame, hand_summary=None):
        scalars = frame.match_scalars.clone()
        scalars[:, 17] = 0
        frame = replace(frame, match_scalars=scalars)
        if not self.sspi_fusion:
            return self.encode_observation(frame, validate=True,
                static_memories=self._inference_static_memories())
        if hand_summary is None or hand_summary.shape != (frame.batch_size, 128):
            raise ValueError("SSPI semantic fusion requires one 128-wide hand summary per frame")
        return self._encode_sspi(frame, hand_summary)

    def _encode_sspi(self, batch, hand_summary):
        batch.validate(
            self.config,
            card_vocab_size=self.catalog.vocab_size,
            ability_vocab_size=self.ability_catalog.vocab_size,
            entity_archetype_vocab_size=self.entity_archetype_catalog.vocab_size,
            effect_vocab_size=self.effect_catalog.vocab_size,
        )
        static_memories = self._inference_static_memories()
        if static_memories is None:
            effect_memory = self.effect_encoder.all_effects()
            profile_memory = self.mechanic_profile_encoder(effect_memory)
        else:
            effect_memory, profile_memory = static_memories
        child_effects, tower_effects = self.active_effect_encoder(
            batch.active_effects, effect_memory=effect_memory,
            child_count=batch.groups.child_mask.shape[1], tower_count=batch.towers.mask.shape[1],
        )
        child_memory, local_slots = self.child_encoder(
            batch.groups, profile_memory=profile_memory, active_effect_memory=child_effects
        )
        group_tokens = self.group_encoder(batch.groups, local_slots, profile_memory=profile_memory)
        tower_tokens = self.tower_encoder(
            batch.towers, profile_memory=profile_memory, active_effect_memory=tower_effects
        )
        own_tokens = self.card_token_encoder(batch.own_cards, owner_type=0, profile_memory=profile_memory)
        opponent_tokens = self.card_token_encoder(batch.opponent_cards, owner_type=1, profile_memory=profile_memory)
        global_inputs = assemble_global_tokens(
            towers=batch.towers, tower_tokens=tower_tokens,
            own_cards=batch.own_cards, own_tokens=own_tokens,
            opponent_cards=batch.opponent_cards, opponent_tokens=opponent_tokens,
            groups=batch.groups, group_tokens=group_tokens,
        )
        scene_state, token_memory, token_mask, own_card_memory, group_memory = self.relation_transformer(
            global_inputs, batch.relation_edges
        )
        scalars = snapshot_match_scalars(batch.match_scalars) if self.config.snapshot_only else batch.match_scalars
        scalar_summary = self.scalar_encoder(scalars)
        decision_context = self.decision_context_encoder(
            torch.cat((scene_state, scalar_summary, hand_summary), dim=-1)
        )
        contextual_children = self.child_context_fusion(
            child_memory, group_memory, batch.groups.child_group_index, batch.groups.child_mask
        )
        spatial_memory, spatial_summary = self.spatial_encoder(
            batch.explicit_spatial_planes, contextual_children,
            batch.groups.child_position, batch.groups.child_extent,
            batch.groups.child_radius, batch.groups.child_mask,
            condition=decision_context,
        )
        events = null_event_set(batch.events) if self.config.snapshot_only else batch.events
        event_summary = self.event_encoder(
            events, group_memory=group_memory, token_memory=token_memory, profile_memory=profile_memory
        )
        previous_action_summary = self.previous_action_encoder(
            batch.previous_action, profile_memory=profile_memory
        )
        candidate_memory, candidate_summary = self.candidate_encoder(
            batch.candidates, own_card_memory=own_card_memory, group_memory=group_memory,
            child_memory=contextual_children, profile_memory=profile_memory,
        )
        return SSPIEncodedObservation(
            scene_state=scene_state, token_memory=token_memory, token_mask=token_mask,
            own_card_memory=own_card_memory, group_memory=group_memory,
            child_memory=child_memory, contextual_child_memory=contextual_children,
            spatial_memory=spatial_memory, spatial_summary=spatial_summary,
            scalar_summary=scalar_summary, hand_summary=hand_summary,
            event_summary=event_summary, previous_action_summary=previous_action_summary,
            candidate_memory=candidate_memory, candidate_summary=candidate_summary,
            decision_context=decision_context, tower_value_context=self.tower_value_encoder(batch.towers),
        )

    def _clear_static_memory_cache(self):
        # Plain attributes: never parameters, buffers, or checkpoint contents.
        self._static_memory_key = None
        self._static_memory_inputs = ()
        self._static_memory_cache = None

    def _inference_static_memories(self):
        modules = (self.effect_encoder, self.mechanic_profile_encoder)
        if (not self.cache_static_inference or self.training or torch.is_grad_enabled()
                or not torch.is_inference_mode_enabled()
                or any(child.training for module in modules for child in module.modules())):
            self._clear_static_memory_cache()
            return None
        tensors = tuple(tensor for module in modules
            for tensor in (*module.parameters(), *module.buffers()))
        if any(torch.is_autocast_enabled(device) for device in {tensor.device.type for tensor in tensors}):
            self._clear_static_memory_cache()
            return None
        # Inference tensors have no mutation counter. Recompute rather than
        # relying on a stale cache if a caller creates model inputs that way.
        if any(torch.is_inference(tensor) for tensor in tensors):
            self._clear_static_memory_cache()
            return None
        key = (tuple(id(child) for module in modules for child in module.modules()),
            self.mechanic_profile_encoder.profile_count, tuple(
            (id(tensor), tensor._version, tensor.device, tensor.dtype, tuple(tensor.shape))
            for tensor in tensors))
        if key != self._static_memory_key:
            effects = self.effect_encoder.all_effects()
            profiles = self.mechanic_profile_encoder(effects)
            self._static_memory_cache = (effects, profiles)
            # Retain the input objects so object IDs cannot be recycled while
            # the corresponding cache is still live.
            self._static_memory_inputs = (tuple(child for module in modules for child in module.modules()), tensors)
            self._static_memory_key = key
        return self._static_memory_cache

    def train(self, mode=True):
        self._clear_static_memory_cache()
        return super().train(mode)

    def _apply(self, fn, recurse=True):
        self._clear_static_memory_cache()
        return super()._apply(fn, recurse=recurse)

    def _load_from_state_dict(self, *args, **kwargs):
        self._clear_static_memory_cache()
        return super()._load_from_state_dict(*args, **kwargs)


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
