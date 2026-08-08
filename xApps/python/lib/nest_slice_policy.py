from collections.abc import Mapping
from dataclasses import dataclass
import math
from typing import Dict, Optional, Tuple


@dataclass(frozen=True)
class SlicePolicySliceConfig:
    """Configuration of one slice controlled by the external policy."""

    sst: int
    initial_quota: int
    throughput_target_mbps: float


@dataclass(frozen=True)
class SlicePolicyConfig:
    """Configuration shared by every decision made for one E2 node."""

    decision_interval_seconds: float
    quota_step: int
    satisfaction_hysteresis: float
    minimum_quota: int
    maximum_quota: int
    slices: Tuple[SlicePolicySliceConfig, ...]


@dataclass(frozen=True)
class SlicePolicyResult:
    """Metrics and quota state calculated for one slice."""

    sst: int
    offered_mbps: Optional[float]
    throughput_mbps: float
    effective_target_mbps: float
    satisfaction: float
    quota_before: int
    quota_after: int


@dataclass(frozen=True)
class SlicePolicyDecision:
    """One immutable hold or quota-transfer proposal."""

    decision_number: int
    window_start: float
    window_end: float
    duration_seconds: float
    slices: Tuple[SlicePolicyResult, ...]
    donor_sst: Optional[int]
    receiver_sst: Optional[int]
    transferred_quota: int

    @property
    def changed(self):
        return self.transferred_quota > 0


class NestSlicePolicy:
    """Pure per-node implementation of the NEST slice quota policy."""

    def __init__(self, config):
        if not isinstance(config, SlicePolicyConfig):
            raise ValueError("policy configuration must be a SlicePolicyConfig")

        if isinstance(config.decision_interval_seconds, bool) or not isinstance(config.decision_interval_seconds, (int, float)) or not math.isfinite(config.decision_interval_seconds) or config.decision_interval_seconds <= 0:
            raise ValueError("decision interval must be finite and greater than zero")

        if isinstance(config.quota_step, bool) or not isinstance(config.quota_step, int) or not 1 <= config.quota_step <= 100:
            raise ValueError("quota step must be an integer in the range 1..100")

        if isinstance(config.satisfaction_hysteresis, bool) or not isinstance(config.satisfaction_hysteresis, (int, float)) or not math.isfinite(config.satisfaction_hysteresis) or not 0 <= config.satisfaction_hysteresis < 1:
            raise ValueError("satisfaction hysteresis must be finite and in the range [0, 1)")

        if isinstance(config.minimum_quota, bool) or not isinstance(config.minimum_quota, int) or not 0 <= config.minimum_quota <= 100:
            raise ValueError("minimum quota must be an integer in the range 0..100")

        if isinstance(config.maximum_quota, bool) or not isinstance(config.maximum_quota, int) or not config.minimum_quota <= config.maximum_quota <= 100:
            raise ValueError("quota limits must satisfy 0 <= minimum <= maximum <= 100")

        if not isinstance(config.slices, (list, tuple)) or len(config.slices) < 2:
            raise ValueError("the policy requires at least two slices")

        configured_ssts = set()
        initial_quota_sum = 0
        slice_configs = []

        for slice_config in config.slices:
            if not isinstance(slice_config, SlicePolicySliceConfig):
                raise ValueError("each slice configuration must be a SlicePolicySliceConfig")

            if isinstance(slice_config.sst, bool) or not isinstance(slice_config.sst, int) or not 1 <= slice_config.sst <= 255:
                raise ValueError("slice SST must be an integer in the range 1..255")

            if slice_config.sst in configured_ssts:
                raise ValueError(f"the policy contains duplicate SST {slice_config.sst}")

            if isinstance(slice_config.initial_quota, bool) or not isinstance(slice_config.initial_quota, int) or not config.minimum_quota <= slice_config.initial_quota <= config.maximum_quota:
                raise ValueError(f"initial quota for SST {slice_config.sst} is outside the configured limits")

            if isinstance(slice_config.throughput_target_mbps, bool) or not isinstance(slice_config.throughput_target_mbps, (int, float)) or not math.isfinite(slice_config.throughput_target_mbps) or slice_config.throughput_target_mbps <= 0:
                raise ValueError(f"throughput target for SST {slice_config.sst} must be finite and greater than zero")

            configured_ssts.add(slice_config.sst)
            initial_quota_sum += slice_config.initial_quota
            slice_configs.append(slice_config)

        if initial_quota_sum != 100:
            raise ValueError("initial slice quotas must sum exactly 100")

        self._config = config
        self._slice_configs = tuple(slice_configs)
        self._slice_order = tuple(slice_config.sst for slice_config in self._slice_configs)
        self._slice_by_sst = {slice_config.sst: slice_config for slice_config in self._slice_configs}
        self._current_quotas = {slice_config.sst: slice_config.initial_quota for slice_config in self._slice_configs}
        self._decision_number = 0
        self._reset_observation_period()

    @property
    def decision_number(self):
        return self._decision_number

    def current_quotas(self):
        """Return a copy of the currently committed quota distribution."""
        return dict(self._current_quotas)

    def observe_window(self, window_start, window_end, throughput_mbps_by_sst, offered_mbps_by_sst=None):
        """Accumulate one complete window and return a decision when due."""
        window_start = self._validate_finite_number("window start", window_start)
        window_end = self._validate_finite_number("window end", window_end)

        if window_end <= window_start:
            raise ValueError("window end must be greater than window start")

        throughput = self._validate_slice_measurements("throughput", throughput_mbps_by_sst)
        offered = None

        if offered_mbps_by_sst is not None:
            offered = self._validate_slice_measurements("offered throughput", offered_mbps_by_sst)

        window_duration = window_end - window_start
        offered_available = offered is not None

        if self._accumulated_duration == 0:
            self._observation_start = window_start
            self._offered_available = offered_available
        else:
            expected_window_start = self._observation_start + self._accumulated_duration

            if abs(window_start - expected_window_start) > 1e-6:
                raise ValueError("the policy received non-consecutive observation windows")

            if offered_available != self._offered_available:
                raise ValueError("offered-throughput availability changed within one decision period")

        for sst in self._slice_order:
            self._accumulated_throughput_mbits[sst] += throughput[sst] * window_duration

            if offered is not None:
                self._accumulated_offered_mbits[sst] += offered[sst] * window_duration

        self._accumulated_duration += window_duration

        if self._accumulated_duration + 1e-9 < self._config.decision_interval_seconds:
            return None

        decision = self._build_decision(window_end)
        self._reset_observation_period()
        return decision

    def commit_decision(self, decision):
        """Commit the proposed quota distribution after control acceptance."""
        if not isinstance(decision, SlicePolicyDecision):
            raise ValueError("decision must be a SlicePolicyDecision")

        if decision.decision_number != self._decision_number:
            raise ValueError("decision number does not match the latest policy decision")

        quotas_before = {slice_result.sst: slice_result.quota_before for slice_result in decision.slices}
        quotas_after = {slice_result.sst: slice_result.quota_after for slice_result in decision.slices}

        if quotas_before != self._current_quotas:
            raise ValueError("decision was calculated from a stale quota distribution")

        if set(quotas_after) != set(self._slice_order) or sum(quotas_after.values()) != 100:
            raise ValueError("decision contains an invalid quota distribution")

        self._current_quotas = quotas_after

    def _build_decision(self, window_end):
        if self._observation_start is None or self._accumulated_duration <= 0:
            raise RuntimeError("cannot build a decision without accumulated observations")

        self._decision_number += 1
        throughput = {}
        offered = {}
        effective_target = {}
        satisfaction = {}

        for sst in self._slice_order:
            slice_config = self._slice_by_sst[sst]
            throughput[sst] = self._accumulated_throughput_mbits[sst] / self._accumulated_duration

            if self._offered_available:
                offered[sst] = self._accumulated_offered_mbits[sst] / self._accumulated_duration
                effective_target[sst] = min(slice_config.throughput_target_mbps, offered[sst])
            else:
                offered[sst] = None
                effective_target[sst] = slice_config.throughput_target_mbps

            satisfaction[sst] = throughput[sst] / effective_target[sst] if effective_target[sst] > 1e-9 else 1.0

        receiver_sst = min(self._slice_order, key=lambda sst: satisfaction[sst])
        donor_sst = max(self._slice_order, key=lambda sst: satisfaction[sst])
        satisfaction_gap = satisfaction[donor_sst] - satisfaction[receiver_sst]
        quotas_after = dict(self._current_quotas)
        transferred_quota = 0

        receiver_needs_quota = satisfaction[receiver_sst] < 1.0
        can_receive = self._current_quotas[receiver_sst] < self._config.maximum_quota
        can_donate = self._current_quotas[donor_sst] > self._config.minimum_quota

        if receiver_sst != donor_sst and receiver_needs_quota and satisfaction_gap > self._config.satisfaction_hysteresis and can_receive and can_donate:
            donor_capacity = self._current_quotas[donor_sst] - self._config.minimum_quota
            receiver_capacity = self._config.maximum_quota - self._current_quotas[receiver_sst]
            transferred_quota = min(self._config.quota_step, donor_capacity, receiver_capacity)
            quotas_after[donor_sst] -= transferred_quota
            quotas_after[receiver_sst] += transferred_quota

        results = tuple(
            SlicePolicyResult(
                sst=sst,
                offered_mbps=offered[sst],
                throughput_mbps=throughput[sst],
                effective_target_mbps=effective_target[sst],
                satisfaction=satisfaction[sst],
                quota_before=self._current_quotas[sst],
                quota_after=quotas_after[sst],
            )
            for sst in self._slice_order
        )

        return SlicePolicyDecision(
            decision_number=self._decision_number,
            window_start=self._observation_start,
            window_end=window_end,
            duration_seconds=self._accumulated_duration,
            slices=results,
            donor_sst=donor_sst if transferred_quota > 0 else None,
            receiver_sst=receiver_sst if transferred_quota > 0 else None,
            transferred_quota=transferred_quota,
        )

    def _validate_slice_measurements(self, name, measurements):
        if not isinstance(measurements, Mapping):
            raise ValueError(f"{name} measurements must be a mapping indexed by SST")

        if set(measurements) != set(self._slice_order):
            raise ValueError(f"{name} measurements must contain exactly the configured SSTs")

        validated = {}

        for sst in self._slice_order:
            validated[sst] = self._validate_finite_number(f"{name} for SST {sst}", measurements[sst])

            if validated[sst] < 0:
                raise ValueError(f"{name} for SST {sst} cannot be negative")

        return validated

    @staticmethod
    def _validate_finite_number(name, value):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"{name} must be a finite number")

        return float(value)

    def _reset_observation_period(self):
        self._observation_start = None
        self._accumulated_duration = 0.0
        self._offered_available = None
        self._accumulated_throughput_mbits = {sst: 0.0 for sst in self._slice_order}
        self._accumulated_offered_mbits = {sst: 0.0 for sst in self._slice_order}