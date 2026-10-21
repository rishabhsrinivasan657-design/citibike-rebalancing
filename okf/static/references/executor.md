---
type: Executor
title: DuckDB executor for Attested Computations
description: Runs a sanctioned computation on the mart Parquet files and returns a receipt.
resource: https://github.com/rishabhsrinivasan657-design/citibike-rebalancing/blob/main/src/rebalancer/okf/attest.py
generated: { by: claude-code/claude-opus-5-5, at: 2026-10-21T19:00:00Z }
---

# How to run

    okf-metric run computations/<id> --param name=value ... --bundle <bundle> --data <marts folder>

The MCP server's `run_metric` tool calls the same code.

# What it does

1. Loads the contract and the SQL file named by `computation`.
2. Rejects unknown parameters, missing required ones, and values of the wrong type.
3. Creates read-only views over the Parquet files the SQL names, and runs the SQL with
   the parameter values bound as named parameters (never pasted into the text).
4. Returns the receipt.

# Receipt

| Field | Meaning |
|---|---|
| `computation` | Concept id of the computation that ran. |
| `computation_sha256` | Fingerprint of the sanctioned SQL (comments and spacing ignored). |
| `executed_sql` | The SQL text that ran. |
| `parameters` | The values supplied. |
| `data_revision` | Fingerprint of the exact input files. |
| `executed_at` | When it ran (UTC). |
| `executor` | `rebalancer-okf-executor/0.1`. |
| `result` | The rows returned. |
| `result_sha256` | Fingerprint of the rows, so an edited result is detectable. |
