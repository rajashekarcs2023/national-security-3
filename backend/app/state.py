"""Process-wide application state for the edge node.

This is the in-memory, single-tenant store the FastAPI app reads/writes from.
Holds:
  - the loaded ML classifier
  - the local baseline tracker
  - the offline queue + synced log
  - rolling buffers of recent readings / events / custody logs
  - the current edge device telemetry
  - connected WebSocket clients
"""

from __future__ import annotations

import asyncio
from collections import deque
from typing import Optional

from app.schemas import (
    AttributionResult,
    BlueForceUnit,
    ClassifiedSignal,
    CotPublication,
    CustodyStateLog,
    EdgeCommand,
    EdgeDeviceStatus,
    EmitterProfile,
    EOObservation,
    IntelligenceEvent,
    OperatorAction,
    PersistentEmitter,
    RFSignalReading,
    SensorNode,
    TdoaSolution,
    UASTrack,
)

# ---------------------------------------------------------------------------
# Emitter library (seed data) — Phase E rewrite
# ---------------------------------------------------------------------------
#
# Every profile below is parametrised the way a real SIGINT library entry
# would be: nominal bandwidth, modulation family, pulse-repetition interval
# where applicable, typical power, and operator notes. The attribution
# scorer (app.pipeline.attribution) consumes exactly these fields.
#
# Frequency bands are chosen to match real-world allocations:
#   * 144-148 MHz        — amateur / military VHF man-pack tactical radios
#   * 225-400 MHz        — military UHF SATCOM + LOS (MIL-STD-188-181)
#   * 1.625-1.66 GHz     — Inmarsat L-band sat-phone (widely used by SOF)
#   * 2.400-2.484 GHz    — ISM; DJI OcuSync, WiFi, Bluetooth
#   * 5.725-5.850 GHz    — ISM-U; DJI high-speed, WiFi 5 GHz
#   * 14.9-15.3 GHz      — Ku-band tactical data link (the "15 GHz burst"
#                           from the mentor convo — characteristic of some
#                           adversary radios the US tracks)
#
# `side="adversary_simulated"` is labelled explicitly so nothing in our
# demo suggests we detect a specific real adversary platform.
EMITTER_LIBRARY: list[EmitterProfile] = [
    # -------------------- FRIENDLY --------------------
    EmitterProfile(
        id="emitter_blue_prc148_vhf",
        name="AN/PRC-148 MBITR (VHF tactical)",
        side="friendly",
        # MBITR is 30-512 MHz on the spec sheet. We narrow to the lower
        # half (30-174 MHz, covering SINCGARS VHF tactical + amateur 2 m)
        # so the profile does not overlap the PRC-117F UHF SATCOM band
        # (225-400 MHz) and short-circuit attribution.
        expected_freq_min_mhz=30.0,
        expected_freq_max_mhz=174.0,
        expected_pattern="short_burst",
        unit_id="Blue-2",
        site_id="Alpha Site - Forward OP",
        nominal_bandwidth_mhz=0.025,
        modulation="FM",
        duty_cycle=0.10,
        typical_power_dbm=36.0,
        notes="Multi-band Inter-Team Radio programmed for VHF tactical voice (30-174 MHz).",
    ),
    EmitterProfile(
        id="emitter_blue_prc117f_uhf",
        name="AN/PRC-117F Man-Pack (UHF SATCOM)",
        side="friendly",
        expected_freq_min_mhz=225.0,
        expected_freq_max_mhz=400.0,
        expected_pattern="short_burst",
        unit_id="Blue-1",
        site_id="Alpha Site - Forward OP",
        nominal_bandwidth_mhz=0.025,
        modulation="FSK",
        duty_cycle=0.08,
        typical_power_dbm=40.0,
        notes="Primary LOS / SATCOM radio for SF teams; MIL-STD-188-181 compliant.",
    ),
    EmitterProfile(
        id="emitter_blue_inmarsat_l",
        name="Inmarsat L-band Sat-Phone",
        side="friendly",
        expected_freq_min_mhz=1625.0,
        expected_freq_max_mhz=1660.5,
        expected_pattern="continuous",
        unit_id="Blue-3",
        site_id="Alpha Site - Forward OP",
        nominal_bandwidth_mhz=0.021,
        modulation="PSK",
        duty_cycle=0.9,
        typical_power_dbm=33.0,
        notes="Commercial L-band carried by many SOF teams for long-haul comms.",
    ),
    # -------------------- CIVILIAN / BACKGROUND --------------------
    EmitterProfile(
        id="emitter_commercial_wifi_24",
        name="802.11b/g/n WiFi (2.4 GHz ISM)",
        side="civilian",
        expected_freq_min_mhz=2400.0,
        expected_freq_max_mhz=2484.0,
        expected_pattern="continuous",
        nominal_bandwidth_mhz=20.0,
        modulation="OFDM",
        duty_cycle=0.4,
        typical_power_dbm=20.0,
        notes="Expected background emitter in any populated area.",
    ),
    EmitterProfile(
        id="emitter_commercial_wifi_58",
        name="802.11ac WiFi (5 GHz U-NII)",
        side="civilian",
        expected_freq_min_mhz=5150.0,
        expected_freq_max_mhz=5850.0,
        expected_pattern="continuous",
        nominal_bandwidth_mhz=80.0,
        modulation="OFDM",
        duty_cycle=0.4,
        typical_power_dbm=23.0,
        notes="Civilian high-throughput WiFi; shares 5.8 GHz with DJI drones.",
    ),
    # -------------------- ADVERSARY-SIMULATED --------------------
    EmitterProfile(
        id="emitter_dji_control_24",
        name="DJI OcuSync 2.4 GHz control",
        side="adversary_simulated",
        expected_freq_min_mhz=2400.0,
        expected_freq_max_mhz=2484.0,
        expected_pattern="repeated_short_bursts",
        nominal_bandwidth_mhz=10.0,
        modulation="FHSS",
        duty_cycle=0.25,
        hop_pattern="ocusync_24_fh",
        typical_power_dbm=26.0,
        notes="DJI Mavic / Mini command-link.",
    ),
    EmitterProfile(
        id="emitter_dji_video_58",
        name="DJI 5.8 GHz HD video downlink",
        side="adversary_simulated",
        expected_freq_min_mhz=5725.0,
        expected_freq_max_mhz=5850.0,
        expected_pattern="frequency_hopping",
        nominal_bandwidth_mhz=40.0,
        modulation="OFDM",
        duty_cycle=0.9,
        hop_pattern="ocusync_58_fh",
        typical_power_dbm=27.0,
        notes="DJI video return-link; high duty cycle.",
    ),
    EmitterProfile(
        id="emitter_unknown_ku_15ghz",
        name="Unknown Ku-band burst (~15 GHz)",
        side="adversary_simulated",
        expected_freq_min_mhz=14900.0,
        expected_freq_max_mhz=15300.0,
        expected_pattern="short_burst",
        nominal_bandwidth_mhz=5.0,
        modulation="PSK",
        duty_cycle=0.05,
        typical_power_dbm=30.0,
        notes="Characteristic of some adversary tactical data links. "
              "Matches mentor's 15 GHz example.",
    ),
]


# ---------------------------------------------------------------------------
# Sensor array (4 nodes for TDOA geolocation) — Phase E
# ---------------------------------------------------------------------------
#
# Four edge nodes surround the AO at ~5-8 km baselines. With only three
# sensors hyperbolic TDOA has a phantom-solution ambiguity (the two
# hyperbolae intersect at *two* points), which a real array breaks by
# adding a fourth sensor. We do the same.
#
# Real SIGINT arrays use GPS-disciplined oscillators with ~10 ns jitter;
# here we simulate a slightly looser 30 ns to keep the CEP credible
# (~10-30 m inside the array, degrading outside the convex hull).
# Lat/lon chosen near 34.05 N / -118.24 W (Los Angeles demo site) so the
# MapPanel renders them in the same frame as the existing tracks.
SENSOR_ARRAY: list[SensorNode] = [
    SensorNode(
        id="EDGE-ALPHA-01",
        name="Alpha (Forward OP)",
        lat=34.0500,
        lon=-118.2500,
        alt_m=110.0,
        clock_jitter_ns=30.0,
    ),
    SensorNode(
        id="EDGE-BRAVO-02",
        name="Bravo (North Ridge)",
        lat=34.0900,
        lon=-118.2200,
        alt_m=180.0,
        clock_jitter_ns=30.0,
    ),
    SensorNode(
        id="EDGE-CHARLIE-03",
        name="Charlie (Coastal Tower)",
        lat=34.0250,
        lon=-118.2000,
        alt_m=60.0,
        clock_jitter_ns=30.0,
    ),
    SensorNode(
        id="EDGE-DELTA-04",
        name="Delta (East Outpost)",
        lat=34.0700,
        lon=-118.1700,
        alt_m=140.0,
        clock_jitter_ns=30.0,
    ),
]


# ---------------------------------------------------------------------------
# Blue-force initial positions — Phase E
# ---------------------------------------------------------------------------
#
# Simulated friendly units that might emit. The attribution engine queries
# this feed to "explain away" friendly emissions. A real deployment gets
# this from the TAK blue-force tracker / PLI bridge.
BLUE_FORCE_SEED: list[BlueForceUnit] = [
    BlueForceUnit(
        unit_id="Blue-1",
        callsign="RAIDER-1-6",
        lat=34.0480,
        lon=-118.2420,
        alt_m=105.0,
        active_emitters=["emitter_blue_prc117f_uhf", "emitter_blue_inmarsat_l"],
        heading_deg=45.0,
        speed_mps=2.0,
    ),
    BlueForceUnit(
        unit_id="Blue-2",
        callsign="RAIDER-1-8",
        lat=34.0560,
        lon=-118.2480,
        alt_m=120.0,
        active_emitters=["emitter_blue_prc148_vhf"],
        heading_deg=135.0,
        speed_mps=1.5,
    ),
    BlueForceUnit(
        unit_id="Blue-3",
        callsign="EAGLE-2-1",
        lat=34.0720,
        lon=-118.2150,
        alt_m=180.0,
        active_emitters=["emitter_blue_prc117f_uhf"],
        heading_deg=200.0,
        speed_mps=0.0,  # stationary OP
    ),
]


class AppState:
    """Singleton holding all live runtime state."""

    def __init__(self) -> None:
        # ------- Edge node config -------
        self.network_online: bool = True
        self.sensitivity_mode: str = "normal"
        self.watch_band_mhz: Optional[tuple[float, float]] = None
        # Phase C — sitewide ROE posture used by the COA recommender. Starts
        # DEFENSIVE (the doctrinally "normal" posture for a forward OP with
        # an active sensor grid). Operators flip to WEAPONS_FREE on order.
        self.roe_posture: str = "DEFENSIVE"
        # Rolling audit log of operator COA decisions — what was picked,
        # for which track, under which posture. Capped at 200. Fed into
        # the after-action brief and the Foundry export.
        self.coa_decisions: deque[dict] = deque(maxlen=200)

        # ------- ML classifier (filled by main.py at startup) -------
        self.classifier = None  # type: ignore[assignment]
        self.baseline = None    # type: ignore[assignment]
        self.llm = None         # type: ignore[assignment]

        # ------- Rolling buffers (for live feed / dashboard) -------
        self.recent_readings: deque[RFSignalReading] = deque(maxlen=200)
        self.recent_classifications: deque[ClassifiedSignal] = deque(maxlen=200)
        self.intelligence_events: deque[IntelligenceEvent] = deque(maxlen=500)
        self.custody_logs: deque[CustodyStateLog] = deque(maxlen=500)
        self.operator_actions: deque[OperatorAction] = deque(maxlen=500)
        self.commands: deque[EdgeCommand] = deque(maxlen=500)
        # Phase A — rolling buffer of EO/IR tipping-camera observations,
        # driven by RF custody opens. Used by the CrossSensorPanel in the UI
        # and by the Foundry export bundle for replay.
        self.eo_observations: deque[EOObservation] = deque(maxlen=200)
        # Phase B — every CoT XML message we publish to ATAK / FreeTAK /
        # TAK clients lands here, so the dashboard can show "recent CoTs"
        # and an operator can re-copy the wire bytes if a downstream TAK
        # ingest fails. Capped at 100 to keep the snapshot small.
        self.cot_publications: deque[CotPublication] = deque(maxlen=100)

        # ------- Tracks (custody) -------
        self.tracks: dict[str, UASTrack] = {}

        # ------- DDIL queue + sync log -------
        self.offline_queue: deque[IntelligenceEvent] = deque()

        # ------- Counters -------
        self.total_readings_processed: int = 0
        self.total_filtered_local: int = 0
        self.total_events_synced: int = 0
        self.bytes_saved_at_edge: int = 0
        self.bytes_actually_synced: int = 0

        # ------- WebSocket clients -------
        self.ws_clients: set = set()
        self.ws_lock = asyncio.Lock()

        # ------- Emulator control -------
        self.emulator_running: bool = False
        self.emulator_paused: bool = False
        self.scenario_active: bool = False

        # ------- Real-data injection (RadioML 2016.10A) -------
        # Fraction of free-run ticks that should pull from the real DeepSig
        # I/Q dataset instead of the synthetic generator. 0 = pure synth.
        # `real_data_available` is set at startup by main.py based on whether
        # the dataset pickle is actually on disk.
        self.real_data_mix: float = 0.0
        self.real_data_available: bool = False

        # ------- Emitter library -------
        self.emitter_library: list[EmitterProfile] = list(EMITTER_LIBRARY)

        # ------- Phase E: attribution, blue-force, geolocation -------
        # Sensor array for TDOA — 3 nodes surrounding the AO. Treated as
        # "live" from startup; the emulator derives time-of-arrival for
        # each RF detection at each node based on its simulated position.
        self.sensor_array: list[SensorNode] = list(SENSOR_ARRAY)
        # Live blue-force feed — mutable, the sim nudges positions every
        # few seconds to mimic real unit movement.
        self.blue_force: dict[str, BlueForceUnit] = {
            u.unit_id: u.model_copy() for u in BLUE_FORCE_SEED
        }
        # Rolling attribution results, keyed by the triggering event id so
        # the UI can colour intel events by verdict without re-running the
        # attribution engine.
        self.attribution_by_event: dict[str, AttributionResult] = {}
        self.attribution_recent: deque[AttributionResult] = deque(maxlen=200)
        # TDOA solutions keyed by track_id (latest wins). Also kept in a
        # rolling buffer for the "recent fixes" panel.
        self.tdoa_by_track: dict[str, TdoaSolution] = {}
        self.tdoa_recent: deque[TdoaSolution] = deque(maxlen=200)
        # Persistent-unknown-emitter clusters, keyed by cluster id.
        self.persistent_emitters: dict[str, PersistentEmitter] = {}
        # Counters for the "unexplained tonight" badge in the UI.
        self.counter_blue_attributed: int = 0
        self.counter_red_known: int = 0
        self.counter_unexplained: int = 0
        self.counter_ambiguous: int = 0

    # ------------------------------------------------------------------
    def device_status(self) -> EdgeDeviceStatus:
        net = "CONNECTED" if self.network_online else "DISCONNECTED"
        return EdgeDeviceStatus(
            network_status=net,
            sensitivity_mode=self.sensitivity_mode,  # type: ignore[arg-type]
            watch_band_mhz=self.watch_band_mhz,
            sync_queue_depth=len(self.offline_queue),
            active_tracks=sum(
                1 for t in self.tracks.values()
                if t.custody_state not in ("CLEARED", "DISMISSED", "TRACK_LOST")
            ),
            total_readings_processed=self.total_readings_processed,
            total_filtered_local=self.total_filtered_local,
            total_events_synced=self.total_events_synced,
            bytes_saved_at_edge=self.bytes_saved_at_edge,
            bytes_actually_synced=self.bytes_actually_synced,
            model_loaded=self.classifier is not None,
            model_summary=self.classifier.summary() if self.classifier else None,
            real_data_available=self.real_data_available,
            real_data_mix=self.real_data_mix,
        )


# Global singleton
STATE = AppState()
