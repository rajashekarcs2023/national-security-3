"""Cursor-on-Target (CoT) XML generation — Phase B.

Converts an ``IntelligenceEvent`` (and optional ``UASTrack``) into a
CoT 2.0 XML message that can be ingested by ATAK / WinTAK / iTAK,
FreeTAKServer, or any TAK-compatible C2 client.

The intent is *operator-correct*: a real S2 cell at a forward OP should
be able to see this message land in their map view and immediately
understand:

  * affiliation (friend / hostile / unknown / suspect / neutral)
  * domain (air / ground)
  * what the system actually saw (RF + EO evidence, with confidence)
  * how stale the contact is, and which sensor cued it
  * a backref to our internal track / event IDs for replay

Two output forms:

  * `build_cot_xml(event, track)` -> minified UTF-8 XML bytes ready for
    UDP / TCP transport.
  * `build_cot_dict(event, track)` -> Python dict mirror of the XML tree
    so the frontend can render a structured preview before publishing.

This module is intentionally pure-function — no network, no globals,
no time.time() calls outside the explicit ``stale_seconds`` parameter.
That makes it easy to unit-test the wire format byte-for-byte and lets
us re-use the exact same XML for "preview" and "publish" buttons.

References:
  * MITRE Cursor on Target (CoT) Schema, Mar 2005 (event 2.0)
  * MIL-STD-2525C symbol identification codes (SIDC), 2008
  * TAK Product Center "CoT XML Format Reference"
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from xml.etree import ElementTree as ET

from app.schemas import EOObservation, IntelligenceEvent, UASTrack


# ---------------------------------------------------------------------------
# Public configuration knobs.
# ---------------------------------------------------------------------------

# How long after `time` a CoT contact is considered "fresh" by ATAK.
# Real operators usually pick 60–120 s for live RF tracks; if we set this
# too short the icon vanishes from the map between updates, too long and
# stale ghosts pile up. 60 s matches the demo cadence.
DEFAULT_STALE_SECONDS = 60

# CoT `how` attribute. `m-g` = "machine-generated, GPS-derived". This is
# the right value for our edge node since the position is computed from
# the sensor's own GPS and the RF DOA estimate, not entered manually.
COT_HOW = "m-g"

# CoT 2.0 protocol version string. Constant; do not change.
COT_VERSION = "2.0"


# ---------------------------------------------------------------------------
# Symbology — CoT type cosmology + 2525C SIDC mapping.
# ---------------------------------------------------------------------------
#
# We map our internal (event_type, classification, EO frame_kind, visual_confirmed)
# tuple onto an ATAK-renderable CoT type and the corresponding 2525C SIDC.
#
# Notation reminder:
#   CoT type:  a-<aff>-<dim>-<service>-<...>
#       aff = f (friendly) | h (hostile) | n (neutral) | u (unknown)
#             | s (suspect) | p (pending) | j (joker) | k (faker)
#       dim = A (air) | G (ground) | S (sea surface) | U (sub) | P (space)
#       service = M (military) | C (civilian) ...
#   2525C SIDC (15 chars):
#       1: scheme  S = warfighting
#       2: affiliation F H N U S P
#       3: dimension   A G S U P
#       4: status      P (present) A (anticipated)
#       5-10: function ID
#       11-12, 13-15: modifiers / country (usually --, ---)
#
# Where the 2525C taxonomy doesn't have a perfect quadcopter pictogram,
# we fall back to the closest accepted symbol (rotary-wing UAV). ATAK is
# tolerant of unknown SIDCs and will render a generic affiliation icon.


@dataclass(frozen=True)
class _Symbology:
    cot_type: str
    sidc: str            # MIL-STD-2525C 15-char code
    callsign_prefix: str  # human-readable prefix used in the CoT detail block
    icon_name: str       # short label for the UI preview


# Visual-confirmed quadcopter (Group-1) — the most common adversary class.
_QUAD_HOSTILE = _Symbology(
    cot_type="a-h-A-M-H-Q",
    sidc="SHAPMHQ---",
    callsign_prefix="HOSTILE-QUAD",
    icon_name="Hostile rotary-wing UAS",
)
# Visual-confirmed fixed-wing UAS (Group-3 ish).
_FIXED_WING_HOSTILE = _Symbology(
    cot_type="a-h-A-M-F-U",
    sidc="SHAPMFU---",
    callsign_prefix="HOSTILE-UAS",
    icon_name="Hostile fixed-wing UAS",
)
# RF says drone, no EO yet (or EO is no_visual). Hostile air, generic.
_HOSTILE_AIR_GENERIC = _Symbology(
    cot_type="a-h-A",
    sidc="SHAP------",
    callsign_prefix="HOSTILE-AIR",
    icon_name="Hostile air contact",
)
# Possible UAS — RF anomaly without visual or below-confirmation confidence.
_SUSPECT_AIR = _Symbology(
    cot_type="a-s-A",
    sidc="SSAP------",
    callsign_prefix="SUSPECT-AIR",
    icon_name="Suspect air contact",
)
# Unknown air / persistent unknown.
_UNKNOWN_AIR = _Symbology(
    cot_type="a-u-A",
    sidc="SUAP------",
    callsign_prefix="UNK-AIR",
    icon_name="Unknown air contact",
)
# Friendly emission attributed to a known unit (e.g. Blue-2 short burst).
_FRIENDLY_GROUND = _Symbology(
    cot_type="a-f-G-U-C",
    sidc="SFGPUCI---",
    callsign_prefix="FRIENDLY",
    icon_name="Friendly ground unit",
)
# Friendly profile mismatch — emitter looks friendly but in wrong band /
# wrong sector. We mark as SUSPECT so an operator investigates rather than
# reflexively trusting.
_FRIENDLY_MISMATCH = _Symbology(
    cot_type="a-s-G-U-C",
    sidc="SSGPUCI---",
    callsign_prefix="VERIFY-FRIEND",
    icon_name="Friendly callsign in wrong sector — verify",
)
# Filtered / false alarm. We rarely publish these but the symbol exists
# for completeness if an operator wants to mark "checked & dismissed".
_NEUTRAL_AIR = _Symbology(
    cot_type="a-n-A",
    sidc="SNAP------",
    callsign_prefix="DISMISSED",
    icon_name="Dismissed contact",
)


def _select_symbology(
    event: IntelligenceEvent,
    track: Optional[UASTrack],
) -> _Symbology:
    """Pick the right CoT type / 2525C SIDC for this event+track tuple.

    Decision priority (most specific first):
      1. Friendly emission → friendly ground.
      2. Profile mismatch  → suspect ground (deception possibility).
      3. Visual-confirmed quadcopter → hostile rotary UAS.
      4. Visual-confirmed fixed-wing → hostile fixed-wing UAS.
      5. RF event_type POSSIBLE_UAS_ACTIVITY w/o visual → suspect air.
      6. Persistent unknown → unknown air.
      7. Generic RF anomaly → hostile air (worst-case so the operator
         is alerted; the operator can downgrade to suspect/unknown if
         desired before publishing).
    """
    if event.event_type == "FRIENDLY_EMISSION":
        return _FRIENDLY_GROUND
    if event.event_type == "PROFILE_MISMATCH":
        return _FRIENDLY_MISMATCH

    eo: Optional[EOObservation] = track.last_eo_obs if track is not None else None
    if eo is not None and eo.confirms_rf:
        if eo.frame_kind == "quadcopter":
            return _QUAD_HOSTILE
        if eo.frame_kind == "fixed_wing":
            return _FIXED_WING_HOSTILE
        # person / other recognised target on a hostile RF track — keep generic
        return _HOSTILE_AIR_GENERIC

    if event.event_type == "POSSIBLE_UAS_ACTIVITY":
        return _SUSPECT_AIR
    if event.event_type == "PERSISTENT_UNKNOWN":
        return _UNKNOWN_AIR

    # Default for RF_ANOMALY without visual confirmation.
    return _HOSTILE_AIR_GENERIC


# ---------------------------------------------------------------------------
# Time formatting.
# ---------------------------------------------------------------------------

def _iso_z(dt: datetime) -> str:
    """Render a tz-aware datetime as the ATAK-flavoured ISO-8601 'Z' format.

    Examples produced:
      2026-05-03T12:34:56.123Z

    ATAK's MIL-STD-2045-47001 parser tolerates microsecond precision, but
    millisecond is the cleanest interchange format and matches what
    FreeTAKServer emits.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    base = dt.strftime("%Y-%m-%dT%H:%M:%S")
    millis = f"{int(dt.microsecond / 1000):03d}"
    return f"{base}.{millis}Z"


# ---------------------------------------------------------------------------
# UID strategy.
# ---------------------------------------------------------------------------

def _cot_uid(event: IntelligenceEvent, track: Optional[UASTrack]) -> str:
    """Stable UID so re-publishing the same event updates the same icon.

    Convention: ``SPECTRUMCUSTODY.<sensor>.<track-or-event-id>``. Real
    deployments use a UUID; we keep it human-grepable so the demo viewer
    can correlate the ATAK icon back to the dashboard event.
    """
    base = track.track_id if track is not None else event.id
    sensor = event.sensor_id or "EDGE-RF-01"
    return f"SPECTRUMCUSTODY.{sensor}.{base}"


# ---------------------------------------------------------------------------
# Builder — dict form (used by the frontend preview AND by build_cot_xml).
# ---------------------------------------------------------------------------

def build_cot_dict(
    event: IntelligenceEvent,
    track: Optional[UASTrack] = None,
    *,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    site_lat: Optional[float] = None,
    site_lon: Optional[float] = None,
    altitude_m: float = 50.0,
) -> dict:
    """Structured CoT message — same data as the XML, easier to render.

    Lat/lon precedence: track > explicit args > sensible Alpha-Site default.
    The site_lat / site_lon overrides exist so the publish endpoint can
    pull live position from the device record without re-loading state.
    """
    symbology = _select_symbology(event, track)
    uid = _cot_uid(event, track)
    now = event.timestamp
    stale = now + timedelta(seconds=stale_seconds)

    if track is not None:
        lat = track.last_known_lat
        lon = track.last_known_lon
        alt = track.last_known_alt_m
    else:
        lat = site_lat if site_lat is not None else 34.0522
        lon = site_lon if site_lon is not None else -118.2437
        alt = altitude_m

    callsign = f"{symbology.callsign_prefix}-{(track.track_id if track else event.id)[-6:]}"

    # Free-text remarks — small enough to fit inside a CoT detail block
    # (ATAK truncates at ~512 chars in the cluetip). Keep it RF-honest.
    confidence_pct = int(round(event.confidence * 100))
    eo = track.last_eo_obs if track is not None else None
    eo_line = ""
    if eo is not None:
        eo_line = (
            f" | EO {eo.frame_kind} conf={int(round(eo.confidence * 100))}% "
            f"slew={eo.slew_time_ms}ms "
            f"({'confirms' if eo.confirms_rf else 'does NOT confirm'} RF)"
        )
    custody_line = ""
    if track is not None:
        custody_line = (
            f" | custody={track.custody_state} "
            f"visual_confirmed={track.visual_confirmed} "
            f"n_det={track.n_detections}"
        )
    remarks = (
        f"[SpectrumCustody] {event.title} | "
        f"RF class={event.classification} conf={confidence_pct}% "
        f"ood={event.ood_score:.2f}{eo_line}{custody_line} | "
        f"recommended: {event.recommended_action}"
    )

    return {
        "version": COT_VERSION,
        "uid": uid,
        "type": symbology.cot_type,
        "how": COT_HOW,
        "time": _iso_z(now),
        "start": _iso_z(now),
        "stale": _iso_z(stale),
        "point": {
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "hae": round(alt, 1),
            # CoT circular error and linear error — set to 9999999.0
            # (the standard "unknown / not provided" sentinel).
            "ce": 9999999.0,
            "le": 9999999.0,
        },
        "detail": {
            "contact": {"callsign": callsign},
            "__group": {"name": "SpectrumCustody", "role": "RF/EO Edge Node"},
            "remarks": remarks,
            "precisionlocation": {"altsrc": "GPS", "geopointsrc": "GPS"},
            "status": {"battery": "100"},
            "track": {
                "course": 0.0,
                "speed": 0.0,
            },
            # Symbol affiliation block — readable by ATAK 4.5+ and gives
            # the 2525C-aware renderer the SIDC explicitly.
            "usericon": {
                "iconsetpath": f"COT_MAPPING_2525C/{symbology.sidc}",
            },
            # Backref to our internal IDs so an operator (or replay) can
            # correlate the icon with the source event in our dashboard.
            "_spectrumcustody": {
                "event_id": event.id,
                "track_id": track.track_id if track is not None else None,
                "site_id": event.site_id,
                "sensor_id": event.sensor_id,
                "priority": event.priority,
                "sidc": symbology.sidc,
                "icon_name": symbology.icon_name,
            },
        },
    }


# ---------------------------------------------------------------------------
# Builder — XML form.
# ---------------------------------------------------------------------------

def build_cot_xml(
    event: IntelligenceEvent,
    track: Optional[UASTrack] = None,
    *,
    stale_seconds: int = DEFAULT_STALE_SECONDS,
    site_lat: Optional[float] = None,
    site_lon: Optional[float] = None,
    altitude_m: float = 50.0,
) -> str:
    """Build a wire-format CoT 2.0 XML string for ATAK / FreeTAKServer.

    Returns a UTF-8 string with an XML declaration. Pretty-printing is
    intentionally OFF — TAK servers parse minified, and the saved bytes
    can be UDP-broadcast directly without re-encoding.
    """
    d = build_cot_dict(
        event,
        track,
        stale_seconds=stale_seconds,
        site_lat=site_lat,
        site_lon=site_lon,
        altitude_m=altitude_m,
    )

    ev = ET.Element(
        "event",
        attrib={
            "version": d["version"],
            "uid": d["uid"],
            "type": d["type"],
            "how": d["how"],
            "time": d["time"],
            "start": d["start"],
            "stale": d["stale"],
        },
    )
    p = d["point"]
    ET.SubElement(
        ev,
        "point",
        attrib={
            "lat": f"{p['lat']:.6f}",
            "lon": f"{p['lon']:.6f}",
            "hae": f"{p['hae']:.1f}",
            "ce": f"{p['ce']:.1f}",
            "le": f"{p['le']:.1f}",
        },
    )
    detail = ET.SubElement(ev, "detail")
    contact = d["detail"]["contact"]
    ET.SubElement(detail, "contact", attrib={"callsign": contact["callsign"]})
    grp = d["detail"]["__group"]
    ET.SubElement(detail, "__group", attrib={"name": grp["name"], "role": grp["role"]})
    remarks_el = ET.SubElement(detail, "remarks")
    remarks_el.text = d["detail"]["remarks"]
    pl = d["detail"]["precisionlocation"]
    ET.SubElement(
        detail,
        "precisionlocation",
        attrib={"altsrc": pl["altsrc"], "geopointsrc": pl["geopointsrc"]},
    )
    st = d["detail"]["status"]
    ET.SubElement(detail, "status", attrib={"battery": st["battery"]})
    tr = d["detail"]["track"]
    ET.SubElement(detail, "track", attrib={"course": f"{tr['course']:.1f}", "speed": f"{tr['speed']:.2f}"})
    ic = d["detail"]["usericon"]
    ET.SubElement(detail, "usericon", attrib={"iconsetpath": ic["iconsetpath"]})

    # Custom backref namespace. ATAK ignores unknown elements inside
    # <detail>, so this round-trips cleanly even for clients that don't
    # understand the SpectrumCustody schema.
    sc = d["detail"]["_spectrumcustody"]
    ET.SubElement(
        detail,
        "_spectrumcustody",
        attrib={
            "event_id": str(sc["event_id"] or ""),
            "track_id": str(sc["track_id"] or ""),
            "site_id": str(sc["site_id"] or ""),
            "sensor_id": str(sc["sensor_id"] or ""),
            "priority": str(sc["priority"] or ""),
            "sidc": str(sc["sidc"] or ""),
            "icon_name": str(sc["icon_name"] or ""),
        },
    )

    body = ET.tostring(ev, encoding="unicode")
    return f"<?xml version=\"1.0\" encoding=\"UTF-8\" standalone=\"yes\"?>{body}"


# ---------------------------------------------------------------------------
# Self-test.  Run with: python -m app.pipeline.cot
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    from app.schemas import EOObservation, IntelligenceEvent, UASTrack, utc_now

    now = utc_now()
    event = IntelligenceEvent(
        id="evt_demo01",
        timestamp=now,
        site_id="Alpha Site - Forward OP",
        sensor_id="EDGE-ALPHA-01",
        track_id="TRK-DRON-NW-001",
        event_type="POSSIBLE_UAS_ACTIVITY",
        title="Drone-control burst NW sector",
        summary="Repeated burst pattern matched drone_control",
        classification="drone_control_repeated_burst",
        confidence=0.91,
        ood_score=0.18,
        priority="high",
        evidence=["repeated burst", "NW sector", "EO confirmed quadcopter"],
        recommended_action="Increase sensitivity. Cue EO. Authorise mitigation.",
        network_state_at_detection="online",
    )
    track = UASTrack(
        track_id="TRK-DRON-NW-001",
        custody_state="TRACKING",
        threat_level="HIGH",
        classification="CONFIRMED_UAS",
        confidence=0.91,
        sector="NW",
        last_known_lat=34.0522,
        last_known_lon=-118.2437,
        last_known_alt_m=120.0,
        n_detections=6,
        first_seen=now,
        last_seen=now,
        visual_confirmed=True,
        last_eo_obs=EOObservation(
            track_id="TRK-DRON-NW-001",
            sector="NW",
            bearing_deg=314.0,
            slew_time_ms=620,
            frame_kind="quadcopter",
            classification="Group-1 quadrotor UAS",
            confidence=0.88,
            bbox=(0.45, 0.40, 0.10, 0.08),
            confirms_rf=True,
        ),
    )

    print("\n=== CoT XML ===")
    print(build_cot_xml(event, track))
    print("\n=== CoT dict (frontend preview) ===")
    import json
    print(json.dumps(build_cot_dict(event, track), indent=2, default=str))
