---
type: Glossary Term
title: Recorder coverage
description: Share of minutes with at least one successful live-feed poll; target 98% over 24 hours.
tags: [data quality, operations]
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T18:00:00Z }
---

# Definition

Minutes with `recorder_up = true` in [fct_recorder_minute](/tables/fct_recorder_minute.md),
divided by all minutes in the window. When the recorder is down, minute values are
carried forward rather than observed, so analyses should exclude or flag those minutes.
