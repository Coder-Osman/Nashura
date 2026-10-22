"""Frozen dimensions for the sole :class:`UniversalCardPolicyV4` baseline."""

from __future__ import annotations

from dataclasses import dataclass, fields, replace

from ..card_features import CARD_MECHANIC_FEATURE_NAMES, CARD_STATIC_FEATURE_NAMES


UNIVERSAL_POLICY_VERSION = "universal-card-policy.v4"
UNIVERSAL_OBSERVATION_VERSION = "universal-semantic-observation.v4"
UNIVERSAL_ACTION_VERSION = "universal-candidate-action.v4"
UNIVERSAL_CARD_CATALOG_VERSION = "universal-card-catalog.v3"


CARD_RUNTIME_FEATURE_NAMES = (
    "in_hand",
    "is_next_card",
    "revealed",
    "evolution_state_known",
    "evolution_ready",
    "evolution_cycle_remaining_over_4",
    "public_availability_over_3",
    "public_cycle_distance_over_4",
    "public_initial_state_known",
    "ability_state_known",
    "ability_available",
    "ability_cooldown_remaining_over_30000ms",
    "ability_charges_over_4",
    "ability_casting",
    "ability_active",
    "unknown_opponent_card_fraction",
    "public_in_hand_estimate",
    "public_next_draw_estimate",
)

TOWER_FEATURE_NAMES = (
    "hitpoints_ratio",
    "shield_over_max_hitpoints",
    "active",
    "has_visible_target",
    "king_activated",
    "hitpoints_over_5000",
    "max_hitpoints_over_5000",
    "attack_cooldown_remaining_over_5000ms",
    "attack_phase_remaining_over_5000ms",
    "attack_windup",
    "attack_release_or_channel",
    "attack_cooldown",
    "attack_interrupted",
    "attack_sequence_index_over_10",
    "attack_damage_multiplier_over_5",
    "dagger_charge_ratio",
    "dagger_recharge_progress",
    "chef_start_delay_remaining_ratio",
    "chef_cooking_progress",
    "chef_surviving_side_towers_over_2",
    "chef_throw_pending",
)

GROUP_FEATURE_NAMES = (
    "member_count_over_16",
    "dropped_child_count_over_16",
    "summed_hitpoints_over_10000",
    "summed_shield_over_10000",
    "mean_hitpoints_ratio",
    "min_hitpoints_ratio",
    "max_hitpoints_ratio",
    "x_span_tiles",
    "y_span_tiles",
    "has_ability_source",
    "has_visible_target",
    "mean_velocity_x_over_10",
    "mean_velocity_y_over_10",
    "mean_speed_over_10",
    "velocity_dispersion_over_100",
    "x_variance_over_board_width_squared",
    "y_variance_over_board_height_squared",
    "xy_covariance_over_board_area",
    "projectile_count_over_16",
    "known_projectile_damage_sum_over_5000",
    "projectile_damage_coverage",
    "has_causal_parent",
)

CHILD_FEATURE_NAMES = (
    "hitpoints_ratio",
    "hitpoints_over_10000",
    "max_hitpoints_over_10000",
    "shield_over_10000",
    "age_over_60000ms",
    "has_visible_target",
    "is_ability_source",
    "x_over_board_width",
    "y_over_board_height",
    "velocity_x_over_10",
    "velocity_y_over_10",
    "attack_cooldown_remaining_over_5000ms",
    "attack_phase_remaining_over_5000ms",
    "projectile_damage_over_5000",
    "projectile_radius_over_5_tiles",
    "projectile_homing",
    "attack_windup",
    "attack_release_or_channel",
    "attack_cooldown",
    "attack_charging",
    "attack_interrupted",
    "attack_sequence_index_over_10",
    "attack_charge_stage_over_10",
    "attack_charge_elapsed_over_5000ms",
    "attack_damage_multiplier_over_5",
    "attack_locked",
    "deployment_in_progress",
    "deployment_remaining_over_5000ms",
    "movement_effective_speed_over_1000",
    "classic_charge_ready",
    "visibility_hidden_or_burrowed",
    "visibility_transition_remaining_over_5000ms",
    "projectile_in_flight",
    "projectile_damage_known",
    "projectile_radius_known",
    "classic_charge_progress_over_10000",
    "visibility_targetable",
    "visibility_targetable_known",
    "visibility_area_damage_eligible",
    "visibility_area_damage_eligible_known",
    "projectile_target_x_over_board_width",
    "projectile_target_y_over_board_height",
    "projectile_target_position_known",
    "projectile_drag_back_active",
    "projectile_drag_stage_known",
    "extra_spawn_accumulator_ratio",
    "extra_spawn_accumulator_known",
    "capture_runtime_known",
    "capture_acquired_delay",
    "capture_grab_pause",
    "capture_dragging",
    "capture_contained",
    "capture_release_pending",
    "capture_phase_budget_remaining_over_5000ms",
    "capture_action_cycle_progress",
    "capture_cooldown_remaining_ratio",
    "threshold_relocation_runtime_known",
    "relocation_waiting_threshold",
    "relocation_active",
    "relocation_exhausted",
    "relocation_burrowed",
    "relocation_remaining_ratio",
    "relocation_index_ratio",
    "relocation_threshold_percent",
    "attack_sequence_progress_ratio",
    "attack_sequence_progress_known",
    "attack_sequence_decay_remaining_ratio",
    "attack_sequence_decay_known",
    "periodic_attack_modifier_present",
    "periodic_attack_modifier_progress_ratio",
    "periodic_attack_modifier_source_death_linger",
    "periodic_attack_modifier_linger_remaining_ratio",
)

EVENT_FEATURE_NAMES = (
    "latest_age_over_decision_ticks",
    "amount_sum_over_5000",
    "has_source",
    "has_target",
    "event_count_over_16",
    "max_amount_over_5000",
    "mean_x_over_board_width",
    "mean_y_over_board_height",
    "position_coverage",
    "amount_coverage",
    "span_over_decision_ticks",
    "source_form_known",
)

MATCH_SCALAR_NAMES = (
    "remaining_time_over_300000ms",
    "elixir_multiplier_over_3",
    "overtime_or_sudden_death",
    "tiebreak",
    "own_elixir_over_10",
    "own_crowns_over_3",
    "enemy_crowns_over_3",
    "visible_entity_count_over_capacity",
    "current_event_group_count_over_capacity",
    "group_overflow_over_capacity",
    "child_overflow_over_capacity",
    "producer_entity_overflow_over_capacity",
    "event_overflow_over_capacity",
    "candidate_count_over_capacity",
    "reserved_elixir_over_10",
    "enemy_elixir_upper_over_10",
    "enemy_elixir_uncertainty_over_10",
    "actor_is_true_red",
)

CANDIDATE_RUNTIME_FEATURE_NAMES = ("effective_cost_over_10", "legal_cell_fraction")

SHADOW_FEATURE_NAMES = (
    "remaining_elixir_over_10",
    "step_over_max_micro_actions",
    "used_candidate_fraction",
    "legal_candidate_fraction",
    "planned_action_count_over_max",
    "previous_delay_offset_over_200ms",
    "used_hand_fraction",
    "has_legal_candidate",
)

SPATIAL_PAIR_FEATURE_NAMES = (
    "dx_over_board_width",
    "dy_over_board_height",
    "absolute_dx_over_board_width",
    "absolute_dy_over_board_height",
    "normalized_distance",
    "x_overlap_over_board_width",
    "y_overlap_over_board_height",
    "spatial_pair_valid",
)

ABILITY_FEATURE_NAMES = (
    "elixir_cost_over_10",
    "cooldown_over_30000ms",
    "cast_time_over_5000ms",
    "charges_over_4",
    "effect_duration_over_10000ms",
    "spawns_form",
    "creates_area",
    "applies_buff",
    "invokes_native_action_group",
    "effect_count_over_4",
)


@dataclass(frozen=True, slots=True)
class ModelConfigV4:
    """Dimensions and capacities for the first universal-card policy."""

    # Board and timing.  Every policy invocation represents one 250 ms turn.
    board_width: int = 18
    board_height: int = 32
    decision_ticks: int = 5
    max_micro_actions: int = 2
    delay_offset_ms: tuple[int, ...] = (0, 50, 100, 150, 200)

    # Observation capacities.
    max_own_cards: int = 8
    max_opponent_cards: int = 9
    max_towers: int = 6
    max_battle_groups: int = 48
    max_children_total: int = 160
    max_recent_events: int = 32
    # Four hand cards plus the two native Ability controller slots.
    max_action_candidates: int = 6

    # Raw structured feature widths.
    card_static_dim: int = len(CARD_STATIC_FEATURE_NAMES)
    card_mechanic_dim: int = len(CARD_MECHANIC_FEATURE_NAMES)
    card_runtime_feature_dim: int = len(CARD_RUNTIME_FEATURE_NAMES)
    tower_feature_dim: int = len(TOWER_FEATURE_NAMES)
    child_feature_dim: int = len(CHILD_FEATURE_NAMES)
    group_feature_dim: int = len(GROUP_FEATURE_NAMES)
    event_feature_dim: int = len(EVENT_FEATURE_NAMES)
    generic_scalar_dim: int = len(MATCH_SCALAR_NAMES)
    candidate_runtime_feature_dim: int = len(CANDIDATE_RUNTIME_FEATURE_NAMES)
    ability_feature_dim: int = len(ABILITY_FEATURE_NAMES)

    # Vocabulary capacities.  Card vocabulary size comes from CardCatalogV1.
    form_type_count: int = 4
    owner_type_count: int = 3
    tower_type_count: int = 3
    tower_troop_type_count: int = 5
    child_archetype_count: int = 429
    child_type_count: int = 8
    group_type_count: int = 4
    event_type_count: int = 39
    ability_vocab_size: int = 25
    relation_type_count: int = 14

    # Card, entity, and relation encoding.
    card_semantic_dim: int = 128
    effect_dim: int = 128
    mechanic_profile_dim: int = 128
    child_dim: int = 128
    group_dim: int = 256
    token_dim: int = 256
    local_pool_slots: int = 4
    transformer_layers: int = 4
    attention_heads: int = 8
    transformer_ffn_dim: int = 1024
    spatial_pair_dim: int = len(SPATIAL_PAIR_FEATURE_NAMES)

    # Spatial encoding.  Tensor layout is always [B,C,H,W] = [B,C,32,18].
    explicit_spatial_channels: int = 14
    learned_scatter_channels: int = 32
    spatial_hidden_channels: int = 64
    spatial_res_blocks: int = 3
    spatial_summary_dim: int = 128

    # Other encoders.
    scalar_summary_dim: int = 128
    event_summary_dim: int = 128
    previous_action_dim: int = 128
    candidate_dim: int = 256
    candidate_summary_dim: int = 128

    # The only persistent neural recurrent core.
    lstm_input_dim: int = 512
    lstm_hidden_dim: int = 512

    # Decoder widths and explicit initial priors.
    decoder_hidden_dim: int = 512
    shadow_feature_dim: int = len(SHADOW_FEATURE_NAMES)
    initial_act_probability: float = 0.15
    initial_second_probability: float = 0.25

    # Observation contract.  When True the model consumes only the current world
    # snapshot: the accumulated public-event delta and the two scalars that count
    # it are neutralized, so a per-tick step and a batched step see the same
    # input at the same tick.  Off by default so existing runs and checkpoints
    # keep the original behaviour byte for byte.
    snapshot_only: bool = False

    # Width of the concatenation that feeds the recurrent core.  Defaults to the
    # frozen production sum; scaling presets raise it together with the encoder
    # widths, and __post_init__ still enforces that the sum matches.
    core_input_width: int = 896

    def __post_init__(self) -> None:
        invalid = sorted(item.name for item in fields(self) if item.type == "int" and getattr(self, item.name) <= 0)
        if invalid:
            raise ValueError(f"V4 dimensions must be positive: {invalid}")
        expected_core_width = (
            self.token_dim
            + self.spatial_summary_dim
            + self.scalar_summary_dim
            + self.event_summary_dim
            + self.previous_action_dim
            + self.candidate_summary_dim
        )
        if self.core_input_width != expected_core_width:
            raise ValueError(
                f"V4 core concatenation width {self.core_input_width} does not match the encoder sum "
                f"{expected_core_width}"
            )
        if (self.board_width, self.board_height) != (18, 32):
            raise ValueError("V4 arena grid is frozen at width=18, height=32")
        if self.decision_ticks != 5:
            raise ValueError("V4 policy decisions are frozen at five native ticks")
        if self.max_micro_actions != 2:
            raise ValueError("V4 actions contain at most two micro-actions")
        if self.max_own_cards != 8 or self.max_opponent_cards != 9:
            raise ValueError("V4 card sets are frozen at 8 own and 9 opponent rows")
        if self.delay_offset_ms != (0, 50, 100, 150, 200):
            raise ValueError("V4 delay offsets are frozen at 0..200 ms in 50 ms steps")
        if self.token_dim != self.group_dim:
            raise ValueError("group_dim and token_dim must match")
        if self.token_dim % self.attention_heads:
            raise ValueError("token_dim must be divisible by attention_heads")
        frozen_widths = {
            "card_static_dim": len(CARD_STATIC_FEATURE_NAMES),
            "card_mechanic_dim": len(CARD_MECHANIC_FEATURE_NAMES),
            "card_runtime_feature_dim": len(CARD_RUNTIME_FEATURE_NAMES),
            "tower_feature_dim": len(TOWER_FEATURE_NAMES),
            "child_feature_dim": len(CHILD_FEATURE_NAMES),
            "group_feature_dim": len(GROUP_FEATURE_NAMES),
            "event_feature_dim": len(EVENT_FEATURE_NAMES),
            "generic_scalar_dim": len(MATCH_SCALAR_NAMES),
            "candidate_runtime_feature_dim": len(CANDIDATE_RUNTIME_FEATURE_NAMES),
            "ability_feature_dim": len(ABILITY_FEATURE_NAMES),
            "shadow_feature_dim": len(SHADOW_FEATURE_NAMES),
            "spatial_pair_dim": len(SPATIAL_PAIR_FEATURE_NAMES),
        }
        drifted = sorted(name for name, width in frozen_widths.items() if getattr(self, name) != width)
        if drifted:
            raise ValueError(f"V4 named feature widths drifted: {drifted}")
        for name, probability in (
            ("initial_act_probability", self.initial_act_probability),
            ("initial_second_probability", self.initial_second_probability),
        ):
            if not 0.0 < probability < 1.0:
                raise ValueError(f"{name} must be in (0, 1)")

# Internal widths that carry capacity without touching the frozen input contract.
_SCALABLE_WIDTHS = (
    "card_semantic_dim",
    "effect_dim",
    "mechanic_profile_dim",
    "child_dim",
    "spatial_summary_dim",
    "scalar_summary_dim",
    "event_summary_dim",
    "previous_action_dim",
    "candidate_summary_dim",
    "token_dim",
    "group_dim",
    "candidate_dim",
    "learned_scatter_channels",
    "spatial_hidden_channels",
    "lstm_input_dim",
    "lstm_hidden_dim",
    "decoder_hidden_dim",
    "transformer_ffn_dim",
)


def scaled_model_config_v4(factor: float, *, base: "ModelConfigV4 | None" = None) -> ModelConfigV4:
    """Widen the internals by ``factor`` while keeping the input contract fixed."""

    if factor <= 0.0:
        raise ValueError("model scale factor must be positive")
    source = base or ModelConfigV4()
    updates: dict[str, int] = {}
    for name in _SCALABLE_WIDTHS:
        value = getattr(source, name)
        updates[name] = max(16, int(round(value * factor / 16.0)) * 16)
    # Attention heads must divide every embed width the model attends over.
    heads = max(1, int(round(source.attention_heads * factor)))
    while heads > 1 and (
        updates["token_dim"] % heads or updates["decoder_hidden_dim"] % heads
    ):
        heads -= 1
    updates["attention_heads"] = heads
    updates["spatial_res_blocks"] = max(
        source.spatial_res_blocks, int(round(source.spatial_res_blocks * factor))
    )
    # The frozen validation runs inside the dataclass, so the raised core width
    # has to travel in the same update.
    updates["core_input_width"] = (
        updates["token_dim"]
        + updates["spatial_summary_dim"]
        + updates["scalar_summary_dim"]
        + updates["event_summary_dim"]
        + updates["previous_action_dim"]
        + updates["candidate_summary_dim"]
    )
    return replace(source, **updates)
