from datetime import datetime
from pathlib import Path

import pytest

from rebalancer.ingest.gbfs import StationInformation, StationStatus

from .test_planner import near

APP = Path(__file__).parents[1] / "app" / "streamlit_app.py"


def fake_live():
    def info(sid, at, short):
        return StationInformation(
            station_id=sid,
            short_name=short,
            name=f"Station {sid}",
            lat=at[0],
            lon=at[1],
            capacity=20,
        )

    def status(sid, bikes):
        return StationStatus(
            station_id=sid,
            num_bikes_available=bikes,
            num_docks_available=20 - bikes,
            is_installed=True,
            is_renting=True,
            is_returning=True,
            last_reported=0,
        )

    return (
        [
            info("a", near(0, 0), "1.01"),
            info("b", near(300, 0), "1.02"),
            info("far", near(9000, 0), "1.03"),
        ],
        [status("a", 1), status("b", 18), status("far", 5)],
    )


PROFILE = [
    {"station_id": "1.01", "day_type": "weekday", "hour": h, "departures": 20.0, "arrivals": 2.0}
    for h in range(24)
]


def test_live_plan_with_model_unavailable_falls_back_to_averages():
    from rebalancer.app_core import NY, live_plan
    from rebalancer.forecast.predict import ModelNotApproved

    def refused(*_):
        raise ModelNotApproved("no")

    out = live_plan(
        "midtown",
        1,
        60,
        True,
        PROFILE,
        fake_live(),
        datetime(2026, 10, 20, 8, 0, tzinfo=NY),
        rates_fn=refused,
    )
    assert "averages" in out.demand_source
    assert {s["name"] for s in out.stations} == {"Station a", "Station b"}
    assert out.moves and out.moves[0]["Drop at"] == "Station a"
    assert out.arcs[0]["tip"].startswith("Van 1, trip 1:")
    a = next(s for s in out.stations if s["name"] == "Station a")
    assert "runs out of bikes" in a["status"] and a["colour"] == [215, 48, 39]


def test_live_plan_uses_predictions_when_available():
    from rebalancer.app_core import NY, live_plan

    def busy(*_):
        return {"1.01": {h: (40.0, 0.0) for h in range(24)}}

    out = live_plan(
        "midtown",
        2,
        60,
        True,
        PROFILE,
        fake_live(),
        datetime(2026, 10, 20, 8, 0, tzinfo=NY),
        rates_fn=busy,
    )
    assert out.demand_source.startswith("Step 4 predictions")
    a = next(s for s in out.stations if s["name"] == "Station a")
    assert a["expected_out"] == 40.0


def test_streamlit_page_renders(monkeypatch):
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    import rebalancer.app_core as core
    import rebalancer.planner.run as planner_run
    from rebalancer.forecast.predict import ModelNotApproved

    def refused(*_):
        raise ModelNotApproved("no")

    monkeypatch.setenv("HF_TOKEN", "test")
    monkeypatch.setattr(core, "fetch_live", fake_live)
    monkeypatch.setattr(core, "model_card", lambda: None)
    monkeypatch.setattr(planner_run, "hub_file", lambda _: Path("unused"))
    monkeypatch.setattr(planner_run, "load_profile", lambda _: PROFILE)
    monkeypatch.setattr(planner_run, "predicted_rates", refused)

    at = AppTest.from_file(str(APP), default_timeout=60).run()
    assert not at.exception, at.exception
    assert at.title[0].value.startswith("🚲 Citi Bike van planner")
    labels = [m.label for m in at.metric]
    assert "Riders who'd miss out, no vans" in labels and "Van trips" in labels
    assert at.dataframe  # the move list
