# 🏅 IDAMP — Intent-Driven Agentic Medallion Pipeline

> **Deterministic Data Engineering for Retail Sales Analytics — no LLM, no API key**
>
> Upload raw CSV files, ask a business question in plain English, and receive a structured executive report — powered by a rule-based multi-agent pipeline working through a Bronze → Silver → Gold Medallion architecture.

---

## 📋 Table of Contents

- [Overview](#overview)
- [Architecture](#architecture)
- [How It Works](#how-it-works)
- [Project Structure](#project-structure)
- [Agents](#agents)
- [Key Design Decisions](#key-design-decisions)
- [Tech Stack](#tech-stack)
- [Setup & Installation](#setup--installation)
- [Running the Pipeline](#running-the-pipeline)
- [Observability](#observability)
- [Known Limitations](#known-limitations)
- [FAQ](#faq)

---

## Overview

Traditional data engineering requires someone to manually inspect raw files, write transformation rules, apply cleansing logic, build aggregation queries, and produce reports. This pipeline automates that entire workflow with **rule-based heuristics — no LLM call, no API key, no network dependency required.**

**What you do:**
1. Upload raw CSV files via the Streamlit UI
2. Type a business question (e.g. *"What is the average unit price by category?"*)
3. Review and approve auto-generated transformation rules at three checkpoints
4. Receive a complete HTML executive report with a SQL-backed chart

**What the pipeline does (deterministically):**
- Profiles your data (column stats, semantic guesses, candidate join keys) using name/dtype pattern matching
- Generates Source-to-Target Mapping (STTM) rules for each layer mechanically
- Executes Bronze ingestion, Silver cleansing, and Gold materialisation by applying those rules with pandas
- Infers a measure, dimension(s), aggregation function, and sort direction from your question's wording and the column names available
- Runs the resulting SQL against DuckDB and renders an interactive HTML report with a Plotly chart

---

## Architecture

```
┌─────────────┐     ┌──────────────────┐     ┌───────────────────────────────────────────┐
│             │     │                  │     │                  AGENTS                   │
│  Streamlit  │────▶│   Orchestrator   │────▶│  Profiler → STTM → Bronze → Silver →      │
│     UI      │     │ (plain control   │     │  Gold → Reporter                          │
│             │◀────│      flow)       │◀────│                                           │
└─────────────┘     └──────────────────┘     └───────────────────────────────────────────┘
       │                                                        │
       │              ⏸ Human approves STTM (×3)               │
       └────────────────────────────────────────────────────────┘
```

There is no Supervisor LLM choosing what to run — every phase always executes the same fixed sequence of specialist functions. Each "agent" is a plain Python module using heuristics (regex/keyword matching on column names, dtype checks, and substring matching on transformation-rule text) instead of natural-language reasoning.

### The Four Phases

| Phase | Functions | Output | Gate |
|-------|-----------|--------|------|
| **Phase 1** | `profile_multiple_datasets` → `generate_bronze_sttm` | `profile_*.json` + `sttm_bronze_*.csv` | ⏸ Human approves Bronze STTM |
| **Phase 2** | `execute_bronze` → `generate_silver_sttm` | `*_bronze.parquet` + `sttm_silver_*.csv` | ⏸ Human approves Silver STTM |
| **Phase 3** | `execute_silver` → `generate_gold_sttm` | `*_silver.parquet` + `sttm_gold_*.csv` | ⏸ Human approves Gold STTM |
| **Phase 4** | `execute_gold` → `generate_report` | Gold `*.parquet` + `report_*.html` | ✅ Pipeline complete |

---

## How It Works

### Medallion Layers

```
Raw CSV  ──▶  Bronze  ──▶  Silver  ──▶  Gold  ──▶  Report
             (ingest)    (cleanse)  (aggregate)   (answer)
```

| Layer | What happens |
|-------|-------------|
| **Bronze** | CSV → Parquet. Columns renamed, types cast by dtype, `_load_timestamp` and `_source_file` metadata injected. No null handling — faithful raw copy. |
| **Silver** | Bronze Parquet → cleansed Parquet. Nulls handled (mean/median/mode/fill), deduplication on the first ID-like column, type standardisation, date formatting, surrogate key `pk_*_silver_id` injected as the first column. |
| **Gold** | Silver Parquet → analytics-ready Parquet. Every Silver table's columns are passed straight through into one wide `gold_output` table; `gold_agent.py` auto-joins the Silver tables on shared `*_id` columns (multi-pass, so table order in the STTM never silently breaks a 3+ table join). |
| **Report** | Gold Parquet → HTML. The reporter infers a measure/dimension/aggregation/sort-direction from the business question's wording and the Gold table's column names, runs the resulting SQL in DuckDB, and renders an HTML report with a Plotly chart. |

### How the Report Actually Reads Your Question

There is no LLM parsing free text here — `reporter.py` uses keyword/name matching:

- **Measure** (the numeric column to aggregate) — prefers a column explicitly named in the question (e.g. "unit price" → `unit_price`), falling back to a fixed keyword priority list (`total_amount`, `revenue`, `sales`, `amount`, `price`, `total`, `cost`).
- **Dimension** (the column to group by) — same idea, restricted to text/boolean/datetime columns so a low-cardinality *numeric* column (like `quantity`) is never mistaken for a category.
- **Aggregation** — `average`/`avg`/`mean` → `AVG`; `count`/`how many` → `COUNT`; `highest value`/`maximum` → `MAX`; `lowest value`/`minimum` → `MIN`; otherwise `SUM`.
- **Sort direction** — `lowest`/`bottom`/`worst`/`smallest`/`least` → ascending; otherwise descending (top-N).

Different questions on the same data genuinely produce different reports — but this is still pattern matching, not comprehension. See [Known Limitations](#known-limitations).

---

## Project Structure

```
IDAMP/
│
├── app/
│   └── streamlit_app.py          # Streamlit UI — entry point for users
│
├── agents/
│   ├── orchestrator.py           # Plain-Python phase sequencing + PipelineState
│   ├── profiler.py                # Column stats + heuristic semantic guesses
│   ├── sttm_generator.py         # Bronze/Silver/Gold STTM generation (mechanical)
│   ├── bronze_agent.py           # Bronze layer ingestion
│   ├── silver_agent.py           # Silver layer cleansing
│   ├── gold_agent.py             # Gold layer join + materialisation
│   └── reporter.py               # Measure/dimension/agg inference + SQL + HTML report
│
├── core/
│   ├── config.py                 # Paths (no API keys — none needed)
│   ├── audit.py                  # AuditLogger — append-only JSONL event log
│   ├── observability.py          # AgentTrace — per-agent execution trace
│   ├── memory.py                 # Local JSONL document store (no vector DB)
│   └── state.py
│
├── tests/                        # pytest suite covering every agent
├── sample_data/                  # products.csv, stores.csv, sales_data.csv
│
├── data/                         # generated at runtime — gitignored
│   ├── bronze_layer/ silver_layer/ gold_layer/
│   ├── sttm/ profiles/ traces/ memory/
├── reports/                      # generated at runtime — gitignored
├── audit_logs/                   # generated at runtime — gitignored
│
├── .env.example                  # no keys required; kept for forward-compatibility
├── requirements.txt
└── README.md
```

`data/`, `reports/`, and `audit_logs/` are created automatically by `core/config.py` on first run — they don't need to exist in the repo.

---

## Agents

### Orchestrator — `agents/orchestrator.py`

Plain Python control flow — no LLM decides what to call. Four entry points matching the four HITL-gated phases:

```python
run_until_bronze_sttm(uploaded_files, business_intent) -> PipelineState
run_bronze_to_silver_sttm(state) -> PipelineState
run_silver_to_gold_sttm(state) -> PipelineState
run_gold_and_report(state) -> PipelineState
```

`PipelineState` is a `TypedDict` carrying `run_id`, `status`, file paths at each stage, approval flags, and `error` — read directly by the Streamlit UI.

---

### Profiler — `agents/profiler.py`

```python
profile_multiple_datasets(file_paths, run_id, task_description) -> str
```

Computes per-column statistics with pandas (null %, unique count, min/max/mean), then guesses semantic meaning (ID / date / money / quantity / name / location / category) from column-name regex patterns, flags same-named columns across datasets as candidate join keys, and writes it all to `data/profiles/profile_combined_*.json`.

---

### STTM Generator — `agents/sttm_generator.py`

```python
generate_bronze_sttm(profile_path, run_id, task_description) -> str
generate_silver_sttm(bronze_output_paths, bronze_sttm_path, run_id, task_description) -> str
generate_gold_sttm(silver_output_paths, silver_sttm_path, business_intent, run_id, task_description) -> str
```

Mechanically builds the STTM CSV (`source_table`, `source_column`, `target_table`, `target_column`, `transformation_type`, `transformation_logic`) for each layer:
- **Bronze** — one row per column (type-cast rule based on dtype) + two metadata rows.
- **Silver** — null-handling/dedup/date-standardisation rules per column, a surrogate-key row, and a dedup row keyed on the first ID-like column.
- **Gold** — passthrough of every Silver column into one `gold_output` target table (`gold_agent.py` does the joining; `reporter.py` does the measure/dimension selection at query time).

The `transformation_logic` text matters: the Bronze/Silver/Gold executors decide what to do by substring-matching this text (e.g. `"fill null" in logic and "mean" in logic`), so if you hand-edit a rule's wording in the approval screen to something the executor doesn't recognise, it silently no-ops instead of erroring.

---

### Bronze Agent — `agents/bronze_agent.py`

```python
execute_bronze(input_files, sttm_path, run_id, task_description) -> list[str]
```

Column renaming, type casting (`to_numeric`, `to_datetime`), injects `_load_timestamp` (UTC ISO string) and `_source_file` into every output Parquet file.

---

### Silver Agent — `agents/silver_agent.py`

```python
execute_silver(input_files, sttm_path, run_id, task_description) -> list[str]
```

Null handling (`dropna`/`fillna` with mean/median/mode/constant), deduplication (`drop_duplicates`), type casting, date standardisation, text normalisation (strip/lower/upper/title case), surrogate key injection (`pk_*_silver_id` as the first column), column filtering to STTM-approved targets only.

---

### Gold Agent — `agents/gold_agent.py`

```python
execute_gold(input_files, sttm_path, run_id, task_description) -> list[str]
```

Loads all Silver tables, then repeatedly merges in whichever remaining table currently shares an `*_id` column with the accumulated result — deferring tables that don't share a key yet, since a later merge may introduce the missing one — and only falls back to a union for whatever's left once no more merges are possible. This multi-pass approach means table order in the STTM never silently breaks a 3+ table join. Then applies renames/group-by/aggregations declared in the STTM and injects `pk_gold_id`.

---

### Reporter — `agents/reporter.py`

```python
generate_report(gold_files, business_intent, run_id, task_description) -> str
```

Loads Gold Parquet into DuckDB, infers measure/dimension/aggregation/sort-direction from the business question (see [How the Report Actually Reads Your Question](#how-the-report-actually-reads-your-question)), runs the `GROUP BY` query, and renders a self-contained HTML report with a Plotly bar chart and a templated executive summary built from the real computed numbers.

**Produces:**
- `reports/report_{run_id[:8]}.html` — self-contained HTML with an embedded Plotly chart
- `reports/report_{run_id[:8]}.json` — structured `direct_answer` / `charts` / `detailed_analysis`

---

## Key Design Decisions

### Why no LLM?

This fork intentionally removes the LangChain/LangGraph ReAct-agent architecture the project originally used (Groq/Gemini API calls at every step) so it can run **fully offline with no API key**. Every specialist function keeps the exact same signature and `PipelineState` shape as the original design — the Streamlit UI required zero changes.

### Substring-matched transformation rules

Rather than an LLM interpreting `transformation_logic` text, the executors check for known substrings (`"fill null"`, `"mean"`, `"deduplic"`, `"group by"`, etc.). This is fast and fully deterministic, but it means the STTM's wording is really a small DSL, not free text — see [Known Limitations](#known-limitations).

### Human-in-the-Loop Gates

Three approval gates pause the pipeline after each STTM generation. The STTM is the transformation recipe — if it's wrong, all downstream data is wrong. Review before execution catches errors before they propagate.

### Run isolation (partial)

`profile_*.json`, `sttm_*_{run_id}.csv`, and `report_{run_id}.html/.json` are namespaced by `run_id`. **Bronze/Silver/Gold Parquet outputs are not** — they're named only after the source CSV (and Gold's is always literally `gold_output.parquet`), so a second run — or a second concurrent session — overwrites the previous run's intermediate/final tables. Fine for one person running one analysis at a time; something to fix before any multi-user or concurrent use.

---

## Tech Stack

| Category | Library | Used for |
|----------|---------|----------|
| **UI** | `streamlit` | File upload, STTM approval (`st.data_editor`), report rendering |
| **Data** | `pandas` | CSV/Parquet read-write, all transformations |
| **Storage** | `pyarrow` | Parquet file backend |
| **Analytics** | `duckdb` | In-memory SQL engine for the Reporter |
| **Charts** | `plotly` | Bar chart rendering |
| **Config** | `python-dotenv` | Loads `.env` (currently unused — no keys needed) |
| **Validation** | `pydantic` | (available for future schema validation) |
| **Testing** | `pytest` | Full agent test suite |

No `langchain`, no `langgraph`, no LLM SDK, no vector database.

---

## Setup & Installation

### Prerequisites

- Python 3.11+

### 1. Clone the repository

```bash
git clone https://github.com/<your-username>/idamp.git
cd idamp
```

### 2. Create a virtual environment

```bash
python -m venv venv
source venv/bin/activate        # Mac/Linux
venv\Scripts\activate           # Windows
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

That's it — no API key, no `.env` values to fill in. `core/config.py` creates all the `data/`, `reports/`, and `audit_logs/` directories automatically on first run.

---

## Running the Pipeline

```bash
streamlit run app/streamlit_app.py
```

The app opens at `http://localhost:8501`.

### Step-by-step usage

1. **Upload CSV files** — try the ones in `sample_data/` (`products.csv`, `stores.csv`, `sales_data.csv`)
2. **Enter your business question** — e.g. *"What are the top-performing product categories by total sales revenue for each store region?"*
3. **Start Workflow** — profiles the data, generates Bronze STTM
4. **Review Bronze STTM** — check/uncheck rows, click Approve & Continue
5. **Review Silver STTM** — same, then Approve & Continue
6. **Review Gold STTM** — same, then Approve & Execute
7. **View report** — executive summary + chart answers your question, downloadable as HTML

---

## Observability

Every agent invocation writes a full trace to `data/traces/trace_{agent_name}_{run_id[:8]}.json`, and `core/audit.py` appends every phase start/complete/fail event to `audit_logs/{run_id}.jsonl` — readable JSON, useful for debugging a run without re-running it.

---

## Known Limitations

- **Not real NL understanding** — the reporter matches keywords/column names, it doesn't parse grammar or meaning. Unusual phrasing may not pick the column you intended.
- **Gold layer is a single wide passthrough table** — without an LLM to interpret business intent into custom Gold shapes, every Silver column is joined into one `gold_output` table; the reporter picks measure/dimension at query time instead.
- **STTM rule text is a small fixed vocabulary** — editing a rule's wording to something the executor doesn't recognise (see [substring-matched transformation rules](#substring-matched-transformation-rules)) silently no-ops rather than erroring.
- **No run isolation for Bronze/Silver/Gold Parquet** — see [Run isolation](#run-isolation-partial) above.
- **No cleanup between runs** — `data/`, `reports/`, and `audit_logs/` accumulate indefinitely; nothing prunes old runs.

---

## FAQ

**Q: Do I need an API key to run this?**

No. This fork is fully deterministic — no LLM call, no network dependency, no `.env` values required.

**Q: Why does the report answer look templated?**

Because it is — `reporter.py` builds the answer text from a fixed template filled in with the real computed numbers (measure, dimension, aggregation, top/bottom row), not from an LLM writing prose.

**Q: Why are there 4 phases instead of running everything at once?**

The STTM is the transformation recipe. If it's wrong, all downstream data is wrong. Phases create human review checkpoints before each layer executes — preventing errors from propagating silently through the pipeline.

**Q: What is the STTM CSV I'm asked to approve?**

Source-to-Target Mapping: `source_schema`, `source_table`, `source_column`, `target_schema`, `target_table`, `target_column`, `transformation_type`, `transformation_logic`. You can edit any cell in the approval screen (`st.data_editor`) before approving — but see the substring-matching caveat above.

**Q: What happens if a phase fails partway through?**

The error and partial state are saved; `PipelineState["error"]` and `["status"]` reflect the failure. Re-running from the Streamlit UI restarts a fresh analysis rather than resuming mid-phase.

**Q: Can I add my own CSVs?**

Yes — any CSV works. Column-name-based heuristics (join keys, semantic meaning, measure/dimension picks) work best when columns are named conventionally (`*_id`, `*_date`, `*_amount`/`price`/`revenue`, `category`/`region`/etc.), since there's no LLM to infer intent from unconventional names.

---

## License

MIT License — see `LICENSE` for details.

---

*Built with pandas · DuckDB · Plotly · Streamlit — no LLM required.*
