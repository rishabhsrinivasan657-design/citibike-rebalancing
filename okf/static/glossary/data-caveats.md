---
type: Reference
title: Known data quirks
description: Quirks in Citi Bike's data that change answers if ignored.
tags: [data quality, caveats]
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T18:00:00Z }
---

# Quirks

1. **Month-end rides appear in two monthly files**; [fct_trips](/tables/fct_trips.md) keeps one.
2. **The repeated 1-2am hour when clocks go back** is ambiguous in local times; about 480
   rides a year appear to end before they start.
3. **Station codes look like numbers but are text** (`6131.1` is a real station).
4. **About 0.02% of rides last over a day** (unreturned bikes closed out later).
5. **Trip files arrive monthly with a 2-3 week lag**; recent days only have live counts.
6. **Recording began 2026-10-19 evening**; an 80-minute gap followed before the relay fix.
