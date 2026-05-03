"""Phase E — TDOA (Time-Difference-Of-Arrival) geolocation.

Given three (or more) RF sensors at surveyed positions, each capturing the
same RF burst at slightly different times due to differential range, we
solve for the emitter's position via hyperbolic positioning.

Algorithm
---------
This module implements two related solvers:

  * ``simulate_tdoa(emitter, sensors)`` — forward model: takes a *known*
    emitter position and per-sensor clock jitter, returns the noisy
    TOA / TDOA measurements that a real array would produce. Used by
    the live pipeline to "fake" measurements for the demo (we don't have
    real GPSDO-disciplined sensors). Anyone replacing this with hardware
    simply skips ``simulate_tdoa``.

  * ``solve_tdoa(sensors, tdoa_s, ...)`` — inverse model: takes 3+ sensor
    positions and the TDOAs (s, relative to the first sensor) and returns
    a closed-form-then-iterated estimate of the emitter position with a
    full 2-D covariance matrix. Internally:

      1. Closed-form Chan 1994 estimate as the initial guess.
      2. Two to three Taylor-series Gauss-Newton refinements with the
         identity weight matrix (we treat sensor noise as i.i.d. for the
         demo; real systems use a measurement-covariance weight).
      3. From the converged covariance we derive CEP (the radius of the
         50%-confidence circle around the estimate) and GDOP (geometric
         dilution of precision — a unitless multiplier from clock jitter
         to position error caused purely by sensor geometry).

References
----------
* Y. T. Chan & K. C. Ho, "A simple and efficient estimator for hyperbolic
  location," IEEE Trans. Signal Processing, 1994.
* W. H. Foy, "Position-Location Solutions by Taylor-Series Estimation,"
  IEEE Trans. AES, 1976.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

import numpy as np

from app.schemas import SensorNode, TdoaSolution, utc_now

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------

C_M_PER_S: float = 299_792_458.0
"""Speed of light in m/s."""

_M_PER_DEG_LAT: float = 111_320.0


# ----------------------------------------------------------------------------
# Local-tangent-plane (ENU) helpers
# ----------------------------------------------------------------------------
#
# For a ~10 km AO an equirectangular approximation centred on the sensor
# array's centroid is accurate to better than 0.5 m. Using full ECEF + WGS84
# would be overkill for the demo and harder to reason about.

def _meters_per_deg_lon(lat_deg: float) -> float:
    return _M_PER_DEG_LAT * math.cos(math.radians(lat_deg))


@dataclass
class _LocalFrame:
    """Local tangent plane centred on (lat0, lon0)."""

    lat0: float
    lon0: float

    def to_enu(self, lat: float, lon: float) -> tuple[float, float]:
        """Geodetic → local east, north (metres)."""
        e = (lon - self.lon0) * _meters_per_deg_lon(self.lat0)
        n = (lat - self.lat0) * _M_PER_DEG_LAT
        return e, n

    def to_geo(self, e: float, n: float) -> tuple[float, float]:
        """Local east, north → geodetic lat, lon."""
        lat = self.lat0 + n / _M_PER_DEG_LAT
        lon = self.lon0 + e / max(_meters_per_deg_lon(self.lat0), 1e-3)
        return lat, lon


def _frame_from_sensors(sensors: list[SensorNode]) -> _LocalFrame:
    lat0 = sum(s.lat for s in sensors) / len(sensors)
    lon0 = sum(s.lon for s in sensors) / len(sensors)
    return _LocalFrame(lat0=lat0, lon0=lon0)


# ----------------------------------------------------------------------------
# Forward simulation — emitter at known (lat, lon) → noisy TDOA per sensor
# ----------------------------------------------------------------------------

def simulate_tdoa(
    emitter_lat: float,
    emitter_lon: float,
    sensors: list[SensorNode],
    rng: Optional[np.random.Generator] = None,
) -> tuple[list[float], list[float]]:
    """Synthesise TOA + TDOA measurements for a known emitter.

    Returns ``(toa_s, tdoa_s)`` where:
      * ``toa_s[i]`` is the simulated time-of-arrival at sensor ``i``
        (relative to emit time = 0; only differences matter).
      * ``tdoa_s[i]`` is ``toa_s[i] - toa_s[0]`` for ``i >= 1``, length N-1.

    Each sensor contributes Gaussian clock jitter with σ = ``clock_jitter_ns``.
    """
    if rng is None:
        rng = np.random.default_rng()
    if len(sensors) < 3:
        raise ValueError("Need at least 3 sensors for TDOA")

    frame = _frame_from_sensors(sensors)
    e_pos = np.array(frame.to_enu(emitter_lat, emitter_lon))

    toa: list[float] = []
    for s in sensors:
        s_pos = np.array(frame.to_enu(s.lat, s.lon))
        r = float(np.linalg.norm(e_pos - s_pos))
        true_toa = r / C_M_PER_S
        jitter_s = float(rng.normal(0.0, s.clock_jitter_ns * 1e-9))
        toa.append(true_toa + jitter_s)

    tdoa = [toa[i] - toa[0] for i in range(1, len(toa))]
    return toa, tdoa


# ----------------------------------------------------------------------------
# Inverse: closed-form Chan + Taylor-series refinement
# ----------------------------------------------------------------------------

def _chan_initial(sensors_xy: np.ndarray, rdiff_m: np.ndarray) -> np.ndarray:
    """Closed-form-ish initial position estimate.

    The textbook Chan 1994 stage-1 estimator solves a linear system in
    ``[x, y, r_0]`` and then substitutes the quadratic constraint
    ``r_0² = (x - x_0)² + (y - y_0)²`` to recover ``r_0``. With exactly 3
    sensors that system is under-determined (2 equations, 3 unknowns) and
    needs a Lagrangian solve. To keep the demo robust *and* readable we
    instead:

      1. Anchor the initial guess at the sensors' centroid (always a
         feasible point inside the AO).
      2. Where we *do* have 4+ sensors, fall back to the standard Chan
         linear least-squares so we get a tighter starting point.

    Either way, the heavy lifting is done by the Gauss-Newton refinement
    in :func:`_taylor_refine`.
    """
    if len(sensors_xy) >= 4:
        s0 = sensors_xy[0]
        K0 = float(np.dot(s0, s0))
        A_rows = []
        b = []
        for i in range(1, len(sensors_xy)):
            si = sensors_xy[i]
            Ki = float(np.dot(si, si))
            A_rows.append([
                -2 * (si[0] - s0[0]),
                -2 * (si[1] - s0[1]),
                -2 * rdiff_m[i - 1],
            ])
            b.append(rdiff_m[i - 1] ** 2 + K0 - Ki)
        A = np.array(A_rows)
        b = np.array(b)
        sol, *_ = np.linalg.lstsq(A, b, rcond=None)
        return sol[:2]
    # 3 sensors: anchor at centroid.
    return sensors_xy.mean(axis=0)


def _taylor_refine(
    sensors_xy: np.ndarray,
    rdiff_m: np.ndarray,
    p_init: np.ndarray,
    sigma_m: float,
    max_iter: int = 8,
    tol_m: float = 0.05,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Gauss-Newton refinement of ``p_init``.

    Returns ``(p_estimate_m, covariance_2x2_m2, fit_residual_m)``.
    """
    p = p_init.astype(float).copy()
    s0 = sensors_xy[0]

    cov = np.eye(2) * 1e6
    last_residual = math.inf
    H = np.zeros((len(rdiff_m), 2))

    for _ in range(max_iter):
        r = np.linalg.norm(sensors_xy - p, axis=1)
        if np.any(r < 1e-3):
            break
        r0 = r[0]
        rdiff_pred = r[1:] - r0

        # Jacobian H: shape (N-1, 2). Row i is ∂(r_{i+1} - r_0)/∂p.
        # ∂r_i/∂p = (p - p_i)/r_i.
        H = np.zeros((len(rdiff_pred), 2))
        for i in range(len(rdiff_pred)):
            si = sensors_xy[i + 1]
            H[i] = (p - si) / r[i + 1] - (p - s0) / r0

        residual = rdiff_m - rdiff_pred

        # Identity weight (i.i.d. measurement noise σ_m).
        try:
            HtH = H.T @ H
            dp = np.linalg.solve(HtH, H.T @ residual)
        except np.linalg.LinAlgError:
            break
        p = p + dp
        last_residual = float(np.linalg.norm(residual))
        if float(np.linalg.norm(dp)) < tol_m:
            break

    # Final covariance from the converged Jacobian. Cov = σ²·(HᵀH)⁻¹.
    try:
        cov = (sigma_m ** 2) * np.linalg.inv(H.T @ H)
    except np.linalg.LinAlgError:
        cov = np.eye(2) * 1e6

    return p, cov, last_residual


def _cep_from_cov(cov: np.ndarray) -> float:
    """Convert a 2-D covariance matrix to a 50%-CEP radius (metres).

    Uses the standard approximation
        CEP ≈ 0.589 * (σ_min + σ_max)
    where σ_min, σ_max are the square-roots of the eigenvalues of ``cov``.
    Accurate to ~3 % for elliptical 2-D Gaussians with eccentricity < 3.
    """
    eig = np.linalg.eigvalsh(cov)
    eig = np.maximum(eig, 0.0)
    sig_min, sig_max = math.sqrt(eig.min()), math.sqrt(eig.max())
    return 0.589 * (sig_min + sig_max)


def _gdop(cov: np.ndarray, sigma_m: float) -> float:
    """Geometric dilution of precision = sqrt(trace(cov)) / σ_m."""
    return float(math.sqrt(max(np.trace(cov), 0.0)) / max(sigma_m, 1e-6))


def solve_tdoa(
    sensors: list[SensorNode],
    tdoa_s: list[float],
    sigma_ns: Optional[float] = None,
) -> TdoaSolution:
    """Solve for emitter (lat, lon) given TDOAs at 3+ sensors.

    Args:
        sensors: list of N sensor nodes (N >= 3). ``sensors[0]`` is the
            reference sensor — TDOAs are differences against it.
        tdoa_s: length-(N-1) list of measured time-differences in seconds,
            i.e. ``tdoa_s[i] = toa[i+1] - toa[0]``.
        sigma_ns: per-sensor clock jitter σ in nanoseconds. If ``None``,
            uses the mean of ``sensor.clock_jitter_ns``.

    Returns:
        ``TdoaSolution`` with lat/lon, CEP, residual, GDOP, and a
        2x2 covariance for UI ellipse rendering.
    """
    if len(sensors) < 3:
        raise ValueError("solve_tdoa requires at least 3 sensors")
    if len(tdoa_s) != len(sensors) - 1:
        raise ValueError("len(tdoa_s) must equal len(sensors) - 1")

    if sigma_ns is None:
        sigma_ns = sum(s.clock_jitter_ns for s in sensors) / len(sensors)
    sigma_m = C_M_PER_S * sigma_ns * 1e-9

    frame = _frame_from_sensors(sensors)
    sensors_xy = np.array([frame.to_enu(s.lat, s.lon) for s in sensors])
    rdiff_m = np.array(tdoa_s) * C_M_PER_S

    # Hyperbolic TDOA has phantom-emitter ambiguity: two TDOA equations
    # intersect at *two* points and Gauss-Newton converges to whichever
    # basin the initial guess sits in. We do a *coarse grid pre-search*
    # to find the basin with the smallest TDOA residual, then refine
    # from there. The grid covers a region 3× the sensor extent — enough
    # for emitters that are outside the sensor convex hull.
    centroid = sensors_xy.mean(axis=0)
    extent = float(np.linalg.norm(sensors_xy - centroid, axis=1).max())

    def _residual_at(p: np.ndarray) -> float:
        r = np.linalg.norm(sensors_xy - p, axis=1)
        if np.any(r < 1.0):
            return math.inf
        rdiff_pred = r[1:] - r[0]
        return float(np.linalg.norm(rdiff_m - rdiff_pred))

    grid_radius = 3.0 * extent
    grid_n = 12  # 12x12 = 144 evaluations, ~ms total
    xs = np.linspace(centroid[0] - grid_radius, centroid[0] + grid_radius, grid_n)
    ys = np.linspace(centroid[1] - grid_radius, centroid[1] + grid_radius, grid_n)
    best_grid_p = centroid
    best_grid_res = math.inf
    for x in xs:
        for y in ys:
            r = _residual_at(np.array([x, y]))
            if r < best_grid_res:
                best_grid_res = r
                best_grid_p = np.array([x, y])

    # Refine from a small set of seeds (grid winner + a few priors) and
    # keep whichever final solution has the lowest fit residual.
    candidates = [
        best_grid_p,
        _chan_initial(sensors_xy, rdiff_m),
        centroid,
        sensors_xy[0],
        sensors_xy[1],
        sensors_xy[2],
    ]

    best_p: Optional[np.ndarray] = None
    best_cov: Optional[np.ndarray] = None
    best_residual = math.inf
    for p_init in candidates:
        p_try, cov_try, res_try = _taylor_refine(
            sensors_xy, rdiff_m, np.asarray(p_init, dtype=float), sigma_m
        )
        if not np.all(np.isfinite(cov_try)) or not np.all(np.isfinite(p_try)):
            continue
        if res_try < best_residual:
            best_residual = res_try
            best_p = p_try
            best_cov = cov_try

    if best_p is None or best_cov is None:
        # Total failure — return centroid with a huge CEP so the UI shows
        # "no fix".
        best_p = centroid
        best_cov = np.eye(2) * 1e10
        best_residual = float("inf")

    p_est, cov, residual_m = best_p, best_cov, best_residual

    cep_m = _cep_from_cov(cov)
    gdop = _gdop(cov, sigma_m)

    lat, lon = frame.to_geo(float(p_est[0]), float(p_est[1]))

    return TdoaSolution(
        lat=lat,
        lon=lon,
        alt_m=0.0,
        cep_m=float(cep_m),
        residual_m=float(residual_m),
        sensor_ids=[s.id for s in sensors],
        gdop=float(gdop),
        method="chan_1994",
        timestamp=utc_now(),
        cov_xx=float(cov[0, 0]),
        cov_xy=float(cov[0, 1]),
        cov_yx=float(cov[1, 0]),
        cov_yy=float(cov[1, 1]),
    )


# ----------------------------------------------------------------------------
# Convenience: simulate + solve in one call (the demo's hot path)
# ----------------------------------------------------------------------------

def simulate_and_solve(
    emitter_lat: float,
    emitter_lon: float,
    sensors: list[SensorNode],
    rng: Optional[np.random.Generator] = None,
) -> TdoaSolution:
    """Simulate noisy TDOAs from a known emitter, then run the solver.

    Convenient for the live pipeline: every detected emitter has a "true"
    position the simulator chose, and we re-solve for it through the
    full TDOA chain so the CEP and residual numbers in the UI are *real*
    (not just the truth dressed up).
    """
    _, tdoa_s = simulate_tdoa(emitter_lat, emitter_lon, sensors, rng=rng)
    return solve_tdoa(sensors, tdoa_s)
