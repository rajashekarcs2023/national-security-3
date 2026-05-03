"""Phase E — Blue-Force Feed.

Simulates a small number of friendly units moving across the AO. The
attribution engine queries this feed to "explain away" friendly emissions:
if a detection's best library match is a friendly radio AND a unit
operating that radio is within ``ATTRIBUTION_RADIUS_M`` metres of the
detection, we tag the verdict ``BLUE_ATTRIBUTED``.

In a real deployment this data comes from:
  * TAK Server's blue-force-tracker / PLI feed
  * Iridium Burst (Iridium-9602), ADS-B-on-Mil, Link-16 PPLI
  * Marines' BFT, Army's JBC-P, USAF's SADL

We just fake it with a periodic update loop. Positions are nudged along
each unit's ``heading_deg`` at ``speed_mps``; units move credibly slowly
(0-3 m/s) so the panel never looks like a video game.

Public surface
--------------
* ``BlueForceFeed.start()``                 — kicks off the background loop
* ``BlueForceFeed.stop()``                  — tears it down
* ``BlueForceFeed.units_with_emitter(eid)`` — query helper for attribution
* ``BlueForceFeed.units_within(lat, lon, m)``
* ``BlueForceFeed.snapshot()``              — list[BlueForceUnit] for ws push
"""

from __future__ import annotations

import asyncio
import math
import random
from datetime import datetime
from typing import Callable, Optional

from app.schemas import BlueForceUnit, utc_now
from app.state import STATE

# Approx conversions for short-range geo-math. Good enough for the demo's
# ~10 km AO; we are never going to hit the cosine-singularity zones.
_M_PER_DEG_LAT = 111_320.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres. Fine for ~10 km baselines."""
    R = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def _meters_per_deg_lon(lat: float) -> float:
    return _M_PER_DEG_LAT * math.cos(math.radians(lat))


class BlueForceFeed:
    """Tiny in-process simulator for a friendly-unit position feed.

    Wired to ``STATE.blue_force`` (initialised from ``BLUE_FORCE_SEED``).
    Mutating that dict is how we update positions; this class just owns
    the timing + RNG.
    """

    # 50 % chance per tick that a unit "wanders": small random heading
    # change so positions aren't perfectly straight lines.
    WANDER_CHANCE = 0.5
    # Maximum heading change per tick, in degrees.
    WANDER_DEG = 30.0

    def __init__(
        self,
        on_update: Optional[Callable[[BlueForceUnit], None]] = None,
        update_period_s: float = 5.0,
        rng_seed: int = 7,
    ) -> None:
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self._period_s = update_period_s
        self._rng = random.Random(rng_seed)
        self._on_update = on_update

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------
    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="blue_force_feed")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=self._period_s + 1)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            self._task.cancel()
        self._task = None

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                self._tick()
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=self._period_s)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            return

    # ------------------------------------------------------------------
    # Position update
    # ------------------------------------------------------------------
    def _tick(self) -> None:
        """Step every unit forward and call the per-unit update hook."""
        now = utc_now()
        for unit in list(STATE.blue_force.values()):
            if unit.speed_mps <= 0.0:
                # Stationary OPs still update timestamp so the UI knows the
                # feed is alive.
                unit.last_update = now
                continue

            # Optional small heading drift to make tracks look human.
            if self._rng.random() < self.WANDER_CHANCE:
                unit.heading_deg = (
                    unit.heading_deg + self._rng.uniform(-self.WANDER_DEG, self.WANDER_DEG)
                ) % 360.0

            # Convert heading + speed into a delta-lat / delta-lon for one
            # update period.
            theta = math.radians(unit.heading_deg)
            dx_m = unit.speed_mps * self._period_s * math.sin(theta)   # east
            dy_m = unit.speed_mps * self._period_s * math.cos(theta)   # north
            dlat = dy_m / _M_PER_DEG_LAT
            dlon = dx_m / max(_meters_per_deg_lon(unit.lat), 1e-3)

            unit.lat += dlat
            unit.lon += dlon
            unit.last_update = now

            if self._on_update is not None:
                # Fire-and-forget: the websocket pusher can ignore failures.
                try:
                    self._on_update(unit)
                except Exception:
                    pass

    # ------------------------------------------------------------------
    # Query helpers used by the attribution engine + UI
    # ------------------------------------------------------------------
    def units_with_emitter(self, emitter_id: str) -> list[BlueForceUnit]:
        return [
            u for u in STATE.blue_force.values() if emitter_id in u.active_emitters
        ]

    def units_within(self, lat: float, lon: float, radius_m: float) -> list[BlueForceUnit]:
        out: list[BlueForceUnit] = []
        for u in STATE.blue_force.values():
            if _haversine_m(u.lat, u.lon, lat, lon) <= radius_m:
                out.append(u)
        return out

    def closest_unit_with_emitter(
        self,
        emitter_id: str,
        lat: float,
        lon: float,
    ) -> tuple[Optional[BlueForceUnit], Optional[float]]:
        """Return (unit, distance_m) for the closest friendly unit operating ``emitter_id``."""
        best: Optional[BlueForceUnit] = None
        best_d = math.inf
        for u in self.units_with_emitter(emitter_id):
            d = _haversine_m(u.lat, u.lon, lat, lon)
            if d < best_d:
                best_d = d
                best = u
        if best is None:
            return None, None
        return best, best_d

    def snapshot(self) -> list[BlueForceUnit]:
        return list(STATE.blue_force.values())
