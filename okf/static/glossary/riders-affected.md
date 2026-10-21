---
type: Glossary Term
title: Riders affected
description: Riders who would find no bike (at an empty station) or no free dock (at a full one); the project's headline outcome.
tags: [metrics, outcome]
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T18:00:00Z }
sources:
  - id: design
    resource: https://github.com/rishabhsrinivasan657-design/citibike-rebalancing/blob/main/docs/DESIGN.md#backtest-step-6-replaying-recorded-mornings
    title: Backtest design
---

# Definition

The sum of **missed pickups** (a rider wanted a bike, none was there) and **missed
returns** (a rider wanted to park, no dock was free) over a time window. It is an
**estimate**: riders turned away leave no trace, so it includes
[hidden demand](/glossary/hidden-demand.md).[^design]

# Caveats

- Some turned-away riders walk to a nearby station, so the count overstates real misses.
- Comparisons (no vans vs a plan) are more reliable than absolute values.

[^design]: Backtest design
