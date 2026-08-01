#!/usr/bin/env python3

import argparse
import pprint
import signal
import time
from dataclasses import asdict, dataclass
from typing import Optional, Tuple

from lib.xAppBase import xAppBase

@dataclass(frozen=True)
class KpmProfile:
    """Describe the KPM capabilities expected from one E2 environment."""

    name: str
    ran_function_id: int
    report_style: int
    measurement_names: Tuple[str, ...]
    ue_identity: str
    sst_source: str
    time_scale_factor: float


@dataclass(frozen=True)
class KpmUeSample:
    """Normalized per-UE sample shared by the NEST xApps."""
    source_profile: str
    e2_node_id: str
    ric_subscription_id: str
    e2_event_instance_id: int
    ue_id: str
    sst: Optional[int]
    kpm_throughput_dl_kbps: float
    wall_clock_throughput_dl_kbps: float
    time_scale_factor: float
    collect_start_time: str
    granularity_period_ms: int


PROFILES = {
    "ocudu": KpmProfile(
        name="ocudu",
        ran_function_id=2,
        report_style=5,
        measurement_names=("DRB.UEThpDl",),
        ue_identity="gNB-DU-UEID",
        sst_source="configuration",
        time_scale_factor=4.0,
    ),
    "nori-sim": KpmProfile(
        name="nori-sim",
        ran_function_id=200,
        report_style=5,
        measurement_names=(
            "NEST.RLC.TxPduBitrateDl.UEID",
            "DRB.NetworkSlicing.SST.UEID",
        ),
        ue_identity="gNB-DU-UEID",
        sst_source="kpm",
        time_scale_factor=1.0,
    ),
}

class NestKpmCollectorXapp(xAppBase):
    """Subscribe to KPM telemetry and expose decoded reports."""

    def __init__(
        self,
        config: str,
        http_server_port: int,
        rmr_port: int,
        profile: KpmProfile,
    ):
        super().__init__(
            config,
            http_server_port,
            rmr_port,
        )

        self.profile = profile
        self.e2sm_kpm.set_ran_func_id(
            profile.ran_function_id
        )

    def normalize_indication(
        self,
        e2_agent_id,
        ric_subscription_id,
        e2_event_instance_id,
        decoded_header,
        decoded_message,
    ) -> Tuple[KpmUeSample, ...]:
        """Convert one Style 5 indication into normalized UE samples."""

        ue_measurements = decoded_message.get("ueMeasData")

        if not isinstance(ue_measurements, dict):
            raise ValueError(
                "Style 5 indication does not contain ueMeasData"
            )

        collect_start_time = decoded_header.get(
            "colletStartTime"
        )

        if hasattr(collect_start_time, "isoformat"):
            collect_start_time = collect_start_time.isoformat()
        else:
            collect_start_time = str(collect_start_time)

        throughput_metric = self.profile.measurement_names[0]
        time_scale_factor = self.profile.time_scale_factor

        if time_scale_factor <= 0:
            raise ValueError(
                "time scale factor must be positive"
            )

        samples = []

        for raw_ue_id, ue_report in sorted(
            ue_measurements.items(),
            key=lambda item: int(item[0]),
        ):
            measurements = ue_report.get("measData")

            if not isinstance(measurements, dict):
                raise ValueError(
                    f"UE {raw_ue_id} does not contain measData"
                )

            values = measurements.get(throughput_metric)

            if not isinstance(values, list) or len(values) != 1:
                raise ValueError(
                    f"UE {raw_ue_id} measurement "
                    f"{throughput_metric} must contain one value"
                )

            value = values[0]

            if (
                isinstance(value, bool) or
                not isinstance(value, (int, float))
            ):
                raise ValueError(
                    f"UE {raw_ue_id} measurement "
                    f"{throughput_metric} is not numeric"
                )

            granularity_period_ms = ue_report.get(
                "granulPeriod"
            )

            if (
                not isinstance(granularity_period_ms, int) or
                granularity_period_ms <= 0
            ):
                raise ValueError(
                    f"UE {raw_ue_id} has an invalid "
                    "granularity period"
                )

            kpm_throughput_dl_kbps = float(value)

            samples.append(
                KpmUeSample(
                    source_profile=self.profile.name,
                    e2_node_id=str(e2_agent_id),
                    ric_subscription_id=str(ric_subscription_id),
                    e2_event_instance_id=int(e2_event_instance_id),
                    ue_id=str(raw_ue_id),
                    sst=None,
                    kpm_throughput_dl_kbps=(
                        kpm_throughput_dl_kbps
                    ),
                    wall_clock_throughput_dl_kbps=(
                        kpm_throughput_dl_kbps /
                        time_scale_factor
                    ),
                    time_scale_factor=time_scale_factor,
                    collect_start_time=collect_start_time,
                    granularity_period_ms=(
                        granularity_period_ms
                    ),
                )
            )

        return tuple(samples)

    def handle_kpm_indication(
        self,
        e2_agent_id,
        e2_event_instance_id,
        indication_header,
        indication_message,
    ):
        """Decode and display one KPM indication before normalization."""
        subscription = self.my_subscriptions.get(
            e2_event_instance_id
        )

        if subscription is None:
            raise ValueError(
                "Could not resolve the RIC subscription ID"
            )

        ric_subscription_id = str(
            subscription.subscription_id
        )

        decoded_header = self.e2sm_kpm.extract_hdr_info(
            indication_header
        )

        decoded_message = self.e2sm_kpm.extract_meas_data(
            indication_message
        )

        print(
            "\n[KPM] Indication received:"
            f" node={e2_agent_id},"
            f" ricSubscription={ric_subscription_id},"
            f" e2EventInstance={e2_event_instance_id}",
            flush=True,
        )

        pprint.pprint(
            {
                "header": decoded_header,
                "message": decoded_message,
            },
            sort_dicts=True,
        )

        samples = self.normalize_indication(
            e2_agent_id,
            ric_subscription_id,
            e2_event_instance_id,
            decoded_header,
            decoded_message,
        )

        for sample in samples:
            print(
                "[KPM] Normalized UE sample:",
                flush=True,
            )

            pprint.pprint(
                asdict(sample),
                sort_dicts=False,
            )

    @xAppBase.start_function
    def start(
        self,
        e2_node_id: str,
        ue_ids: Tuple[int, ...],
        report_period_ms: int,
        granularity_period_ms: int,
        startup_delay_seconds: float,
    ):
        """Create the configured Style 5 subscription."""

        if self.profile.report_style != 5:
            raise ValueError(
                "The first collector version supports only Style 5"
            )

        print(
            f"[KPM] Waiting {startup_delay_seconds}s "
            "for RMR initialization",
            flush=True,
        )

        time.sleep(startup_delay_seconds)

        print(
            "[KPM] Creating subscription:"
            f" node={e2_node_id},"
            f" ranFunction={self.profile.ran_function_id},"
            f" style={self.profile.report_style},"
            f" UEs={ue_ids},"
            f" measurements={self.profile.measurement_names}",
            flush=True,
        )

        self.e2sm_kpm.subscribe_report_service_style_5(
            e2_node_id,
            report_period_ms,
            list(ue_ids),
            list(self.profile.measurement_names),
            granularity_period_ms,
            self.handle_kpm_indication,
        )


def parse_ue_ids(value: str) -> Tuple[int, ...]:
    """Parse and validate the comma-separated UE identifiers."""

    try:
        ue_ids = tuple(
            int(item.strip())
            for item in value.split(",")
            if item.strip()
        )
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "UE identifiers must be integers"
        ) from error

    if not ue_ids:
        raise argparse.ArgumentTypeError(
            "At least one UE identifier is required"
        )

    return ue_ids


def parse_args() -> argparse.Namespace:
    """Parse execution parameters independently from the selected profile."""

    parser = argparse.ArgumentParser(
        description="Portable NEST KPM collector xApp"
    )

    parser.add_argument(
        "--profile",
        choices=PROFILES,
        required=True,
    )

    parser.add_argument(
        "--e2-node-id",
        required=True,
    )

    parser.add_argument(
        "--ue-ids",
        type=parse_ue_ids,
        required=True,
    )

    parser.add_argument(
        "--report-period-ms",
        type=int,
        default=1000,
    )

    parser.add_argument(
        "--granularity-period-ms",
        type=int,
        default=1000,
    )

    parser.add_argument(
        "--config",
        default="",
    )

    parser.add_argument(
        "--http-server-port",
        type=int,
        default=8092,
    )

    parser.add_argument(
        "--rmr-port",
        type=int,
        default=4562,
    )

    parser.add_argument(
        "--startup-delay-seconds",
        type=float,
        default=5.0,
    )

    return parser.parse_args()


def main() -> None:
    """Load and display the selected execution contract."""

    args = parse_args()
    profile = PROFILES[args.profile]

    if args.report_period_ms <= 0:
        raise ValueError("report period must be positive")

    if args.granularity_period_ms <= 0:
        raise ValueError("granularity period must be positive")

    print("Selected KPM profile:")
    print(f"  profile: {profile.name}")
    print(f"  E2 node: {args.e2_node_id}")
    print(f"  RAN function: {profile.ran_function_id}")
    print(f"  report style: {profile.report_style}")
    print(f"  UE identity: {profile.ue_identity}")
    print(f"  UE IDs: {args.ue_ids}")
    print(f"  measurements: {profile.measurement_names}")
    print(f"  SST source: {profile.sst_source}")
    print(f"  time scale factor: {profile.time_scale_factor}")


    collector = NestKpmCollectorXapp(
        args.config,
        args.http_server_port,
        args.rmr_port,
        profile,
    )

    signal.signal(signal.SIGQUIT, collector.signal_handler)
    signal.signal(signal.SIGTERM, collector.signal_handler)
    signal.signal(signal.SIGINT, collector.signal_handler)

    collector.start(
        args.e2_node_id,
        args.ue_ids,
        args.report_period_ms,
        args.granularity_period_ms,
        args.startup_delay_seconds,
    )

if __name__ == "__main__":
    main()