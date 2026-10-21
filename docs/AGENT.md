# Asking Claude about Citi Bike, with approved answers

The project includes an MCP server: a plug-in that lets an AI assistant such as Claude
Desktop use the project's knowledge bundle (Step 8). The assistant can search and read
what each table and metric means, and get numbers only by running **approved metrics**.
Each answer comes with a receipt and a check, and warnings when data is stale or a
definition hasn't been reviewed. There is no tool for writing its own SQL.

## Set up (about 5 minutes)

1. In the project folder, install the extra pieces:

   ```bash
   uv sync --extra agent
   ```

2. Get a **read-only** Hugging Face token (huggingface.co → Settings → Access Tokens →
   Create new token → type **Read**). The one you made for the Streamlit app works.

3. In Claude Desktop: **Settings → Developer → Edit Config**. Add this to
   `claude_desktop_config.json`, with your folder path and token:

   ```json
   {
     "mcpServers": {
       "citibike": {
         "command": "/Users/YOU/Desktop/Projects/Tape/citibike-rebalancing/.venv/bin/okf-mcp",
         "env": { "HF_TOKEN": "hf_your_read_token" }
       }
     }
   }
   ```

4. Restart Claude Desktop. On first use the server downloads the bundle and a few
   small summary tables (a few MB) to `~/.cache/citibike-okf`. Refresh them any time with:

   ```bash
   HF_TOKEN=hf_... uv run okf-sync
   ```

## Try it

- "Which Midtown stations were empty the longest on October 20?"
- "How many likely van drop-offs were there in Midtown yesterday?"
- "What was the recorder's coverage on October 19 and 20?"
- "What were the busiest stations in September?"
- "How many riders were turned away at Grand Central?" (no approved metric: it
  should say it can't answer reliably)

## What the tools do

| Tool | Purpose |
|---|---|
| `search_concepts` | Find tables, metrics and glossary terms by keyword |
| `read_concept` | Read one concept, with its trust tier and whether it's stale |
| `list_metrics` | The approved metrics and their parameters |
| `run_metric` | Run one; returns the result, a receipt, the checker's verdict and warnings |
