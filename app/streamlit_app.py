"""Citi Bike rebalancing: live van plan.

Run locally:  uv run --extra app streamlit run app/streamlit_app.py
Deploy: Streamlit Community Cloud, main file app/streamlit_app.py, secret HF_TOKEN.
"""

from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

import pydeck as pdk
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# Streamlit Cloud keeps secrets in st.secrets; the project code reads environment variables.
for key in ("HF_TOKEN", "HF_HISTORY_REPO"):
    try:
        if key in st.secrets and not os.environ.get(key):
            os.environ[key] = str(st.secrets[key])
    except Exception:  # no secrets file when running locally
        pass

from rebalancer.app_core import NY, fetch_live, live_plan, model_card  # noqa: E402
from rebalancer.planner.run import ZONES, hub_file, load_profile  # noqa: E402

st.set_page_config(page_title="Citi Bike van planner", page_icon="🚲", layout="wide")


@st.cache_data(ttl=60, show_spinner="Getting live bike counts…")
def cached_live():
    return fetch_live()


@st.cache_data(ttl=6 * 3600, show_spinner="Loading station averages…")
def cached_profile():
    return load_profile(hub_file("raw/profiles/station_hour.parquet"))


@st.cache_data(ttl=6 * 3600)
def cached_model_card():
    return model_card()


st.title("🚲 Citi Bike van planner")
st.caption(
    "Which stations will run out of bikes or docks in the next hour, and where should "
    "rebalancing vans move bikes to prevent it? Live data from Citi Bike, refreshed every minute."
)

with st.sidebar:
    st.header("Plan settings")
    zone = st.selectbox(
        "Area", sorted(ZONES), index=sorted(ZONES).index("midtown"), format_func=str.title
    )
    vans = st.slider("Vans", 1, 6, 3)
    minutes = st.select_slider(
        "Plan ahead", options=[30, 60, 90], value=60, format_func=lambda m: f"{m} minutes"
    )
    use_model = st.toggle(
        "Use the prediction model",
        value=True,
        help="Off: use plain 12-month averages for this hour and day type.",
    )
    st.button("Refresh now", on_click=st.cache_data.clear)

if not os.environ.get("HF_TOKEN"):
    st.error("This app needs an HF_TOKEN secret to read the station averages and model.")
    st.stop()

try:
    live = cached_live()
    result = live_plan(zone, vans, minutes, use_model, cached_profile(), live, datetime.now(NY))
except Exception as exc:  # show a friendly message rather than a stack trace
    st.error(f"Couldn't build a plan right now: {exc}")
    st.stop()

s = result.score
c1, c2, c3, c4 = st.columns(4)
before, after = s["riders_affected_no_moves"], s["riders_affected_with_plan"]
c1.metric("Riders who'd miss out, no vans", f"{before:.0f}")
c2.metric(
    "With this plan",
    f"{after:.0f}",
    delta=f"{-(before - after):.0f}" if before else None,
    delta_color="inverse",
)
c3.metric("Bikes moved", f"{s['bikes_moved']}")
c4.metric("Van trips", f"{s['trips']}", help=f"{s['stops']} drop-off stops in total")
st.caption(
    f"{len(result.inst.stations)} stations in {zone.title()} at "
    f"{result.now_local:%a %b %d, %H:%M} New York time. Expected riders: "
    f'{result.demand_source}. "Miss out" = would find no bike or no free dock '
    f"(estimate). Planned in {result.plan.solve_seconds:.1f} s."
)

centre, _ = ZONES[zone]
st.pydeck_chart(
    pdk.Deck(
        map_style=None,
        initial_view_state=pdk.ViewState(latitude=centre[0], longitude=centre[1], zoom=13.3),
        layers=[
            pdk.Layer(
                "ScatterplotLayer",
                data=result.stations,
                get_position=["lon", "lat"],
                get_fill_color="colour",
                get_radius=35,
                pickable=True,
                opacity=0.85,
            ),
            pdk.Layer(
                "ArcLayer",
                data=result.arcs,
                get_source_position=["from_lon", "from_lat"],
                get_target_position=["to_lon", "to_lat"],
                get_width="width",
                get_source_color=[30, 90, 200],
                get_target_color=[30, 200, 120],
                pickable=True,
            ),
        ],
        tooltip={"text": "{tip}"},
    ),
    height=520,
)
st.caption(
    "🔴 runs out of bikes · 🟠 fills up · 🟢 fine for the hour · arcs = van moves "
    "(blue = pick up, green = drop off)"
)

st.subheader("Van moves")
if result.moves:
    st.dataframe(result.moves, hide_index=True, use_container_width=True)
else:
    st.info("No moves needed: nothing in this area is expected to run out or fill up.")

card = cached_model_card()
with st.expander("How good is the prediction model?"):
    if card:
        rows = []
        for target, r in card.items():
            for label, m, b in (
                ("all hours", r["model"], r["averages"]),
                ("weekday 7–10am", r["model_rush"], r["averages_rush"]),
            ):
                rows.append(
                    {
                        "Predicting": "pickups" if target == "departures" else "returns",
                        "Hours": label,
                        "Averages error": round(b["mae"], 2),
                        "Model error": round(m["mae"], 2),
                        "Better by": f"{(b['mae'] - m['mae']) / b['mae']:.0%}",
                    }
                )
        st.write(
            "Tested on September 2026, a month the model never saw. Error = average "
            "miss in riders per station per hour."
        )
        st.dataframe(rows, hide_index=True, use_container_width=True)
    else:
        st.write("Model results aren't available right now.")

st.caption(
    "Built with a live recorder (GitHub Actions → Hugging Face), dbt on DuckDB, a "
    "gradient-boosting demand model and an integer-programming van planner. "
    "[Source code](https://github.com/rishabhsrinivasan657-design/citibike-rebalancing)"
)
