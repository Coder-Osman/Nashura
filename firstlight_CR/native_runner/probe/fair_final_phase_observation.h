#pragma once

inline thread_local unsigned int g_fair_final_phase_observation_depth = 0;

class FairFinalPhaseObservationScope {
 public:
  FairFinalPhaseObservationScope() { ++g_fair_final_phase_observation_depth; }
  ~FairFinalPhaseObservationScope() { --g_fair_final_phase_observation_depth; }
  FairFinalPhaseObservationScope(const FairFinalPhaseObservationScope &) = delete;
  FairFinalPhaseObservationScope &operator=(const FairFinalPhaseObservationScope &) = delete;
};
