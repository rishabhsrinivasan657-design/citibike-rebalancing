# Citi Bike Rebalancing

Forecast which Citi Bike stations will run empty or full in the next 30–60 minutes,
plan crew moves to prevent it, and backtest how many stock-out minutes the plan saves.

- Plan and milestones: [docs/PLAN.md](docs/PLAN.md)
- Architecture and data contracts: [docs/DESIGN.md](docs/DESIGN.md)
- Turning on 24/7 recording (free): [docs/SETUP_CLOUD.md](docs/SETUP_CLOUD.md)

## Quick start

```bash
make install     # Python deps via uv
make test        # unit tests
make up          # Redpanda + collector + lake sink (Docker)
make logs        # follow collector and sink logs
```

Past trips, weather and holidays (Step 2) are downloaded on GitHub, not locally:

```bash
gh workflow run backfill.yml
```

Plan van moves for a real zone and time (Step 5), also on GitHub:

```bash
gh workflow run plan.yml -f start=08:00 -f vans=3
```

Build the clean tables with dbt (Step 3), on GitHub; also runs every 6 hours:

```bash
gh workflow run transform.yml
```

Parquet lands in `data/lake/<table>/date=YYYY-MM-DD/hour=HH/`.
