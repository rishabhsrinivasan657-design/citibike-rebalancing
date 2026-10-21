---
type: Attester
title: Deterministic attester for DuckDB computations
description: Checks a receipt without any LLM; callers must not show a value that fails.
resource: https://github.com/rishabhsrinivasan657-design/citibike-rebalancing/blob/main/src/rebalancer/okf/attest.py
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T19:00:00Z }
---

# Checks

1. **Sanctioned SQL**: the SQL that ran equals the computation's SQL (comments and
   spacing ignored), and its fingerprint matches.
2. **Parameters**: only declared parameters, all required ones present, valid types.
3. **Result integrity**: the rows match `result_sha256`.
4. **Reproducible** (when the data is available): the data fingerprint matches and
   re-running gives the same rows.
5. **Claimed value** (optional): a value an agent is about to state appears in the result.

It also reports `definition_stale` when the computation is past its `stale_after`,
which is a separate question from whether this run was sanctioned (spec §10.6).
