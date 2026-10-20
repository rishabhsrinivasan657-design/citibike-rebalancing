"""Train the station-hour demand model (Step 4). Runs on GitHub (train.yml).

    forecast-train --raw ./work/raw --out ./work/models

Writes demand.joblib (two Poisson gradient-boosting models + metadata),
profile_features.parquet (the per-station averages the model takes as input) and
metrics.md. See docs/DESIGN.md, "Demand model".
"""

from __future__ import annotations

import argparse
import json
import os
from datetime import UTC, datetime
from pathlib import Path

import joblib
import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor
from sklearn.metrics import mean_absolute_error, mean_poisson_deviance

from rebalancer.backfill import db

FEATURES = [
    "avg_departures",
    "avg_arrivals",
    "hour",
    "dow",
    "is_holiday",
    "daylight_hours",
    "temp_c",
    "precip_mm",
    "snow_cm",
    "wind_kmh",
]
NYC_LAT = 40.78
# Daylight hours from the day of year (standard declination approximation). Used
# instead of month: the test month (September) never appears in training, but its
# daylight does (it matches early April).
DAYLIGHT_SQL = (
    f"24 / pi() * acos(-tan(radians({NYC_LAT})) * tan(radians(23.44 * sin(2 * pi() * "
    "(284 + dayofyear({col})) / 365))))"
)
TRAIN_END = "2026-08-01"  # train before this; August validates; September tests
TEST_START, TEST_END = "2026-09-01", "2026-10-01"


def build_table(con, raw: Path) -> None:
    """Station-hour table with counts (zeros included) and every non-profile feature."""
    trips = (raw / "trips").as_posix() + "/month=*/trips.parquet"
    weather = (raw / "weather" / "forecast.parquet").as_posix()
    holidays = (raw / "holidays" / "holidays.parquet").as_posix()
    con.execute(
        f"""
        CREATE TABLE station_hour AS
        WITH rides AS (
            SELECT DISTINCT ON (ride_id) started_at, ended_at, start_station_id, end_station_id
            FROM read_parquet('{trips}')
        ),
        events AS (
            SELECT start_station_id AS short_name, date_trunc('hour', started_at) AS hour_local,
                   1 AS dep, 0 AS arr
            FROM rides WHERE start_station_id IS NOT NULL
            UNION ALL
            SELECT end_station_id, date_trunc('hour', ended_at), 0, 1
            FROM rides WHERE end_station_id IS NOT NULL
        ),
        counts AS (
            SELECT short_name, hour_local, sum(dep) AS departures, sum(arr) AS arrivals
            FROM events GROUP BY ALL
        ),
        active AS (
            SELECT short_name, min(hour_local) AS first_h, max(hour_local) AS last_h
            FROM counts GROUP BY 1
        ),
        grid AS (
            SELECT short_name, unnest(range(first_h, last_h + INTERVAL 1 HOUR, INTERVAL 1 HOUR))
                AS hour_local
            FROM active
        ),
        hol AS (SELECT date FROM read_parquet('{holidays}')),
        wx AS (SELECT * FROM read_parquet('{weather}'))
        SELECT
            g.short_name,
            g.hour_local,
            coalesce(c.departures, 0)::FLOAT AS departures,
            coalesce(c.arrivals, 0)::FLOAT AS arrivals,
            hour(g.hour_local) AS hour,
            dayofweek(g.hour_local) AS dow,
            (g.hour_local::DATE IN (SELECT date FROM hol))::INT AS is_holiday,
            {DAYLIGHT_SQL.format(col="g.hour_local")} AS daylight_hours,
            CASE WHEN dayofweek(g.hour_local) IN (0, 6)
                      OR g.hour_local::DATE IN (SELECT date FROM hol)
                 THEN 'weekend' ELSE 'weekday' END AS day_type,
            w.temperature_2m AS temp_c,
            w.precipitation AS precip_mm,
            w.snowfall AS snow_cm,
            w.wind_speed_10m AS wind_kmh
        FROM grid AS g
        LEFT JOIN counts AS c USING (short_name, hour_local)
        LEFT JOIN wx AS w ON w.time_utc = timezone('America/New_York', g.hour_local)
        """
    )


def profile_sql(where: str) -> str:
    """Average departures/arrivals per (station, day type, hour) over the given rows."""
    return f"""
        SELECT short_name, day_type, hour,
               avg(departures) AS avg_departures, avg(arrivals) AS avg_arrivals
        FROM station_hour WHERE {where} GROUP BY ALL
    """


def matrix(con, profile_table: str, where: str, sample: int | None = None):
    sample_sql = f"USING SAMPLE {sample} ROWS (reservoir, 7)" if sample else ""
    cols = ", ".join(f"coalesce({f}, 0)::DOUBLE AS {f}" for f in FEATURES)
    data = con.execute(
        f"""
        SELECT {cols}, s.departures, s.arrivals,
               (s.hour BETWEEN 7 AND 9 AND s.day_type = 'weekday')::INT AS rush
        FROM (SELECT * FROM station_hour WHERE {where} {sample_sql}) AS s
        LEFT JOIN {profile_table} AS p USING (short_name, day_type, hour)
        """
    ).fetchnumpy()
    x = np.column_stack([data[f] for f in FEATURES])
    return x, data["departures"], data["arrivals"], data["rush"].astype(bool)


MODEL_PARAMS = {
    "loss": "poisson",
    "max_iter": 500,
    "learning_rate": 0.05,
    "max_leaf_nodes": 31,
    "min_samples_leaf": 200,
    "l2_regularization": 10.0,
    "early_stopping": True,
    "validation_fraction": 0.1,
    "n_iter_no_change": 25,
    "random_state": 7,
}


def fit(x: np.ndarray, y: np.ndarray) -> HistGradientBoostingRegressor:
    # Poisson boosting is unstable on zero-heavy counts without regularisation: rows
    # predicted near zero have near-zero curvature, so a later leaf mixing them with
    # positive counts can take an enormous step (the first run predicted ~1e71).
    # l2_regularization and large leaves bound those steps.
    return HistGradientBoostingRegressor(**MODEL_PARAMS).fit(x, y)


def approved(metrics: dict) -> bool:
    """A model may be used only if it beats the averages on the test month, overall and
    in the morning rush, for both pickups and returns."""
    return bool(metrics) and all(
        r["model"]["mae"] < r["averages"]["mae"]
        and r["model_rush"]["mae"] < r["averages_rush"]["mae"]
        for r in metrics.values()
    )


def scores(y: np.ndarray, pred: np.ndarray) -> dict[str, float]:
    return {
        "mae": float(mean_absolute_error(y, pred)),
        "poisson_deviance": float(mean_poisson_deviance(y, np.clip(pred, 1e-6, None))),
    }


def evaluate(con, sample: int) -> tuple[dict, str]:
    con.execute(
        f"CREATE OR REPLACE TABLE profile_train AS {profile_sql(f"hour_local < '{TRAIN_END}'")}"
    )
    x_tr, dep_tr, arr_tr, _ = matrix(con, "profile_train", f"hour_local < '{TRAIN_END}'", sample)
    test_where = f"hour_local >= '{TEST_START}' AND hour_local < '{TEST_END}'"
    x_te, dep_te, arr_te, rush = matrix(con, "profile_train", test_where)
    base_col = {
        "departures": FEATURES.index("avg_departures"),
        "arrivals": FEATURES.index("avg_arrivals"),
    }
    results = {}
    for target, y_tr, y_te in (("departures", dep_tr, dep_te), ("arrivals", arr_tr, arr_te)):
        model = fit(x_tr, y_tr)
        pred = model.predict(x_te)
        base = x_te[:, base_col[target]]
        results[target] = {
            "mean_actual": float(y_te.mean()),
            "mean_predicted": float(pred.mean()),
            "max_predicted": float(pred.max()),
            "model": scores(y_te, pred),
            "averages": scores(y_te, base),
            "model_rush": scores(y_te[rush], pred[rush]),
            "averages_rush": scores(y_te[rush], base[rush]),
            "iterations": int(model.n_iter_),
            "test_rows": int(len(y_te)),
        }
    return results, render(results, len(x_tr))


def render(results: dict, train_rows: int) -> str:
    lines = [
        "## Demand model: September 2026 test (never seen in training)",
        "",
        f"Trained on {train_rows:,} sampled station-hours (Oct 2025 – Jul 2026). Lower is better.",
        "",
        "| Predicting | Hours | Averages MAE | Model MAE | Change "
        "| Averages deviance | Model deviance |",
        "|---|---|---|---|---|---|---|",
    ]
    for target, r in results.items():
        for label, m, b in (
            ("all", r["model"], r["averages"]),
            ("weekday 7–10am", r["model_rush"], r["averages_rush"]),
        ):
            change = (b["mae"] - m["mae"]) / b["mae"]
            verdict = f"{change:.1%} better" if change >= 0 else f"{-change:.1%} worse"
            lines.append(
                f"| {target} | {label} | {b['mae']:.3f} | {m['mae']:.3f} | {verdict} "
                f"| {b['poisson_deviance']:.3f} | {m['poisson_deviance']:.3f} |"
            )
    lines += [
        "",
        "| Predicting | Mean actual | Mean predicted | Largest prediction |",
        "|---|---|---|---|",
    ]
    for target, r in results.items():
        lines.append(
            f"| {target} | {r['mean_actual']:.3f} | {r['mean_predicted']:.3f} "
            f"| {r['max_predicted']:.1f} |"
        )
    verdict = (
        "**Approved**: beats the averages."
        if approved(results)
        else (
            "**Not approved**: doesn't beat the averages everywhere, so the planner keeps "
            "using averages."
        )
    )
    lines += ["", verdict]
    return "\n".join(lines)


def train_production(con, out: Path, sample: int, metrics: dict) -> None:
    """Refit on all 12 months with the full-year averages, for live use."""
    con.execute(f"CREATE OR REPLACE TABLE profile_all AS {profile_sql('true')}")
    x, dep, arr, _ = matrix(con, "profile_all", "true", sample)
    bundle = {
        "departures": fit(x, dep),
        "arrivals": fit(x, arr),
        "features": FEATURES,
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "metrics": metrics,
        "approved": approved(metrics),
        "max_target": float(max(dep.max(), arr.max())),
    }
    out.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, out / "demand.joblib", compress=3)
    con.execute(
        f"COPY profile_all TO '{(out / 'profile_features.parquet').as_posix()}' (FORMAT parquet)"
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=Path("work/raw"))
    parser.add_argument("--out", type=Path, default=Path("work/models"))
    parser.add_argument("--sample", type=int, default=6_000_000)
    args = parser.parse_args()

    con = db.connect()
    build_table(con, args.raw)
    metrics, report = evaluate(con, args.sample)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(report + "\n")
    if not approved(metrics):
        raise SystemExit("model not approved: it doesn't beat the averages; nothing saved")
    train_production(con, args.out, args.sample, metrics)
    (args.out / "metrics.md").write_text(report + "\n")
    (args.out / "metrics.json").write_text(json.dumps(metrics, indent=2))


if __name__ == "__main__":
    main()
