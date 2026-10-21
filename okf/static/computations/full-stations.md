---
type: Attested Computation
title: Stations full the longest
description: Stations ranked by recorded minutes with no free dock, between two New York dates, optionally
  in a zone.
tags:
- attested
- duckdb
status: stable
runtime: duckdb
computation: /references/computations/full-stations.sql
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
- name: top_n
  type: integer
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

Computes the [full-minutes](/metrics/full-minutes.md) metric. Reads [agg_station_day](/tables/agg_station_day.md), [dim_zone](/tables/dim_zone.md).

# Parameters

- `start_date` (date)
- `end_date` (date)
- `zone` (string): a zone name from [dim_zone](/tables/dim_zone.md), or `all`
- `top_n` (integer)

# Output

`station_name`, `full_minutes`, `minutes_recorded`, `full_share`.

# Computation

The sanctioned SQL is the file named by `computation`. An agent may only supply parameter values; the [attester](/references/attester.md) rejects any other SQL.
