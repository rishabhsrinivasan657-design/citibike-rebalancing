---
type: Metric
title: Full station-minutes
description: Minutes a station had no free dock, counted only while the recorder was up.
tags: [metric]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T19:00:00Z }
---

# Definition

A station is full in a minute when `num_docks_available = 0` and it accepts returns. Riders arriving then can't park.

# How to compute

Use the sanctioned computation [full-stations](/computations/full-stations.md); do not write your own SQL for this metric.
