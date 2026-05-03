"""Scripted demo scenarios.

A scenario is a sequence of (class_to_emit, dwell_ticks, optional note)
instructions for the RF emulator, plus optional control directives that mutate
runtime state (toggle network, change sensitivity, etc.).

The flagship scenario is the 14-step Site Alpha story documented in context.md
and context0.md: normal -> friendly -> anomaly -> network drop -> queued
events -> network return -> sync -> command-down -> drone swarm -> escalate.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Optional, Union


@dataclass
class EmitStep:
    kind: Literal["emit"] = "emit"
    class_name: str = "background_noise"
    dwell_ticks: int = 1
    note: Optional[str] = None


@dataclass
class NetworkStep:
    kind: Literal["network"] = "network"
    online: bool = True
    note: Optional[str] = None


@dataclass
class SensitivityStep:
    kind: Literal["sensitivity"] = "sensitivity"
    mode: Literal["normal", "high", "low"] = "normal"
    note: Optional[str] = None


@dataclass
class WaitStep:
    kind: Literal["wait"] = "wait"
    seconds: float = 1.0
    note: Optional[str] = None


@dataclass
class AnnounceStep:
    """Emits a plain announcement to the dashboard scenario panel."""

    kind: Literal["announce"] = "announce"
    text: str = ""


@dataclass
class EOFailStep:
    """Force the simulated EO sensor into ``no_visual`` for ``seconds``.

    Phase A — used by ``cross_cue_demo`` to demonstrate the
    ``TRACKING → VISUAL_LOST_RF_PRESENT → REACQUIRED`` custody cycle that
    real operators see when fog, dust, tree-line, or gimbal faults
    blind the camera while the RF detection still holds.

    The runner calls ``app.state.eo_sensor.disable_for(step.seconds)`` and
    keeps moving — the sensor naturally re-enables once the timer expires.
    """

    kind: Literal["eo_fail"] = "eo_fail"
    seconds: float = 8.0
    note: Optional[str] = None


ScenarioStep = Union[
    EmitStep, NetworkStep, SensitivityStep, WaitStep, AnnounceStep, EOFailStep
]


@dataclass
class Scenario:
    name: str
    description: str
    steps: list[ScenarioStep] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Scenarios
# ---------------------------------------------------------------------------

FULL_DEMO = Scenario(
    name="full_demo",
    description=(
        "The 14-step Site Alpha mission: normal background, friendly Blue-2 "
        "attribution, unknown burst, network outage with offline queueing, "
        "drone-control-like activity during outage, network restoration with "
        "queued sync, command-down sensitivity boost, drone swarm, and "
        "operator escalation."
    ),
    steps=[
        AnnounceStep(text="Site Alpha online. Edge node EDGE-RF-01 monitoring."),

        # 1. Normal background baseline
        EmitStep(class_name="background_noise", dwell_ticks=4, note="ambient baseline"),
        EmitStep(class_name="commercial_continuous", dwell_ticks=2, note="commercial wifi-like"),
        EmitStep(class_name="background_noise", dwell_ticks=3),

        # 2. Friendly emitter appears -> attributed
        AnnounceStep(text="Friendly Blue-2 emission inbound."),
        EmitStep(class_name="friendly_radio_burst", dwell_ticks=2, note="Blue-2 short burst"),
        EmitStep(class_name="background_noise", dwell_ticks=2),
        EmitStep(class_name="friendly_radio_burst", dwell_ticks=1),

        # 3. Unknown burst — first anomaly
        AnnounceStep(text="Unknown RF burst NE sector."),
        EmitStep(class_name="unknown_ood", dwell_ticks=1, note="single unknown burst"),
        EmitStep(class_name="background_noise", dwell_ticks=2),

        # 4. Network goes DOWN
        AnnounceStep(text="Command link DROPPED. Edge continues classifying locally."),
        NetworkStep(online=False, note="DDIL begin"),
        EmitStep(class_name="background_noise", dwell_ticks=2),

        # 5. Repeated drone-control-like bursts during outage
        AnnounceStep(text="Repeated bursts during outage — drone-control-like pattern."),
        EmitStep(class_name="drone_control_repeated_burst", dwell_ticks=2,
                 note="repeated burst during outage"),
        EmitStep(class_name="background_noise", dwell_ticks=1),
        EmitStep(class_name="drone_control_repeated_burst", dwell_ticks=2,
                 note="persistent unknown emitter"),

        # 6. Frequency hopping sample
        EmitStep(class_name="frequency_hopping", dwell_ticks=1, note="hopping signal"),
        EmitStep(class_name="background_noise", dwell_ticks=2),

        # 7. Network RESTORED -> sync queued
        AnnounceStep(text="Command link RESTORED. Draining offline queue."),
        NetworkStep(online=True, note="DDIL end — drain queue"),
        EmitStep(class_name="background_noise", dwell_ticks=2),

        # 8. Command sends down: increase sensitivity
        AnnounceStep(text="Command-down: increase sensitivity for drone band."),
        SensitivityStep(mode="high", note="op increased sensitivity"),
        EmitStep(class_name="background_noise", dwell_ticks=1),

        # 9. Profile mismatch (possible spoof)
        AnnounceStep(text="Friendly profile mismatch — verify Blue-2."),
        EmitStep(class_name="friendly_profile_mismatch", dwell_ticks=1,
                 note="Blue-2 in wrong band"),

        # 10. Chirp event
        EmitStep(class_name="chirp", dwell_ticks=1, note="RF chirp / sweep"),
        EmitStep(class_name="background_noise", dwell_ticks=1),

        # 11. Drone swarm (persistent activity)
        AnnounceStep(text="Drone swarm activity NE sector — multiple tracks."),
        EmitStep(class_name="drone_control_repeated_burst", dwell_ticks=2,
                 note="swarm contact 1"),
        EmitStep(class_name="frequency_hopping", dwell_ticks=1, note="swarm contact 2"),
        EmitStep(class_name="drone_control_repeated_burst", dwell_ticks=1,
                 note="swarm contact 3"),

        # 12. Return to baseline; sensitivity normal
        AnnounceStep(text="Demo complete. Restoring sensitivity to normal."),
        SensitivityStep(mode="normal"),
        EmitStep(class_name="background_noise", dwell_ticks=3),
    ],
)


QUICK_ANOMALY = Scenario(
    name="quick_anomaly",
    description="60-second compressed anomaly demo.",
    steps=[
        EmitStep(class_name="background_noise", dwell_ticks=2),
        EmitStep(class_name="friendly_radio_burst", dwell_ticks=1),
        EmitStep(class_name="unknown_ood", dwell_ticks=1),
        EmitStep(class_name="drone_control_repeated_burst", dwell_ticks=2),
        EmitStep(class_name="background_noise", dwell_ticks=2),
    ],
)


DRONE_SWARM = Scenario(
    name="drone_swarm",
    description="Drone-swarm focused scenario.",
    steps=[
        EmitStep(class_name="background_noise", dwell_ticks=2),
        EmitStep(class_name="drone_control_repeated_burst", dwell_ticks=2),
        EmitStep(class_name="frequency_hopping", dwell_ticks=2),
        EmitStep(class_name="drone_control_repeated_burst", dwell_ticks=1),
        EmitStep(class_name="frequency_hopping", dwell_ticks=1),
        EmitStep(class_name="drone_control_repeated_burst", dwell_ticks=2),
        EmitStep(class_name="background_noise", dwell_ticks=2),
    ],
)


# ---------------------------------------------------------------------------
# Phase A — cross-sensor cueing scenario
#
# Walks the operator through the four multi-modal custody beats that the
# kill-chain plan (PLAN_KILL_CHAIN.md §A) calls out:
#
#   1. RF detection opens custody (DETECTED)
#   2. EO confirms → multi-modal hold (TRACKING)
#   3. EO blinded (fog / occlusion / gimbal) while RF still holds
#      (VISUAL_LOST_RF_PRESENT)
#   4. EO re-acquires → REACQUIRED → back to TRACKING
#
# The cadence is deliberately short (~30 s total) so it fits inside a
# 60-second judge demo and produces clean before/after screenshots.
# ---------------------------------------------------------------------------

CROSS_CUE_DEMO = Scenario(
    name="cross_cue_demo",
    description=(
        "Cross-sensor cueing: RF custody opens, EO confirms, EO blinds, "
        "RF holds, then EO re-acquires. Exercises the multi-modal custody "
        "states (DETECTED → TRACKING → VISUAL_LOST_RF_PRESENT → REACQUIRED)."
    ),
    # Timing notes: the run_scenario walker advances on wall-clock while the
    # emulator processes one ScriptedStep per ~0.9 s tick. Each AnnounceStep
    # / EOFailStep adds ~0.3 s of walker time without an emulator tick, which
    # would drift the walker past the script if we don't compensate. The
    # design here avoids any WaitStep and keeps the announce count tight so
    # the re-acquire EmitSteps land *before* the emulator script exhausts.
    steps=[
        AnnounceStep(text="Cross-sensor cueing demo. Watching NW sector."),

        # 1. Quiet baseline — calm spectrogram before the open. (1 tick)
        EmitStep(class_name="background_noise", dwell_ticks=1),

        # 2. RF detection — drone-control bursts open DETECTED and the
        #    first EO tip fires asynchronously in process_tick(). (2 ticks)
        AnnounceStep(text="RF burst NW — opening custody, tipping EO gimbal."),
        EmitStep(class_name="drone_control_repeated_burst", dwell_ticks=2,
                 note="RF detection — gimbal slewing"),

        # 3. Continued bursts — EO confirms, custody promotes to TRACKING.
        #    (2 ticks of drone, 1 tick of hopping = 3 ticks total)
        AnnounceStep(text="EO confirmed — multi-modal custody hold."),
        EmitStep(class_name="drone_control_repeated_burst", dwell_ticks=2,
                 note="multi-modal hold"),
        EmitStep(class_name="frequency_hopping", dwell_ticks=1,
                 note="hopping — RF still tight"),

        # 4. EO blinds for 7 s — fog / dust / occlusion / gimbal fault.
        #    Custody sweep (period 2 s, threshold 4 s of no fresh EO confirm)
        #    degrades the track to VISUAL_LOST_RF_PRESENT during this window.
        #    Mask must be longer than (VISUAL_LOST_WINDOW_SECONDS + sweep
        #    period) ≈ 6 s so degrade fires *before* the re-acquire emits.
        #    7 s gives a 1 s margin. (6 ticks under mask)
        AnnounceStep(text="EO occluded (fog/dust). RF still holds."),
        EOFailStep(seconds=7.0, note="EO blinded — no_visual frames forced"),
        EmitStep(class_name="drone_control_repeated_burst", dwell_ticks=3,
                 note="RF holds while EO is dark"),
        EmitStep(class_name="frequency_hopping", dwell_ticks=3,
                 note="visual lost — RF present"),

        # 5. EO re-enables and the next confirming tip flips the track to
        #    REACQUIRED, then a follow-up confirm returns it to TRACKING.
        #    The first re-acquire emit is intentionally background_noise to
        #    pause RF for one tick — gives the EO freshness gate (2 s) time
        #    to clear after the last masked tip and lets the mask expire
        #    cleanly before any new tip fires.
        EmitStep(class_name="background_noise", dwell_ticks=2,
                 note="brief lull while EO recovers"),
        AnnounceStep(text="EO re-acquiring — gimbal back online."),
        EmitStep(class_name="drone_control_repeated_burst", dwell_ticks=2,
                 note="re-acquire attempt"),
        EmitStep(class_name="drone_control_repeated_burst", dwell_ticks=1,
                 note="multi-modal hold restored"),

        # 6. Wind down. Operator can publish CoT / pick a COA from here.
        AnnounceStep(text="Cross-sensor demo complete."),
        EmitStep(class_name="background_noise", dwell_ticks=1),
    ],
)


# ---------------------------------------------------------------------------
# Phase E — Persistent Unknown Emitter scenario.
# ---------------------------------------------------------------------------
#
# Mentor's brief: "we attributed this 15GHz burst to UNKNOWN, geolocated,
# 3rd recurrence tonight — recommending COA: HAND_OFF_INTERCEPTOR."
#
# This scenario fires repeated ``unknown_ood`` bursts. They land on the
# same rotating spot in ``main._pick_true_emitter_position`` because we
# keep the class steady — the spot rotates only every 50 reads, so a
# dozen sequential unknowns stay anchored. Once 3+ unknowns hit the same
# spot, the streaming-DBSCAN persistence detector promotes them to a
# ``PersistentEmitter`` event (MEDIUM + INVESTIGATE_AND_GEOLOCATE). As the
# cluster grows past 5 it bumps to HIGH priority with recommended action
# ``HAND_OFF_INTERCEPTOR`` — matching the mentor's scripted demo moment.
PERSISTENT_UNKNOWN_DEMO = Scenario(
    name="persistent_unknown_demo",
    description=(
        "Phase E: 12 sequential unknown_ood bursts at the same lurking spot "
        "→ streaming DBSCAN promotes a PersistentEmitter event "
        "(MEDIUM → HIGH/HAND_OFF_INTERCEPTOR as the cluster grows)."
    ),
    steps=[
        AnnounceStep(text="Phase E demo — persistent unknown emitter."),
        # Hot start: keep a steady cadence of unknown_ood at the same lurking
        # spot. The rotating spot in ``main._pick_true_emitter_position``
        # advances every 50 reads — 12 sequential unknowns stay anchored.
        # Roughly half land a successful TDOA fix (the ones that don't are
        # gated by min-residual / GDOP), so we emit 12 to comfortably cross
        # the n>=5 HIGH-priority threshold.
        EmitStep(class_name="unknown_ood", dwell_ticks=1, note="1st unknown burst"),
        EmitStep(class_name="unknown_ood", dwell_ticks=1, note="2nd"),
        EmitStep(class_name="unknown_ood", dwell_ticks=1, note="3rd"),
        EmitStep(class_name="unknown_ood", dwell_ticks=1, note="4th"),
        AnnounceStep(text="Persistence detector should fire (n>=3, MEDIUM)."),
        EmitStep(class_name="unknown_ood", dwell_ticks=1, note="5th"),
        EmitStep(class_name="unknown_ood", dwell_ticks=1, note="6th"),
        EmitStep(class_name="unknown_ood", dwell_ticks=1, note="7th"),
        EmitStep(class_name="unknown_ood", dwell_ticks=1, note="8th"),
        EmitStep(class_name="unknown_ood", dwell_ticks=1, note="9th"),
        EmitStep(class_name="unknown_ood", dwell_ticks=1, note="10th"),
        AnnounceStep(text="Cluster size 5+ → priority HIGH, HAND_OFF_INTERCEPTOR."),
        EmitStep(class_name="unknown_ood", dwell_ticks=1, note="11th"),
        EmitStep(class_name="unknown_ood", dwell_ticks=1, note="12th — sustained"),
        AnnounceStep(text="Persistent unknown emitter demo complete."),
    ],
)


SCENARIOS: dict[str, Scenario] = {
    s.name: s
    for s in (FULL_DEMO, QUICK_ANOMALY, DRONE_SWARM, CROSS_CUE_DEMO, PERSISTENT_UNKNOWN_DEMO)
}
