---
type: Metric
title: Empty station-minutes
description: Minutes a station had no bike to rent, counted only while the recorder was up.
tags: [metric]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T19:00:00Z }
---

# Definition

A station is empty in a minute when `num_bikes_available = 0` and it is renting ([fct_station_minute](/tables/fct_station_minute.md)). Riders arriving then can't take a bike; see [riders affected](/glossary/riders-affected.md).

# How to compute

Use the sanctioned computation [empty-stations](/computations/empty-stations.md); do not write your own SQL for this metric.
