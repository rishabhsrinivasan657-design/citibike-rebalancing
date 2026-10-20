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

FEATURES_V1 = [
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
# v2 adds inputs that also exist live: day-of-week averages (Monday vs Friday), the
# station's level over the most recent 3 months, and its size; and weights recent
# training rows more (half-weight after 120 days), because v1 under-predicted busy
# months. "Same hour last week" is left out on purpose: last week's trips aren't
# published until weeks later, so the live app couldn't supply it.
FEATURES_V2 = FEATURES_V1 + [
    "dow_avg_departures",
    "dow_avg_arrivals",
    "recent_avg_departures",
    "recent_avg_arrivals",
    "capacity",
]
FEATURES = FEATURES_V1  # kept for older imports
VARIANTS = {
    "v1": {"features": FEATURES_V1, "half_life_days": None},
    "v2": {"features": FEATURES_V2, "half_life_days": 120},
}
RECENT_DAYS = 90
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


def build_capacity(con, lake: Path | None) -> None:
    """Latest dock count per station (trip-file ID), from the recorder's station list."""
    path = lake / "station_information" if lake else None
    if path is not None and path.exists():
        glob = path.as_posix() + "/date=*/hour=*/*.parquet"
        con.execute(
            f"""CREATE OR REPLACE TABLE capacity AS
                SELECT short_name, arg_max(capacity, polled_at)::DOUBLE AS capacity
                FROM read_parquet('{glob}', hive_partitioning = true, union_by_name = true)
                WHERE short_name IS NOT NULL GROUP BY 1"""
        )
    else:
        con.execute("CREATE OR REPLACE TABLE capacity (short_name VARCHAR, capacity DOUBLE)")


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


def build_profiles(con, name: str, where: str, end: str) -> None:
    """Averages the model takes as inputs, from the given rows only (training months
    when testing, so the test month never leaks in)."""
    con.execute(f"CREATE OR REPLACE TABLE prof_{name} AS {profile_sql(where)}")
    con.execute(
        f"""CREATE OR REPLACE TABLE dow_{name} AS
            SELECT short_name, dow, hour, avg(departures) AS dow_avg_departures,
                   avg(arrivals) AS dow_avg_arrivals
            FROM station_hour WHERE {where} AND is_holiday = 0 GROUP BY ALL"""
    )
    con.execute(
        f"""CREATE OR REPLACE TABLE recent_{name} AS
            SELECT short_name, day_type, hour, avg(departures) AS recent_avg_departures,
                   avg(arrivals) AS recent_avg_arrivals
            FROM station_hour
            WHERE {where} AND hour_local >= TIMESTAMP '{end}' - INTERVAL {RECENT_DAYS} DAY
            GROUP BY ALL"""
    )


def matrix(
    con,
    profiles: str,
    where: str,
    features: list[str],
    sample: int | None = None,
    weight_ref: str | None = None,
    half_life_days: float | None = None,
):
    sample_sql = f"USING SAMPLE {sample} ROWS (reservoir, 7)" if sample else ""
    # Missing averages mean "no history": 0. Missing size stays missing (trees handle it).
    cols = ", ".join(
        f"{f}::DOUBLE AS {f}" if f == "capacity" else f"coalesce({f}, 0)::DOUBLE AS {f}"
        for f in features
    )
    weight = (
        f"pow(0.5, date_diff('day', s.hour_local, TIMESTAMP '{weight_ref}') / {half_life_days})"
        if half_life_days
        else "1.0"
    )
    data = con.execute(
        f"""
        SELECT {cols}, s.departures, s.arrivals,
               (s.hour BETWEEN 7 AND 9 AND s.day_type = 'weekday')::INT AS rush,
               {weight}::DOUBLE AS weight
        FROM (SELECT * FROM station_hour WHERE {where} {sample_sql}) AS s
        LEFT JOIN prof_{profiles} USING (short_name, day_type, hour)
        LEFT JOIN dow_{profiles} USING (short_name, dow, hour)
        LEFT JOIN recent_{profiles} USING (short_name, day_type, hour)
        LEFT JOIN capacity USING (short_name)
        """
    ).fetchnumpy()
    x = np.column_stack([np.asarray(data[f], dtype=float) for f in features])
    return (
        x,
        data["departures"],
        data["arrivals"],
        data["rush"].astype(bool),
        np.asarray(data["weight"], dtype=float),
    )


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


def fit(x: np.ndarray, y: np.ndarray, w: np.ndarray | None = None) -> HistGradientBoostingRegressor:
    # Poisson boosting is unstable on zero-heavy counts without regularisation: rows
    # predicted near zero have near-zero curvature, so a later leaf mixing them with
    # positive counts can take an enormous step (the first run predicted ~1e71).
    # l2_regularization and large leaves bound those steps.
    return HistGradientBoostingRegressor(**MODEL_PARAMS).fit(x, y, sample_weight=w)


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


def evaluate(con, sample: int) -> tuple[dict, str | None, str]:
    """Test every variant on September; returns (results per variant, chosen, report)."""
    build_profiles(con, "train", f"hour_local < '{TRAIN_END}'", TRAIN_END)
    test_where = f"hour_local >= '{TEST_START}' AND hour_local < '{TEST_END}'"
    results: dict[str, dict] = {}
    train_rows = 0
    for name, v in VARIANTS.items():
        feats = v["features"]
        x_tr, dep_tr, arr_tr, _, w_tr = matrix(
            con,
            "train",
            f"hour_local < '{TRAIN_END}'",
            feats,
            sample,
            TRAIN_END,
            v["half_life_days"],
        )
        x_te, dep_te, arr_te, rush, _ = matrix(con, "train", test_where, feats)
        train_rows = len(x_tr)
        results[name] = {}
        for target, y_tr, y_te in (("departures", dep_tr, dep_te), ("arrivals", arr_tr, arr_te)):
            model = fit(x_tr, y_tr, w_tr)
            pred = model.predict(x_te)
            base = x_te[:, feats.index(f"avg_{target}")]
            results[name][target] = {
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
    chosen = choose(results)
    return results, chosen, render(results, chosen, train_rows)


def choose(results: dict) -> str | None:
    """Best approved variant by morning-rush error (averaged over pickups and returns).
    v1 wins ties, so a change has to earn its place."""
    ok = {
        name: sum(r["model_rush"]["mae"] for r in m.values()) / len(m)
        for name, m in results.items()
        if approved(m)
    }
    return min(ok, key=lambda n: (ok[n], n)) if ok else None


def render(results: dict, chosen: str | None, train_rows: int) -> str:
    names = list(results)
    lines = [
        "## Demand model: September 2026 test (never seen in training)",
        "",
        f"Trained on {train_rows:,} sampled station-hours (Oct 2025 – Jul 2026). "
        "Error = average miss in riders per station per hour; lower is better.",
        "",
        "| Predicting | Hours | Averages | " + " | ".join(names) + " |",
        "|---|---|---|" + "---|" * len(names),
    ]
    first = results[names[0]]
    for target in first:
        for label, key, base_key in (
            ("all", "model", "averages"),
            ("weekday 7–10am", "model_rush", "averages_rush"),
        ):
            base = first[target][base_key]["mae"]
            cells = []
            for n in names:
                m = results[n][target][key]["mae"]
                cells.append(f"{m:.3f} ({(base - m) / base:+.1%})")
            lines.append(f"| {target} | {label} | {base:.3f} | " + " | ".join(cells) + " |")
    lines += [
        "",
        "| Variant | Predicting | Mean actual | Mean predicted | Largest |",
        "|---|---|---|---|---|",
    ]
    for n in names:
        for target, r in results[n].items():
            lines.append(
                f"| {n} | {target} | {r['mean_actual']:.3f} | {r['mean_predicted']:.3f} "
                f"| {r['max_predicted']:.1f} |"
            )
    if chosen:
        lines += [
            "",
            f"**Chosen: {chosen}** (approved: beats the averages; best in the morning rush).",
        ]
    else:
        lines += [
            "",
            "**Not approved**: no variant beats the averages everywhere, so the "
            "planner keeps using averages.",
        ]
    return "\n".join(lines)


def train_production(con, out: Path, sample: int, variant: str, metrics: dict) -> None:
    """Refit the chosen variant on all 12 months, with averages from all months."""
    feats, half_life = VARIANTS[variant]["features"], VARIANTS[variant]["half_life_days"]
    (data_end,) = con.execute(
        "SELECT strftime(max(hour_local) + INTERVAL 1 HOUR, '%Y-%m-%d %H:%M:%S') FROM station_hour"
    ).fetchone()
    build_profiles(con, "all", "true", data_end)
    x, dep, arr, _, w = matrix(con, "all", "true", feats, sample, data_end, half_life)
    bundle = {
        "departures": fit(x, dep, w),
        "arrivals": fit(x, arr, w),
        "variant": variant,
        "features": feats,
        "trained_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "metrics": metrics,
        "approved": approved(metrics),
        "max_target": float(max(dep.max(), arr.max())),
    }
    out.mkdir(parents=True, exist_ok=True)
    joblib.dump(bundle, out / "demand.joblib", compress=3)
    for table, filename in (
        ("prof_all", "profile_features"),
        ("dow_all", "profile_dow"),
        ("recent_all", "profile_recent"),
        ("capacity", "capacity"),
    ):
        con.execute(f"COPY {table} TO '{(out / filename).as_posix()}.parquet' (FORMAT parquet)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--raw", type=Path, default=Path("work/raw"))
    parser.add_argument("--out", type=Path, default=Path("work/models"))
    parser.add_argument("--lake", type=Path, help="recorder lake, for station sizes")
    parser.add_argument("--sample", type=int, default=6_000_000)
    args = parser.parse_args()

    con = db.connect()
    build_table(con, args.raw)
    build_capacity(con, args.lake)
    results, chosen, report = evaluate(con, args.sample)
    print(report)
    if summary := os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(summary, "a") as f:
            f.write(report + "\n")
    if not chosen:
        raise SystemExit("model not approved: it doesn't beat the averages; nothing saved")
    train_production(con, args.out, args.sample, chosen, results[chosen])
    (args.out / "metrics.md").write_text(report + "\n")
    (args.out / "metrics.json").write_text(json.dumps(results[chosen], indent=2))
    (args.out / "metrics_variants.json").write_text(
        json.dumps({"chosen": chosen, "variants": results}, indent=2)
    )


if __name__ == "__main__":
    main()
