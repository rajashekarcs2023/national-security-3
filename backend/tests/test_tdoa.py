"""Unit tests for the TDOA geolocation solver.

We drive the solver with *synthesised* TOAs from a known emitter position
and then verify the inverse solver recovers that position within a
distance budget. We also verify CEP scales monotonically with clock
jitter (more jitter → wider CEP) and that GDOP is bounded for a sensible
sensor geometry.

No network, no emulator — just numpy + the solver.
"""

from __future__ import annotations

import math
from typing import Iterable

import numpy as np
import pytest

from app.pipeline.tdoa import (
    C_M_PER_S,
    simulate_and_solve,
    simulate_tdoa,
    solve_tdoa,
)
from app.schemas import SensorNode


# ---------------------------------------------------------------------------
# Sensor geometries
# ---------------------------------------------------------------------------

def _square_array(jitter_ns: float = 30.0) -> list[SensorNode]:
    """4 sensors on a ~5 km square around (34.05, -118.22)."""
    return [
        SensorNode(id="A", name="A", lat=34.0500, lon=-118.2500, alt_m=100.0, clock_jitter_ns=jitter_ns),
        SensorNode(id="B", name="B", lat=34.0900, lon=-118.2200, alt_m=100.0, clock_jitter_ns=jitter_ns),
        SensorNode(id="C", name="C", lat=34.0250, lon=-118.2000, alt_m=100.0, clock_jitter_ns=jitter_ns),
        SensorNode(id="D", name="D", lat=34.0700, lon=-118.1700, alt_m=100.0, clock_jitter_ns=jitter_ns),
    ]


def _three_sensor_triangle(jitter_ns: float = 30.0) -> list[SensorNode]:
    """3 sensors in an equilateral-ish triangle."""
    return [
        SensorNode(id="A", name="A", lat=34.0500, lon=-118.2500, alt_m=100.0, clock_jitter_ns=jitter_ns),
        SensorNode(id="B", name="B", lat=34.0900, lon=-118.2200, alt_m=100.0, clock_jitter_ns=jitter_ns),
        SensorNode(id="C", name="C", lat=34.0350, lon=-118.1900, alt_m=100.0, clock_jitter_ns=jitter_ns),
    ]


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6_371_000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# ---------------------------------------------------------------------------
# Forward model sanity
# ---------------------------------------------------------------------------

def test_simulate_tdoa_zero_jitter_is_exact() -> None:
    """With jitter_ns=0 the TDOAs should satisfy (r_i - r_0)/c exactly."""
    sensors = _square_array(jitter_ns=0.0)
    emitter = (34.06, -118.22)
    _, tdoa_s = simulate_tdoa(
        emitter[0], emitter[1], sensors, rng=np.random.default_rng(42),
    )

    # Expected TDOA = (r_i - r_0) / c.
    # Compute ranges in ENU.
    from app.pipeline.tdoa import _frame_from_sensors

    frame = _frame_from_sensors(sensors)
    e_xy = np.array(frame.to_enu(*emitter))
    ranges = [float(np.linalg.norm(np.array(frame.to_enu(s.lat, s.lon)) - e_xy)) for s in sensors]
    expected = [(ranges[i] - ranges[0]) / C_M_PER_S for i in range(1, len(sensors))]

    assert len(tdoa_s) == len(sensors) - 1
    for got, want in zip(tdoa_s, expected):
        assert abs(got - want) < 1e-12, f"zero-jitter TDOA drifted: {got} vs {want}"


# ---------------------------------------------------------------------------
# Inverse model accuracy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "emitter",
    [
        (34.0550, -118.2250),  # near the array centroid
        (34.0600, -118.2100),  # inside convex hull, off-centre
        (34.0400, -118.2350),  # edge of convex hull
    ],
)
def test_square_array_recovers_inside_positions_under_100m(
    emitter: tuple[float, float],
) -> None:
    """Inside the convex hull, mean recovered position must be within 100 m."""
    sensors = _square_array(jitter_ns=30.0)
    errors: list[float] = []
    for seed in range(10):
        rng = np.random.default_rng(seed)
        sol = simulate_and_solve(emitter[0], emitter[1], sensors, rng=rng)
        errors.append(_haversine_m(emitter[0], emitter[1], sol.lat, sol.lon))

    mean_err = float(np.mean(errors))
    assert mean_err < 100.0, f"mean recovery error {mean_err:.1f} m > 100 m budget"


def test_zero_jitter_recovery_is_centimetre_class() -> None:
    """With zero jitter the solver should land < 5 m away from truth."""
    sensors = _square_array(jitter_ns=0.0)
    emitter = (34.0575, -118.2200)
    rng = np.random.default_rng(0)
    sol = simulate_and_solve(emitter[0], emitter[1], sensors, rng=rng)
    err = _haversine_m(emitter[0], emitter[1], sol.lat, sol.lon)
    assert err < 5.0, f"zero-jitter error {err:.2f} m exceeds 5 m"
    assert sol.residual_m < 1e-3


def test_cep_grows_monotonically_with_jitter() -> None:
    """More clock noise → wider CEP, holding geometry constant."""
    emitter = (34.0575, -118.2200)
    ceps: list[float] = []
    for jitter_ns in (10.0, 30.0, 100.0):
        sensors = _square_array(jitter_ns=jitter_ns)
        cep_trials: list[float] = []
        for seed in range(10):
            rng = np.random.default_rng(seed)
            sol = simulate_and_solve(emitter[0], emitter[1], sensors, rng=rng)
            cep_trials.append(sol.cep_m)
        ceps.append(float(np.mean(cep_trials)))

    # Strictly monotone within the 0-100 ns regime.
    assert ceps[0] < ceps[1] < ceps[2], f"CEP not monotonic in jitter: {ceps}"


def test_gdop_is_finite_and_bounded_for_square_array() -> None:
    """Geometry-driven GDOP should be small for a well-spread 4-sensor array."""
    sensors = _square_array(jitter_ns=30.0)
    sol = simulate_and_solve(
        34.0575, -118.2200, sensors, rng=np.random.default_rng(1),
    )
    assert math.isfinite(sol.gdop)
    assert sol.gdop > 0.0
    assert sol.gdop < 10.0, f"GDOP too high for square array: {sol.gdop}"


def test_three_sensor_array_still_recovers_within_hull() -> None:
    """Solver must work with the minimum 3-sensor configuration."""
    sensors = _three_sensor_triangle(jitter_ns=30.0)
    emitter = (34.0575, -118.2200)
    errors = []
    for seed in range(10):
        rng = np.random.default_rng(seed)
        sol = simulate_and_solve(emitter[0], emitter[1], sensors, rng=rng)
        errors.append(_haversine_m(emitter[0], emitter[1], sol.lat, sol.lon))
    # 3-sensor arrays have wider error budget — phantom-ambiguity
    # breaker in the solver uses a grid pre-search.
    assert float(np.mean(errors)) < 300.0


def test_rejects_too_few_sensors() -> None:
    """solve_tdoa must raise with < 3 sensors."""
    sensors = _three_sensor_triangle()[:2]
    with pytest.raises(ValueError):
        solve_tdoa(sensors, tdoa_s=[0.0])


def test_rejects_tdoa_length_mismatch() -> None:
    """tdoa_s must be length (N-1)."""
    sensors = _three_sensor_triangle()
    with pytest.raises(ValueError):
        solve_tdoa(sensors, tdoa_s=[0.0])  # should be 2 entries for 3 sensors


def test_returns_sensor_ids_matching_input() -> None:
    """TdoaSolution.sensor_ids should echo the input array order."""
    sensors = _square_array()
    sol = simulate_and_solve(
        34.06, -118.22, sensors, rng=np.random.default_rng(0),
    )
    assert sol.sensor_ids == [s.id for s in sensors]


def test_covariance_is_symmetric_and_positive_semidefinite() -> None:
    sensors = _square_array(jitter_ns=30.0)
    sol = simulate_and_solve(
        34.06, -118.22, sensors, rng=np.random.default_rng(0),
    )
    cov = np.array([[sol.cov_xx, sol.cov_xy], [sol.cov_yx, sol.cov_yy]])
    # Symmetric (or close — cov_xy and cov_yx are stored independently).
    assert abs(sol.cov_xy - sol.cov_yx) < 1e-6 * max(abs(sol.cov_xx), abs(sol.cov_yy), 1.0)
    # Eigenvalues non-negative.
    eig = np.linalg.eigvalsh(cov)
    assert min(eig) >= -1e-6


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
