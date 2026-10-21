---
type: Attested Computation
title: Busiest stations by rides
description: Stations ranked by rides started plus ended between two months, optionally in a zone, from
  the trip files.
tags:
- attested
- duckdb
status: stable
runtime: duckdb
computation: /references/computations/busiest-stations.sql
parameters:
- name: start_month
  type: month
  required: true
- name: end_month
  type: month
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
- id: agg_station_month
  resource: /tables/agg_station_month.md
  title: agg_station_month
- id: dim_zone
  resource: /tables/dim_zone.md
  title: dim_zone
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T19:00:00Z }
---

# Purpose

Computes the [rides](/metrics/rides.md) metric. Reads [agg_station_month](/tables/agg_station_month.md), [dim_zone](/tables/dim_zone.md).

# Parameters

- `start_month` (month)
- `end_month` (month)
- `zone` (string): a zone name from [dim_zone](/tables/dim_zone.md), or `all`
- `top_n` (integer)

# Output

`station_name`, `rides_started`, `rides_ended`, `rides_total`.

# Computation

The sanctioned SQL is the file named by `computation`. An agent may only supply parameter values; the [attester](/references/attester.md) rejects any other SQL.
