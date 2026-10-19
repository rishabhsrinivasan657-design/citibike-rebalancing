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

Parquet lands in `data/lake/<table>/date=YYYY-MM-DD/hour=HH/`.
