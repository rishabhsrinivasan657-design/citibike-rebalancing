---
type: Attested Computation
title: Van activity per day
description: Likely Lyft van drop-offs and pickups per New York day between two dates, optionally in a
  zone.
tags:
- attested
- duckdb
status: stable
runtime: duckdb
computation: /references/computations/van-activity.sql
parameters:
- name: start_date
  type: date
  required: true
- name: end_date
  type: date
  required: true
- name: zone
  type: string
  required: true
executor:
  resource: /references/executor.md
  receipt:
  - computation_sha256
  - executed_sql
  - parameters
  - data_revision
  - executed_at
  - result
  - result_sha256
attester:
  resource: /references/attester.md
stale_after: '2027-04-21T00:00:00Z'
sources:
- id: agg_station_day
  resource: /tables/agg_station_day.md
  title: agg_station_day
- id: dim_zone
  resource: /tables/dim_zone.md
  title: dim_zone
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T19:00:00Z }
---

# Purpose

Computes the [van-visits](/metrics/van-visits.md) metric. Reads [agg_station_day](/tables/agg_station_day.md), [dim_zone](/tables/dim_zone.md).

# Parameters

- `start_date` (date)
- `end_date` (date)
- `zone` (string): a zone name from [dim_zone](/tables/dim_zone.md), or `all`

# Output

`local_date`, `drop_off_visits`, `pick_up_visits`, `bikes_dropped_off`, `bikes_picked_up`, `stations_visited`.

# Computation

The sanctioned SQL is the file named by `computation`. An agent may only supply parameter values; the [attester](/references/attester.md) rejects any other SQL.
