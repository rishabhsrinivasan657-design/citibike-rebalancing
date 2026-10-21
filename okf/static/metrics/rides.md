---
type: Metric
title: Rides per station
description: Rides started and ended at a station, from the published trip files.
tags: [metric]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T19:00:00Z }
---

# Definition

From [fct_trips](/tables/fct_trips.md) via [agg_station_month](/tables/agg_station_month.md). Trip files lag 2-3 weeks.

# How to compute

Use the sanctioned computation [busiest-stations](/computations/busiest-stations.md); do not write your own SQL for this metric.
