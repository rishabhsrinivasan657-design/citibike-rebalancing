---
type: Glossary Term
title: Station
description: A Citi Bike docking location; identified by a GBFS UUID live and by a short code in trip files.
tags: [core, stations]
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T18:00:00Z }
sources:
  - id: gbfs
    resource: https://github.com/MobilityData/gbfs/blob/v1.1/gbfs.md
    title: General Bikeshare Feed Specification v1.1
---

# Definition

A fixed place with docks where riders take and return bikes. About 2,500 exist,
mostly in New York City, plus Jersey City and Hoboken.[^gbfs]

# Identifiers

- `station_id`: GBFS UUID used by the live feed and every mart.
- `short_name`: Citi Bike's code (e.g. `6140.05`), used in trip files. It is text:
  `7293.10` and `7293.1` are different stations.

Current and past versions are in [dim_station](/tables/dim_station.md).

[^gbfs]: General Bikeshare Feed Specification v1.1
