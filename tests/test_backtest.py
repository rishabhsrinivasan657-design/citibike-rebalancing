from datetime import UTC, datetime, timedelta

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rebalancer.backtest.replay import Replay, load, max_replay_error, moves_from_plan, simulate
from rebalancer.planner.model import Move

START = datetime(2026, 10, 20, 8, 0)


def replay(**stations) -> Replay:
    """stations: name -> dict(bikes=[...], cap=int, van={t: delta}, dep=rate, arr=rate)."""
    n = len(next(iter(stations.values()))["bikes"])
    rep = Replay(START, n - 1, {}, {}, {}, {}, {}, {})
    for s, d in stations.items():
        rep.names[s] = s
        rep.bikes[s] = d["bikes"]
        rep.cap[s] = [d.get("cap", 20)] * n
        rep.van[s] = [d.get("van", {}).get(t, 0) for t in range(n)]
        rep.hidden_dep[s] = [d.get("dep", 0.0)] * n
        rep.hidden_arr[s] = [d.get("arr", 0.0)] * n
    return rep


def moves(rep: Replay, **by_station) -> dict:
    out = {s: [0] * (rep.minutes + 1) for s in rep.stations}
    for s, changes in by_station.items():
        for t, delta in changes.items():
            out[s][t] = delta
    return out


def test_replaying_what_happened_reproduces_recorded_counts():
    rep = replay(
        v={"bikes": [2, 2, 12, 9, 6, 3], "van": {2: 10}},
        e={"bikes": [0, 0, 1, 0, 0, 0], "dep": 1.0},
    )
    assert max_replay_error(rep) == 0


def test_taking_lyfts_van_away_leaves_riders_without_bikes():
    rep = replay(v={"bikes": [2, 2, 12, 9, 6, 3], "van": {2: 10}})
    assert simulate(rep, rep.van).missed_pickups == 0
    # Without the +10: 2 bikes, then riders take 3 a minute for 3 minutes -> 7 missed.
    assert simulate(rep, None).missed_pickups == pytest.approx(7)


def test_hidden_demand_at_an_empty_station_and_a_delivery_that_fixes_it():
    rep = replay(e={"bikes": [0] * 6, "cap": 10, "dep": 1.0})
    assert simulate(rep, None).missed_pickups == pytest.approx(5)  # 1 a minute, 5 minutes
    ours = simulate(rep, moves(rep, e={1: 10}))
    assert ours.missed_pickups == pytest.approx(1)  # only minute 1, before the van arrived
    assert ours.bikes["e"][-1] == pytest.approx(6)


def test_hidden_returns_at_a_full_station_and_a_pickup_that_frees_docks():
    rep = replay(f={"bikes": [10] * 4, "cap": 10, "arr": 2.0})
    assert simulate(rep, None).missed_returns == pytest.approx(6)
    assert simulate(rep, moves(rep, f={1: -5})).missed_returns == pytest.approx(2)


def test_van_cannot_take_bikes_that_are_not_there():
    rep = replay(s={"bikes": [3, 3, 3]})
    out = simulate(rep, moves(rep, s={1: -15}))
    assert out.van_shortfall == pytest.approx(12)
    assert min(out.bikes["s"]) == 0 and out.missed_pickups == 0


def test_van_cannot_overfill_a_station():
    rep = replay(s={"bikes": [18, 18], "cap": 20})
    out = simulate(rep, moves(rep, s={1: 5}))
    assert out.bikes["s"][-1] == 20 and out.van_shortfall == pytest.approx(3)


def test_plan_moves_land_at_the_minute_the_work_finishes():
    rep = replay(a={"bikes": [5] * 11}, b={"bikes": [5] * 11})
    m = moves_from_plan(rep, [Move(1, 1, "a", "b", 4, 3.2, 7.0, 60)])
    assert m["a"][4] == -4 and m["b"][7] == 4 and sum(m["a"]) == -4


def test_load_reads_tables_assigns_van_minutes_and_hourly_rates(tmp_path):
    t0 = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)  # 8:00 New York
    ts = pa.timestamp("us", tz="UTC")
    bikes = [5, 5, 15, 14, 14]  # +10 van at minute 2, then a rider
    pq.write_table(
        pa.table(
            {
                "station_id": ["a"] * 5,
                "minute": pa.array([t0 + timedelta(minutes=i) for i in range(5)], ts),
                "num_bikes_available": bikes,
                "num_docks_available": [20 - b for b in bikes],
                "recorder_up": [True, True, True, False, True],
            }
        ),
        tmp_path / "minute.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "station_id": ["a"],
                "started_minute": pa.array([t0 + timedelta(minutes=2)], ts),
                "ended_minute": pa.array([t0 + timedelta(minutes=2)], ts),
            }
        ),
        tmp_path / "visits.parquet",
    )
    pq.write_table(
        pa.table(
            {
                "station_id": ["1.01", "1.01"],
                "day_type": ["weekday", "weekday"],
                "hour": [8, 9],
                "departures": [60.0, 120.0],
                "arrivals": [30.0, 0.0],
            }
        ),
        tmp_path / "profile.parquet",
    )
    rep = load(
        tmp_path / "minute.parquet",
        tmp_path / "visits.parquet",
        tmp_path / "profile.parquet",
        ["a"],
        {"a": "1.01"},
        {"a": "A"},
        START,
        4,
        "weekday",
    )
    assert rep.bikes["a"] == bikes and rep.cap["a"] == [20] * 5
    assert rep.van["a"] == [0, 0, 10, 0, 0]
    assert rep.rider("a", 3) == -1
    assert rep.hidden_dep["a"][1] == pytest.approx(1.0) and rep.hidden_arr["a"][1] == 0.5
    assert rep.recorder_gaps == 1
    assert max_replay_error(rep) == 0


def test_only_complete_weekday_mornings_are_verified(tmp_path):
    from rebalancer.backtest.run import recorded_mornings

    ts = pa.timestamp("us", tz="UTC")
    minutes, ups = [], []
    # Mon Oct 19 (complete), Tue Oct 20 (one gap), Sat Oct 24 (weekend): 8:00-8:10 New York
    for day, gap in ((19, None), (20, 8 * 60 + 5), (24, None)):
        for k in range(11):
            local_min = 8 * 60 + k
            minutes.append(datetime(2026, 10, day, 12, 0, tzinfo=UTC) + timedelta(minutes=k))
            ups.append(local_min != gap)
    pq.write_table(
        pa.table({"minute": pa.array(minutes, ts), "recorder_up": ups}),
        tmp_path / "m.parquet",
    )
    assert recorded_mornings(tmp_path / "m.parquet", "08:00", 10) == [datetime(2026, 10, 19, 8, 0)]


def test_combined_table_totals_and_early_evidence_note():
    from rebalancer.backtest.run import render_all

    rows = [
        {
            "date": datetime(2026, 10, 20, 8),
            "No vans at all": 100.0,
            "What actually happened (Lyft's vans)": 100.0,
            "Our plan (planned with averages)": 80.0,
            "Our plan (planned with predictions)": 70.0,
        },
        {
            "date": datetime(2026, 10, 21, 8),
            "No vans at all": 100.0,
            "What actually happened (Lyft's vans)": 90.0,
            "Our plan (planned with averages)": 90.0,
        },
    ]
    text = render_all(rows, "midtown", "08:00")
    assert "| **Total** | **190** | **200** | **170** | **70** |" in text
    assert "| vs no vans (same mornings) | 5% fewer | – | 15% fewer | 30% fewer |" in text
    assert "Only 2 morning(s) so far" in text
    assert "| Wed Oct 21 | 90 | 100 | 90 | – |" in text
