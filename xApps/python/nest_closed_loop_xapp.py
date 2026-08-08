#!/usr/bin/env python3

import json
import math
import threading
import time
import argparse
import signal
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional, Tuple

from lib.nest_slice_policy import NestSlicePolicy
from lib.nest_slice_policy import SlicePolicyConfig
from lib.nest_slice_policy import SlicePolicyDecision
from lib.nest_slice_policy import SlicePolicySliceConfig
from lib.xAppBase import xAppBase
from nest_kpm_collector_xapp import KpmUeSample
from nest_kpm_collector_xapp import NestKpmCollectorXapp
from nest_kpm_collector_xapp import PROFILES
from nest_kpm_collector_xapp import parse_ue_ids


def load_policy_config(path):
    """Load and validate a NEST slice policy from JSON."""
    policy_path = Path(path)

    try:
        with policy_path.open("r", encoding="utf-8") as policy_file:
            document = json.load(policy_file)
    except OSError as error:
        raise ValueError(f"could not read policy configuration {policy_path}: {error}") from error
    except json.JSONDecodeError as error:
        raise ValueError(f"policy configuration is not valid JSON: {error}") from error

    if not isinstance(document, dict):
        raise ValueError("policy configuration root must be an object")

    controller = document.get("localSliceController", document)

    if not isinstance(controller, dict):
        raise ValueError("localSliceController must be an object")

    required_fields = (
        "decisionInterval",
        "quotaStep",
        "satisfactionHysteresis",
        "minimumQuota",
        "maximumQuota",
        "slices",
    )
    missing_fields = [field for field in required_fields if field not in controller]

    if missing_fields:
        raise ValueError(f"policy configuration is missing required fields: {', '.join(missing_fields)}")

    raw_slices = controller["slices"]

    if not isinstance(raw_slices, list):
        raise ValueError("policy slices must be an array")

    slices = []

    for raw_slice in raw_slices:
        if not isinstance(raw_slice, dict):
            raise ValueError("each policy slice must be an object")

        required_slice_fields = (
            "sliceId",
            "initialQuota",
            "throughputTargetMbps",
        )
        missing_slice_fields = [field for field in required_slice_fields if field not in raw_slice]

        if missing_slice_fields:
            raise ValueError(f"policy slice is missing required fields: {', '.join(missing_slice_fields)}")

        slices.append(
            SlicePolicySliceConfig(
                sst=raw_slice["sliceId"],
                initial_quota=raw_slice["initialQuota"],
                throughput_target_mbps=raw_slice["throughputTargetMbps"],
            )
        )

    config = SlicePolicyConfig(
        decision_interval_seconds=controller["decisionInterval"],
        quota_step=controller["quotaStep"],
        satisfaction_hysteresis=controller["satisfactionHysteresis"],
        minimum_quota=controller["minimumQuota"],
        maximum_quota=controller["maximumQuota"],
        slices=tuple(slices),
    )

    NestSlicePolicy(config)
    return config

@dataclass(frozen=True)
class AggregatedKpmWindow:
    """One complete KPM window aggregated by SST."""

    e2_node_id: str
    window_start: float
    window_end: float
    throughput_mbps_by_sst: Dict[int, float]


def parse_collect_start_time(value):
    """Convert one KPM collection-start timestamp into Unix seconds."""
    if not isinstance(value, str) or not value:
        raise ValueError("KPM collection start time must be a non-empty string")

    normalized_value = value[:-1] + "+00:00" if value.endswith("Z") else value

    try:
        parsed = datetime.fromisoformat(normalized_value)
    except ValueError as error:
        raise ValueError(f"invalid KPM collection start time: {value}") from error

    if parsed.tzinfo is None:
        return (parsed - datetime(1970, 1, 1)).total_seconds()

    return parsed.astimezone(timezone.utc).timestamp()


def aggregate_kpm_samples(samples, expected_ssts):
    """Aggregate all per-UE samples from one indication by SST."""
    if not isinstance(samples, (list, tuple)) or not samples:
        raise ValueError("one non-empty KPM sample collection is required")

    if not isinstance(expected_ssts, (list, tuple)) or not expected_ssts:
        raise ValueError("at least one expected SST is required")

    expected_ssts = tuple(expected_ssts)

    if len(set(expected_ssts)) != len(expected_ssts):
        raise ValueError("expected SSTs must be unique")

    first_sample = samples[0]

    if not isinstance(first_sample, KpmUeSample):
        raise ValueError("every KPM sample must be a KpmUeSample")

    e2_node_id = first_sample.e2_node_id
    collect_start_time = first_sample.collect_start_time
    granularity_period_ms = first_sample.granularity_period_ms
    throughput_mbps_by_sst = {sst: 0.0 for sst in expected_ssts}
    observed_ssts = set()
    observed_ues = set()

    if not isinstance(granularity_period_ms, int) or isinstance(granularity_period_ms, bool) or granularity_period_ms <= 0:
        raise ValueError("KPM granularity period must be a positive integer")

    for sample in samples:
        if not isinstance(sample, KpmUeSample):
            raise ValueError("every KPM sample must be a KpmUeSample")

        if sample.e2_node_id != e2_node_id:
            raise ValueError("one KPM window cannot contain more than one E2 node")

        if sample.collect_start_time != collect_start_time:
            raise ValueError("one KPM window cannot contain different collection-start timestamps")

        if sample.granularity_period_ms != granularity_period_ms:
            raise ValueError("one KPM window cannot contain different granularity periods")

        if isinstance(sample.sst, bool) or not isinstance(sample.sst, int) or sample.sst not in throughput_mbps_by_sst:
            raise ValueError(f"UE {sample.ue_id} contains an unexpected or missing SST")

        if sample.ue_id in observed_ues:
            raise ValueError(f"KPM window contains duplicate UE {sample.ue_id}")

        if isinstance(sample.kpm_throughput_dl_kbps, bool) or not isinstance(sample.kpm_throughput_dl_kbps, (int, float)) or not math.isfinite(sample.kpm_throughput_dl_kbps) or sample.kpm_throughput_dl_kbps < 0:
            raise ValueError(f"UE {sample.ue_id} contains invalid KPM throughput")

        observed_ues.add(sample.ue_id)
        observed_ssts.add(sample.sst)
        throughput_mbps_by_sst[sample.sst] += sample.kpm_throughput_dl_kbps / 1000.0

    if observed_ssts != set(expected_ssts):
        raise ValueError("KPM window does not contain every configured SST")

    window_start = parse_collect_start_time(collect_start_time)
    window_end = window_start + granularity_period_ms / 1000.0

    return AggregatedKpmWindow(
        e2_node_id=e2_node_id,
        window_start=window_start,
        window_end=window_end,
        throughput_mbps_by_sst=throughput_mbps_by_sst,
    )


@dataclass(frozen=True)
class PendingControl:
    """One control request awaiting ACK or Failure."""

    kind: str
    sent_at_unix_ns: int
    quotas: Tuple[Tuple[int, int], ...]
    decision: Optional[SlicePolicyDecision]


class NestClosedLoopXapp(NestKpmCollectorXapp):
    """Join normalized KPM telemetry, slice policy and E2SM-RC control."""

    def __init__(self, config, http_server_port, rmr_port, profile, policy_config, plmn, anchor_ue_id, sd=None, rc_ran_function_id=300):
        super().__init__(config, http_server_port, rmr_port, profile)

        if profile.sst_source != "kpm":
            raise ValueError("closed-loop control requires KPM-provided SST")

        if not isinstance(plmn, str) or len(plmn) not in (5, 6) or not plmn.isdigit():
            raise ValueError("PLMN must contain five or six decimal digits")

        if isinstance(anchor_ue_id, bool) or not isinstance(anchor_ue_id, int) or not 0 <= anchor_ue_id <= 0xFFFFFFFF:
            raise ValueError("anchor UE ID must be an integer in the range 0..4294967295")

        if sd is not None and (isinstance(sd, bool) or not isinstance(sd, int) or not 0 <= sd <= 0xFFFFFF):
            raise ValueError("SD must be absent or an integer in the range 0..16777215")

        if isinstance(rc_ran_function_id, bool) or not isinstance(rc_ran_function_id, int) or not 0 <= rc_ran_function_id <= 4095:
            raise ValueError("RC RAN Function ID must be an integer in the range 0..4095")

        NestSlicePolicy(policy_config)

        self.policy_config = policy_config
        self.plmn = plmn
        self.anchor_ue_id = anchor_ue_id
        self.sd = sd
        self.rc_ran_function_id = rc_ran_function_id
        self.policies = {}
        self.pending_controls = {}
        self.active_nodes = set()
        self.state_lock = threading.RLock()

        self.e2sm_rc.set_ran_func_id(rc_ran_function_id)
        self.set_control_response_callback(self.handle_control_response)

    def _log_event(self, event, **fields):
        record = {
            "event": event,
            "timestampUnixNs": time.time_ns(),
            **fields,
        }
        print("[NEST CLOSED LOOP] " + json.dumps(record, sort_keys=True), flush=True)

    def _policy_for_node(self, e2_node_id):
        with self.state_lock:
            policy = self.policies.get(e2_node_id)

            if policy is None:
                policy = NestSlicePolicy(self.policy_config)
                self.policies[e2_node_id] = policy

            return policy

    def _build_rc_quotas(self, quotas):
        configured_ssts = tuple(slice_config.sst for slice_config in self.policy_config.slices)

        if set(quotas) != set(configured_ssts):
            raise ValueError("control quotas must contain exactly the configured SSTs")

        return [
            {
                "plmn": self.plmn,
                "sst": sst,
                "sd": self.sd,
                "min_prb_ratio": quotas[sst],
                "max_prb_ratio": quotas[sst],
                "dedicated_prb_ratio": 0,
            }
            for sst in configured_ssts
        ]

    def _send_control(self, e2_node_id, kind, quotas, decision=None):
        if kind not in ("initial", "decision"):
            raise ValueError("control kind must be initial or decision")

        if kind == "decision" and decision is None:
            raise ValueError("a decision control requires one policy decision")

        quota_items = tuple((slice_config.sst, quotas[slice_config.sst]) for slice_config in self.policy_config.slices)
        pending = PendingControl(
            kind=kind,
            sent_at_unix_ns=time.time_ns(),
            quotas=quota_items,
            decision=decision,
        )

        with self.state_lock:
            if e2_node_id in self.pending_controls:
                raise RuntimeError(f"E2 node {e2_node_id} already has a pending control request")

            self.pending_controls[e2_node_id] = pending

        self._log_event(
            "control-send",
            e2NodeId=e2_node_id,
            kind=kind,
            decisionNumber=decision.decision_number if decision is not None else None,
            quotas=dict(quota_items),
        )

        try:
            return self.e2sm_rc.send_control_request_style_2_action_6_by_slices(
                e2_node_id=e2_node_id,
                anchor_ue_id=self.anchor_ue_id,
                slice_quotas=self._build_rc_quotas(dict(quota_items)),
                ack_request=1,
            )
        except Exception:
            with self.state_lock:
                if self.pending_controls.get(e2_node_id) is pending:
                    self.pending_controls.pop(e2_node_id)

            self._log_event(
                "control-send-error",
                e2NodeId=e2_node_id,
                kind=kind,
                decisionNumber=decision.decision_number if decision is not None else None,
            )
            raise

    def send_initial_control(self, e2_node_id):
        """Send the configured initial quota distribution."""
        policy = self._policy_for_node(e2_node_id)
        return self._send_control(e2_node_id, "initial", policy.current_quotas())

    def handle_control_response(self, e2_node_id, message_type, payload):
        """Commit accepted decisions and discard rejected proposals."""
        received_at_unix_ns = time.time_ns()

        with self.state_lock:
            pending = self.pending_controls.get(e2_node_id)

            if pending is None:
                self._log_event(
                    "control-response-unexpected",
                    e2NodeId=e2_node_id,
                    messageType=message_type,
                    payloadBytes=len(payload),
                )
                return

            policy = self._policy_for_node(e2_node_id)

            if message_type == 12041:
                if pending.decision is not None:
                    policy.commit_decision(pending.decision)

                self.active_nodes.add(e2_node_id)
                outcome = "ack"
            elif message_type == 12042:
                if pending.kind == "initial":
                    self.active_nodes.discard(e2_node_id)

                outcome = "failure"
            else:
                raise ValueError("unsupported RIC Control response message type")

            self.pending_controls.pop(e2_node_id)

        self._log_event(
            "control-response",
            e2NodeId=e2_node_id,
            kind=pending.kind,
            outcome=outcome,
            decisionNumber=pending.decision.decision_number if pending.decision is not None else None,
            quotas=dict(pending.quotas),
            payloadBytes=len(payload),
            latencyMs=(received_at_unix_ns - pending.sent_at_unix_ns) / 1e6,
        )

    def process_kpm_samples(self, samples):
        """Aggregate one indication and execute the per-node policy."""
        expected_ssts = tuple(slice_config.sst for slice_config in self.policy_config.slices)
        window = aggregate_kpm_samples(samples, expected_ssts)
        policy = self._policy_for_node(window.e2_node_id)

        with self.state_lock:
            if window.e2_node_id not in self.active_nodes:
                self._log_event(
                    "kpm-window-ignored",
                    e2NodeId=window.e2_node_id,
                    reason="initial-control-not-acknowledged",
                    windowStart=window.window_start,
                    windowEnd=window.window_end,
                )
                return None

            if window.e2_node_id in self.pending_controls:
                self._log_event(
                    "kpm-window-ignored",
                    e2NodeId=window.e2_node_id,
                    reason="control-request-pending",
                    windowStart=window.window_start,
                    windowEnd=window.window_end,
                )
                return None

            decision = policy.observe_window(
                window.window_start,
                window.window_end,
                window.throughput_mbps_by_sst,
            )

        if decision is None:
            self._log_event(
                "policy-window-accumulated",
                e2NodeId=window.e2_node_id,
                windowStart=window.window_start,
                windowEnd=window.window_end,
                throughputMbps=window.throughput_mbps_by_sst,
            )
            return None

        self._log_event(
            "policy-decision",
            e2NodeId=window.e2_node_id,
            decision=asdict(decision),
        )

        if not decision.changed:
            return decision

        proposed_quotas = {slice_result.sst: slice_result.quota_after for slice_result in decision.slices}
        self._send_control(window.e2_node_id, "decision", proposed_quotas, decision)
        return decision

    def handle_kpm_indication(self, e2_agent_id, e2_event_instance_id, indication_header, indication_message):
        """Normalize one indication and pass it to the closed-loop policy."""
        samples = super().handle_kpm_indication(e2_agent_id, e2_event_instance_id, indication_header, indication_message)
        self.process_kpm_samples(samples)
        return samples

    @xAppBase.start_function
    def start(self, e2_node_id, ue_ids, report_period_ms, granularity_period_ms, startup_delay_seconds):
        """Send initial quotas and create the Style 5 subscription."""
        if self.profile.report_style != 5:
            raise ValueError("closed-loop control supports only KPM Style 5")

        self._log_event(
            "startup-wait",
            e2NodeId=e2_node_id,
            delaySeconds=startup_delay_seconds,
        )

        time.sleep(startup_delay_seconds)

        self._policy_for_node(e2_node_id)
        self.send_initial_control(e2_node_id)

        self._log_event(
            "subscription-create",
            e2NodeId=e2_node_id,
            ranFunctionId=self.profile.ran_function_id,
            reportStyle=self.profile.report_style,
            ueIds=ue_ids,
            measurements=self.profile.measurement_names,
            reportPeriodMs=report_period_ms,
            granularityPeriodMs=granularity_period_ms,
        )

        self.e2sm_kpm.subscribe_report_service_style_5(
            e2_node_id,
            report_period_ms,
            list(ue_ids),
            list(self.profile.measurement_names),
            granularity_period_ms,
            self.handle_kpm_indication,
        )

def parse_integer(value):
    """Parse one decimal or hexadecimal integer."""
    try:
        return int(value, 0)
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"invalid integer: {value}") from error


def parse_optional_sd(value):
    """Parse one optional three-octet SD."""
    if value.lower() in ("none", "absent"):
        return None

    sd = parse_integer(value)

    if not 0 <= sd <= 0xFFFFFF:
        raise argparse.ArgumentTypeError("SD must be absent or in the range 0..0xffffff")

    return sd


def parse_args():
    """Parse the persistent closed-loop xApp parameters."""
    parser = argparse.ArgumentParser(description="Persistent NEST KPM-driven slice control xApp")

    parser.add_argument("--profile", choices=("nori-sim",), default="nori-sim")
    parser.add_argument("--policy-config", required=True)
    parser.add_argument("--e2-node-id", required=True)
    parser.add_argument("--ue-ids", type=parse_ue_ids, required=True)
    parser.add_argument("--plmn", default="00101")
    parser.add_argument("--anchor-ue-id", type=parse_integer, default=0)
    parser.add_argument("--sd", type=parse_optional_sd, default=None)
    parser.add_argument("--rc-ran-function-id", type=parse_integer, default=300)
    parser.add_argument("--report-period-ms", type=int, default=500)
    parser.add_argument("--granularity-period-ms", type=int, default=500)
    parser.add_argument("--config", default="")
    parser.add_argument("--http-server-port", type=int, default=8093)
    parser.add_argument("--rmr-port", type=int, default=4560)
    parser.add_argument("--startup-delay-seconds", type=float, default=5.0)

    return parser.parse_args()


def main():
    """Load the policy and start the persistent closed-loop xApp."""
    args = parse_args()
    profile = PROFILES[args.profile]
    policy_config = load_policy_config(args.policy_config)

    if args.report_period_ms <= 0:
        raise ValueError("report period must be positive")

    if args.granularity_period_ms <= 0:
        raise ValueError("granularity period must be positive")

    if args.report_period_ms != args.granularity_period_ms:
        raise ValueError("the validated closed loop requires equal reporting and granularity periods")

    if args.report_period_ms / 1000.0 > policy_config.decision_interval_seconds:
        raise ValueError("KPM reporting period cannot exceed the policy decision interval")

    if args.startup_delay_seconds < 0:
        raise ValueError("startup delay cannot be negative")

    print("Selected NEST closed-loop contract:")
    print(f"  E2 node: {args.e2_node_id}")
    print(f"  UE IDs: {args.ue_ids}")
    print(f"  KPM RAN function: {profile.ran_function_id}")
    print(f"  RC RAN function: {args.rc_ran_function_id}")
    print(f"  report period: {args.report_period_ms} ms")
    print(f"  decision interval: {policy_config.decision_interval_seconds} s")
    print(f"  PLMN: {args.plmn}")
    print(f"  anchor UE ID: {args.anchor_ue_id}")
    print(f"  SD: {args.sd}")

    xapp = NestClosedLoopXapp(
        args.config,
        args.http_server_port,
        args.rmr_port,
        profile,
        policy_config,
        args.plmn,
        args.anchor_ue_id,
        args.sd,
        args.rc_ran_function_id,
    )

    signal.signal(signal.SIGQUIT, xapp.signal_handler)
    signal.signal(signal.SIGTERM, xapp.signal_handler)
    signal.signal(signal.SIGINT, xapp.signal_handler)

    xapp.start(
        args.e2_node_id,
        args.ue_ids,
        args.report_period_ms,
        args.granularity_period_ms,
        args.startup_delay_seconds,
    )


if __name__ == "__main__":
    main()