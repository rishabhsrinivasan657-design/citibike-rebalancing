---
type: Glossary Term
title: Van visit (rebalancing)
description: A Lyft crew loading or unloading bikes at a station; inferred from sudden one-way jumps in bike counts.
tags: [rebalancing, inferred]
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T18:00:00Z }
---

# Definition

Lyft moves bikes with vans to keep stations usable. Visits are not published; they
are **inferred** in [fct_inferred_rebalancing](/tables/fct_inferred_rebalancing.md):
a change of at least 7 bikes within 1-3 minutes that riders at that station would
almost never cause at that time of day.

# Caveats

- Jersey City and Hoboken are not covered.
- Small or slow visits fall under the threshold and look like riders.
- Lyft may also add capacity in ways the detector cannot see (e.g. staff docking extra bikes).
