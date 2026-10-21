---
type: Metric
title: Recorder coverage
description: Share of minutes the recorder captured; the target is 98% over 24 hours.
tags: [metric]
status: stable
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T19:00:00Z }
---

# Definition

See [recorder coverage](/glossary/recorder-coverage.md). Low coverage makes every other live metric less reliable for that day.

# How to compute

Use the sanctioned computation [recorder-coverage](/computations/recorder-coverage.md); do not write your own SQL for this metric.
