"""Travel times on Manhattan's street grid (see docs/DESIGN.md, "Travel time")."""

from __future__ import annotations

import math

GRID_ANGLE_DEG = 29.0  # Manhattan avenues run ~29° east of true north
SPEED_M_PER_MIN = 200.0  # ≈ 12 km/h in Midtown traffic
PARK_MIN = 2.0
M_PER_DEG_LAT = 110_540.0
M_PER_DEG_LON_EQUATOR = 111_320.0


def grid_xy(lat: float, lon: float, lat0: float, lon0: float) -> tuple[float, float]:
    """Metres from (lat0, lon0), rotated so avenues run along the y-axis."""
    x = (lon - lon0) * M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat0))
    y = (lat - lat0) * M_PER_DEG_LAT
    t = math.radians(GRID_ANGLE_DEG)
    return x * math.cos(t) - y * math.sin(t), x * math.sin(t) + y * math.cos(t)


def block_distance_m(
    a: tuple[float, float], b: tuple[float, float], lat0: float, lon0: float
) -> float:
    ax, ay = grid_xy(*a, lat0, lon0)
    bx, by = grid_xy(*b, lat0, lon0)
    return abs(ax - bx) + abs(ay - by)


def straight_distance_m(
    a: tuple[float, float], b: tuple[float, float], lat0: float, lon0: float
) -> float:
    ax, ay = grid_xy(*a, lat0, lon0)
    bx, by = grid_xy(*b, lat0, lon0)
    return math.hypot(ax - bx, ay - by)


def drive_minutes(
    a: tuple[float, float], b: tuple[float, float], lat0: float, lon0: float
) -> float:
    if a == b:
        return 0.0
    return PARK_MIN + block_distance_m(a, b, lat0, lon0) / SPEED_M_PER_MIN
