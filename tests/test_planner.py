import math
import random

import pytest

from rebalancer.planner.geo import block_distance_m, drive_minutes, straight_distance_m
from rebalancer.planner.model import Instance, Move, Params, Plan, Station
from rebalancer.planner.schedule import plan, score, verify

DEPOT = (40.7536, -73.9832)  # Bryant Park
AVENUE = (math.sin(math.radians(29)), math.cos(math.radians(29)))  # unit vector up an avenue


def near(dx_m: float, dy_m: float) -> tuple[float, float]:
    """A point dx metres east and dy metres north of the depot."""
    lat = DEPOT[0] + dy_m / 110_540
    lon = DEPOT[1] + dx_m / (111_320 * math.cos(math.radians(DEPOT[0])))
    return lat, lon


def st(sid, at, bikes, docks, dep=0.0, arr=0.0):
    return Station(sid, sid, *at, bikes=bikes, docks=docks, departures=dep, arrivals=arr)


def run(stations, **params):
    inst = Instance(stations, DEPOT, Params(**params))
    result = plan(inst)  # plan() raises if any limit is broken
    return inst, result


# --- travel times -----------------------------------------------------------------


def test_along_an_avenue_block_distance_equals_straight_line():
    a = near(0, 0)
    b = near(800 * AVENUE[0], 800 * AVENUE[1])
    assert block_distance_m(a, b, *DEPOT) == pytest.approx(800, rel=0.01)
    assert straight_distance_m(a, b, *DEPOT) == pytest.approx(800, rel=0.01)


def test_diagonal_trip_is_longer_than_straight_line():
    a, b = near(0, 0), near(500, 500)
    ratio = block_distance_m(a, b, *DEPOT) / straight_distance_m(a, b, *DEPOT)
    assert 1.05 < ratio <= math.sqrt(2) + 1e-9


def test_drive_minutes_includes_parking():
    assert drive_minutes(DEPOT, DEPOT, *DEPOT) == 0
    assert drive_minutes(near(0, 0), near(0, 1), *DEPOT) == pytest.approx(2.0, abs=0.05)


# --- planner behaviour --------------------------------------------------------------


def test_moves_bikes_from_quiet_station_to_one_about_to_run_empty():
    inst, result = run(
        [st("full", near(200, 0), bikes=20, docks=5), st("empty", near(-200, 0), 1, 24, dep=15)],
        vans=1,
    )
    assert [(m.from_id, m.to_id) for m in result.moves] == [("full", "empty")]
    s = score(inst, result)
    assert s["no_bike_with_plan"] < s["no_bike_no_moves"]


def test_no_moves_when_nothing_is_wrong():
    _, result = run([st("a", near(0, 0), 10, 10), st("b", near(300, 0), 10, 10)])
    assert result.moves == [] and result.status == "Nothing to fix"


def test_van_capacity_splits_big_transfers():
    _, result = run(
        [st("donor", near(100, 0), 60, 0), st("needy", near(-100, 0), 0, 60, dep=45)], vans=2
    )
    assert result.moves and all(m.bikes <= 25 for m in result.moves)
    assert sum(m.bikes for m in result.moves) > 25


def test_cannot_take_more_bikes_than_the_donor_has():
    _, result = run([st("donor", near(100, 0), 3, 20), st("needy", near(-100, 0), 0, 20, dep=10)])
    assert sum(m.bikes for m in result.moves) <= 3


def test_cannot_drop_more_than_free_docks():
    _, result = run([st("donor", near(100, 0), 30, 0), st("needy", near(-100, 0), 0, 4, dep=20)])
    assert 0 < sum(m.bikes for m in result.moves) <= 4


def test_overflowing_station_gets_bikes_taken_away():
    inst, result = run(
        [st("overflow", near(100, 0), 18, 2, arr=12), st("roomy", near(-150, 0), 5, 25)]
    )
    assert result.moves and result.moves[0].from_id == "overflow"
    s = score(inst, result)
    assert s["no_dock_with_plan"] < s["no_dock_no_moves"]


def test_no_move_if_it_cannot_arrive_within_the_shift():
    # ~16 km of city blocks away: no van can get there within 60 minutes.
    _, result = run([st("donor", near(0, 100), 30, 0), st("far", near(6000, 6000), 1, 20, dep=60)])
    assert result.moves == []


def test_late_delivery_still_counts_for_later_riders():
    # Empties almost at once, but bikes arriving ~15 min later still help everyone after.
    inst, result = run(
        [st("donor", near(0, 300), 25, 0), st("needy", near(0, -300), 0, 25, dep=30)]
    )
    s = score(inst, result)
    assert result.moves and s["no_bike_with_plan"] < s["no_bike_no_moves"]


def test_simulation_counts_missed_riders():
    from rebalancer.planner.schedule import simulate

    inst = Instance([st("a", near(0, 0), 5, 5, dep=20), st("b", near(9, 9), 9, 1, arr=10)], DEPOT)
    out = simulate(inst, [])
    assert out["missed_pickups"] == pytest.approx(15, abs=0.5)  # 20 wanted, 5 there
    assert out["missed_returns"] == pytest.approx(9, abs=0.5)  # 10 arrive, 1 free dock


def test_more_urgent_station_is_served_first():
    _, result = run(
        [
            st("donor", near(0, 0), 25, 0),
            st("soon", near(300, 0), 2, 20, dep=30),  # empty in ~4 min
            st("later", near(-300, 0), 8, 20, dep=12),  # empty in ~40 min
        ],
        vans=1,
    )
    assert result.moves[0].to_id == "soon"


def test_verify_catches_broken_plans():
    inst = Instance([st("a", near(0, 0), 5, 0), st("b", near(100, 0), 0, 3, dep=10)], DEPOT)
    bad = Plan(
        moves=[Move(1, 1, "a", "b", 30, 1.0, 1.5, 60.0)], dropped=[], solve_seconds=0, status=""
    )
    problems = " ".join(verify(inst, bad))
    assert "carries 30 bikes" in problems
    assert "only 5 there" in problems
    assert "only 3 free docks" in problems
    assert "too optimistic" in problems


@pytest.mark.parametrize("seed", range(15))
def test_random_instances_always_valid_and_never_worse(seed):
    rng = random.Random(seed)
    stations = []
    for k in range(rng.randint(5, 25)):
        cap = rng.choice([19, 23, 27, 31, 39])
        bikes = rng.randint(0, cap)
        stations.append(
            st(
                f"s{k}",
                near(rng.uniform(-1200, 1200), rng.uniform(-1200, 1200)),
                bikes,
                cap - bikes,
                dep=rng.uniform(0, 25),
                arr=rng.uniform(0, 25),
            )
        )
    inst, result = run(stations, vans=rng.randint(1, 4))
    s = score(inst, result)
    assert s["riders_affected_with_plan"] <= s["riders_affected_no_moves"] + 0.5
    assert result.solve_seconds < 30


def test_expected_counts_prorates_partial_hours():
    from datetime import datetime

    from rebalancer.planner.run import expected_counts

    rates = {7: (60.0, 0.0), 8: (120.0, 30.0)}
    dep, arr = expected_counts(rates, datetime(2000, 1, 3, 7, 30), 60)
    assert dep == pytest.approx(30 + 60) and arr == pytest.approx(15)


def test_build_instance_filters_zone_and_closed_stations():
    from datetime import datetime

    from rebalancer.ingest.gbfs import StationInformation, StationStatus
    from rebalancer.planner.run import build_instance, render

    def info(sid, at, short):
        return StationInformation(
            station_id=sid, short_name=short, name=f"S {sid}", lat=at[0], lon=at[1], capacity=20
        )

    def status(sid, bikes, renting=True):
        return StationStatus(
            station_id=sid,
            num_bikes_available=bikes,
            num_docks_available=20 - bikes,
            is_installed=True,
            is_renting=renting,
            is_returning=True,
            last_reported=0,
        )

    infos = [
        info("a", near(0, 0), "1.01"),
        info("b", near(300, 0), "1.02"),
        info("far", near(5000, 0), "1.03"),
        info("closed", near(0, 300), "1.04"),
    ]
    statuses = [status("a", 1), status("b", 18), status("far", 5), status("closed", 5, False)]
    profile = [
        {
            "station_id": "1.01",
            "day_type": "weekday",
            "hour": 8,
            "departures": 20.0,
            "arrivals": 2.0,
        }
    ]
    inst = build_instance(
        profile, infos, statuses, "midtown", datetime(2000, 1, 3, 8, 0), "weekday", Params(vans=1)
    )
    assert sorted(s.id for s in inst.stations) == ["a", "b"]
    a = inst.by_id()["a"]
    assert a.departures == pytest.approx(20) and a.arrivals == pytest.approx(2)
    report = render(inst, plan(inst), datetime(2000, 1, 3, 8, 0))
    assert "| 1 | 1 | S b | S a |" in report and "Van plan: Midtown, weekday 08:00–09:00" in report


def test_recorded_snapshot_reads_the_right_minute_and_station_version(tmp_path):
    from datetime import UTC, datetime

    import pyarrow as pa
    import pyarrow.parquet as pq

    from rebalancer.planner.run import recorded_snapshot

    noon_utc = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)  # 8:00 New York (EDT)
    ts = pa.timestamp("us", tz="UTC")
    pq.write_table(
        pa.table(
            {
                "station_id": ["a", "a", "b"],
                "minute": pa.array([noon_utc, noon_utc.replace(minute=1), noon_utc], ts),
                "num_bikes_available": [7, 9, 3],
                "num_ebikes_available": [1, 1, 0],
                "num_bikes_disabled": [0, 0, 1],
                "num_docks_available": [13, 11, 16],
                "num_docks_disabled": [0, 0, 0],
                "is_renting": [True, True, True],
                "is_returning": [True, True, True],
                "recorder_up": [True, True, True],
                "last_reported": pa.array([noon_utc] * 3, ts),
            }
        ),
        tmp_path / "minute.parquet",
    )
    early, late = datetime(2026, 10, 19, tzinfo=UTC), datetime(2026, 10, 20, 11, tzinfo=UTC)
    pq.write_table(
        pa.table(
            {
                "station_id": ["a", "a", "b"],
                "short_name": ["1.01", "1.01", "1.02"],
                "name": ["A old", "A new", "B"],
                "lat": [40.75] * 3,
                "lon": [-73.98] * 3,
                "region_id": ["71"] * 3,
                "capacity": [18, 20, 20],
                "valid_from": pa.array([early, late, early], ts),
                "valid_to": pa.array([late, None, None], ts),
            }
        ),
        tmp_path / "stations.parquet",
    )
    info, status = recorded_snapshot(
        tmp_path / "minute.parquet", tmp_path / "stations.parquet", datetime(2026, 10, 20, 8, 0)
    )
    assert {s.station_id: s.num_bikes_available for s in status} == {"a": 7, "b": 3}
    assert {i.station_id: i.name for i in info} == {"a": "A new", "b": "B"}


def test_saved_instance_round_trips(tmp_path):
    from datetime import datetime

    from rebalancer.planner.run import load_instance, save_instance

    inst = Instance(
        [st("a", near(0, 0), 3, 17, dep=12.5), st("b", near(200, 0), 15, 5)], DEPOT, Params(vans=2)
    )
    save_instance(inst, datetime(2026, 10, 20, 8), "test", tmp_path / "i.json")
    back, start, source = load_instance(tmp_path / "i.json")
    assert back == inst and start == datetime(2026, 10, 20, 8) and source == "test"
