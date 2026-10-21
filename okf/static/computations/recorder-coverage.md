---
type: Attested Computation
title: Recorder coverage per day
description: Share of minutes the recorder captured per New York day between two dates.
tags:
- attested
- duckdb
status: stable
runtime: duckdb
computation: /references/computations/recorder-coverage.sql
parameters:
- name: start_date
  type: date
  required: true
- name: end_date
  type: date
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
- id: fct_recorder_minute
  resource: /tables/fct_recorder_minute.md
  title: fct_recorder_minute
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T19:00:00Z }
---

# Purpose

Computes the [recorder-coverage](/metrics/recorder-coverage.md) metric. Reads [fct_recorder_minute](/tables/fct_recorder_minute.md).

# Parameters

- `start_date` (date)
- `end_date` (date)

# Output

`local_date`, `minutes_in_window`, `minutes_recorded`, `coverage`.

# Computation

The sanctioned SQL is the file named by `computation`. An agent may only supply parameter values; the [attester](/references/attester.md) rejects any other SQL.
