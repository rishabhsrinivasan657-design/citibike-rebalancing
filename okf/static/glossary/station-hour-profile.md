---
type: Glossary Term
title: Station-hour profile
description: A station's average departures and arrivals for a day type and local hour, over 12 months of trips.
tags: [baseline, demand]
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T18:00:00Z }
---

# Definition

For each station, day type (weekday, or weekend/holiday) and New York hour: the
average number of rides starting and ending there per day, counting days with zero
rides. It is the baseline the demand model must beat, and the source of
[hidden demand](/glossary/hidden-demand.md) estimates. Table:
[stg_station_hour_profile](/tables/stg_station_hour_profile.md).
