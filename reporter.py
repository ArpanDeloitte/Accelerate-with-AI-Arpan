"""Reporting agent — deterministic version (no LLM, no API key required).

Loads the Gold tables into DuckDB, then infers from the business question
(via keyword/name matching, not NL understanding):
  - which numeric column is the "measure",
  - which categorical column(s) are the "dimension(s)",
  - which aggregation to use (SUM/AVG/COUNT/MAX/MIN), and
  - which sort direction to show results in (top N vs bottom N),
runs a GROUP BY query, and renders a self-contained HTML report with a
Plotly chart and a templated executive summary built from the actual query
results.

I/O contract (UNCHANGED — UI and orchestrator safe):
    generate_report(gold_files, business_intent, run_id, task_description) -> str
"""

import re
import json
import pandas as pd
import duckdb
import plotly.graph_objects as go
from pathlib import Path
from core.config import REPORTS_DIR
from core.audit import AuditLogger
from core.observability import AgentTrace
from core.memory import store_document

_MEASURE_PRIORITY = ["total_amount", "revenue", "sales", "amount", "price", "total", "cost"]
_DIMENSION_PRIORITY = ["category", "region", "segment", "payment_method", "product_name", "store_name", "city", "state"]
_EXCLUDE_COLUMNS = re.compile(r"^(pk_|_)|(_id$)|^id$", re.IGNORECASE)

_AVG_RE = re.compile(r"\b(average|avg|mean)\b")
_COUNT_RE = re.compile(r"\b(count|number of|how many)\b")
_MAX_VALUE_RE = re.compile(r"\b(highest|maximum|largest|max)\s+value\b")
_MIN_VALUE_RE = re.compile(r"\b(lowest|minimum|smallest|min)\s+value\b")
_ASCENDING_RE = re.compile(r"\b(lowest|bottom|worst|smallest|least|minimum)\b")

_AGG_VERB = {"SUM": "summing", "AVG": "averaging", "COUNT": "counting", "MAX": "taking the maximum of", "MIN": "taking the minimum of"}
_DIRECTION_WORD = {"DESC": "descending", "ASC": "ascending"}


# ---------------------------------------------------------------------------
# Heuristic measure/dimension/aggregation/direction selection — replaces
# the LLM's job of actually reading the business question.
# ---------------------------------------------------------------------------

def _word_matches(a: str, b: str) -> bool:
    """Loose match tolerant of simple plurals/suffixes (e.g. "category" ~ "categories").

    Short words (<4 chars) must match exactly, to avoid coincidental
    collisions like "cost" vs "most".
    """
    if a == b:
        return True
    if len(a) < 4 or len(b) < 4:
        return False
    prefix_len = min(len(a), len(b)) - 1
    return a[:prefix_len] == b[:prefix_len]


def _columns_named_in_intent(columns: list, business_intent: str) -> list:
    """Score each column by how explicitly the business question names it.

    Returns (score, column) pairs, score 2 = exact phrase match, 1 = every
    word in the column name appears somewhere in the question, 0 = at least
    one word does. Higher score = more specific/confident match.
    """
    intent_lower = (business_intent or "").lower()
    intent_words = re.findall(r"[a-z0-9]+", intent_lower)
    scored = []
    for col in columns:
        col_words = [w for w in col.lower().split("_") if w]
        if not col_words:
            continue
        readable = " ".join(col_words)
        if readable in intent_lower:
            scored.append((2, col))
        elif all(any(_word_matches(cw, iw) for iw in intent_words) for cw in col_words):
            scored.append((1, col))
        elif any(_word_matches(cw, iw) for cw in col_words for iw in intent_words):
            scored.append((0, col))
    return scored


def _rank_by_intent(candidates: list, priority: list, business_intent: str) -> list:
    """Rank candidate columns using the business question, falling back to a
    fixed keyword priority list.

    Columns the question names explicitly are promoted ahead of the rest.
    When several named columns also appear in `priority`, the priority
    list's relative order wins over raw text-match strength — this keeps
    behaviour predictable when a question loosely mentions several columns
    at once (e.g. "...for each store region" shouldn't demote "category"
    just because "region" happens to be an exact substring match while
    "category" only fuzzy-matches "categories").
    """
    def priority_index(col: str) -> int:
        col_lower = col.lower()
        for i, keyword in enumerate(priority):
            if keyword in col_lower:
                return i
        return len(priority)

    scored_named = _columns_named_in_intent(candidates, business_intent)
    named_cols = {col for _, col in scored_named}
    score_of = {}
    for score, col in scored_named:
        score_of[col] = max(score, score_of.get(col, -1))

    named_in_priority = sorted(
        (c for c in named_cols if priority_index(c) < len(priority)),
        key=lambda c: (priority_index(c), -score_of[c]),
    )
    named_not_in_priority = sorted(
        (c for c in named_cols if priority_index(c) == len(priority)),
        key=lambda c: -score_of[c],
    )
    remaining_in_priority = sorted(
        (c for c in candidates if c not in named_cols and priority_index(c) < len(priority)),
        key=priority_index,
    )
    remaining_rest = [c for c in candidates if c not in named_cols and priority_index(c) == len(priority)]

    ranked = named_in_priority + named_not_in_priority + remaining_in_priority + remaining_rest
    seen = set()
    result = []
    for c in ranked:
        if c not in seen:
            seen.add(c)
            result.append(c)
    return result


def _pick_measure(df: pd.DataFrame, business_intent: str = "") -> str | None:
    """Pick the numeric column most likely to be the business "measure",
    preferring one the question names explicitly."""
    numeric_cols = [
        c for c in df.columns
        if pd.api.types.is_numeric_dtype(df[c]) and not _EXCLUDE_COLUMNS.search(c)
    ]
    if not numeric_cols:
        return None
    ranked = _rank_by_intent(numeric_cols, _MEASURE_PRIORITY, business_intent)
    return ranked[0] if ranked else numeric_cols[0]


def _pick_dimensions(df: pd.DataFrame, measure: str | None, business_intent: str = "", max_dims: int = 2) -> list:
    """Pick 1-2 categorical columns to group the measure by, preferring ones
    the question names explicitly.

    Only text/boolean/datetime columns are treated as dimension candidates —
    a low-cardinality NUMERIC column (e.g. `quantity`) is never mistaken for
    a category just because it happens to have few unique values. If a
    dataset genuinely has no categorical column at all, low-cardinality
    numeric columns are used as a last resort so the report can still group
    by something.
    """
    def is_categorical(col: str) -> bool:
        # pandas >= 3.0 defaults text columns to StringDtype rather than the
        # legacy numpy `object` dtype, so `dtype == object` alone misses
        # them entirely -- is_string_dtype() covers both.
        return (
            pd.api.types.is_string_dtype(df[col])
            or df[col].dtype == bool
            or pd.api.types.is_datetime64_any_dtype(df[col])
        )

    def base_filter(c: str) -> bool:
        return c != measure and not _EXCLUDE_COLUMNS.search(c)

    candidate_cols = [c for c in df.columns if base_filter(c) and is_categorical(c)]
    if not candidate_cols:
        threshold = max(20, int(len(df) * 0.3))
        candidate_cols = [c for c in df.columns if base_filter(c) and df[c].nunique() <= threshold]

    ranked = _rank_by_intent(candidate_cols, _DIMENSION_PRIORITY, business_intent)
    return ranked[:max_dims]


def _pick_aggregation(business_intent: str) -> tuple:
    """Infer the SQL aggregation function + a human label from the question.

    Defaults to SUM/"total" (the original behaviour) unless the question
    contains an unambiguous statistical keyword. Words like "highest"/"top"
    on their own only flip sort direction (see _pick_sort_direction) — they
    do NOT switch the aggregation function, since "highest sales" almost
    always means "highest total sales", not "the single highest sale."
    """
    intent = (business_intent or "").lower()
    if _COUNT_RE.search(intent):
        return "COUNT", "count of"
    if _AVG_RE.search(intent):
        return "AVG", "average"
    if _MAX_VALUE_RE.search(intent):
        return "MAX", "maximum"
    if _MIN_VALUE_RE.search(intent):
        return "MIN", "minimum"
    return "SUM", "total"


def _pick_sort_direction(business_intent: str) -> str:
    """Infer ORDER BY direction; defaults to descending ("top N")."""
    return "ASC" if _ASCENDING_RE.search((business_intent or "").lower()) else "DESC"


def generate_chart_from_spec(df: pd.DataFrame, chart_spec: dict, chart_id: int) -> str:
    """Render a single Plotly bar chart from a computed chart spec dict."""
    try:
        x_col = chart_spec["x_column"]
        y_col = chart_spec["y_column"]
        title = chart_spec.get("title", f"Chart {chart_id}")
        fig = go.Figure(data=[go.Bar(x=df[x_col], y=df[y_col], marker_color="#667eea")])
        fig.update_layout(title=title, xaxis_title=x_col, yaxis_title=y_col, height=450, template="plotly_white")
        return fig.to_html(include_plotlyjs="cdn", div_id=f"chart_{chart_id}")
    except Exception as e:
        print(f"[REPORTER] Error generating chart {chart_id}: {e}")
        return ""


def _build_analysis(
    result_df: pd.DataFrame,
    dims: list,
    value_col: str | None,
    business_intent: str,
    agg_label: str = "total",
    direction: str = "DESC",
    is_count: bool = False,
) -> dict:
    """Build the answer/chart-spec/detailed-analysis structure from computed results."""
    if not dims or not value_col or result_df.empty:
        return {
            "direct_answer": {
                "question": business_intent,
                "answer": (
                    "The Gold data did not contain a clear numeric measure and categorical "
                    "dimension pair to answer this automatically. Review the Gold table directly."
                ),
                "why": "No suitable measure/dimension pair was detected by the automatic heuristics.",
                "approach": "N/A",
            },
            "charts": [],
            "detailed_analysis": "No further automatic analysis available.",
        }

    primary_dim = dims[0]
    top_row = result_df.iloc[0]
    ranked_row_label = "lowest" if direction == "ASC" else "top"

    if is_count:
        value_label = "count of records"
        value_display = lambda v: f"{int(v):,}"
    else:
        measure_label = value_col.replace("_", " ")
        value_label = measure_label if measure_label.lower().startswith(agg_label) else f"{agg_label} {measure_label}"
        value_display = lambda v: f"{v:,.2f}"

    lines = [f"{row[primary_dim]}: {value_display(row[value_col])}" for _, row in result_df.head(5).iterrows()]
    answer = (
        f"By {value_label}, grouped by {primary_dim.replace('_', ' ')}: "
        + "; ".join(lines) + "."
    )

    detail_lines = [
        f"Across {len(result_df)} distinct {primary_dim.replace('_', ' ')} value(s), the {ranked_row_label} "
        f"performer is '{top_row[primary_dim]}' with {value_display(top_row[value_col])} {value_label}.",
    ]
    if len(dims) > 1:
        detail_lines.append(
            f"A secondary breakdown by {dims[1].replace('_', ' ')} is available in the Gold "
            "table for deeper drill-down."
        )
    detail_lines.append(
        "This report was generated by rule-based heuristics (no LLM/API key): the measure, "
        "dimension, aggregation, and sort order were inferred from column names/dtypes and "
        "keywords in the business question, not by full natural-language understanding."
    )

    return {
        "direct_answer": {
            "question": business_intent,
            "answer": answer,
            "why": None,  # filled in by generate_report, which knows the actual SQL/agg used
            "approach": None,
        },
        "charts": [{
            "title": f"{value_label.title()} by {primary_dim.replace('_', ' ').title()}",
            "x_column": primary_dim,
            "y_column": value_col,
        }],
        "detailed_analysis": " ".join(detail_lines),
    }


# ---------------------------------------------------------------------------
# Public entry point — I/O contract UNCHANGED
# ---------------------------------------------------------------------------

def generate_report(
    gold_files: list,
    business_intent: str,
    run_id: str,
    task_description: str,
) -> str:
    """Reporter agent entry point — deterministic, no LLM required.

    Args:
        gold_files: Gold Parquet file paths to analyse.
        business_intent: The business question. Used to infer the measure,
            dimension(s), aggregation function, and sort direction via
            keyword/name matching (see module docstring) — not full NL
            understanding.
        run_id: Unique identifier for this pipeline run.
        task_description: High-level goal (kept for logging parity; unused for logic).

    Returns:
        str: Path to the saved HTML report.
    """
    trace = AgentTrace("reporter", run_id)
    trace.set_input(gold_files=gold_files, business_intent=business_intent)

    print(f"[REPORTER] Starting report generation for run_id: {run_id}")
    audit = AuditLogger(run_id)
    audit.log("reporter", "started", gold_files=gold_files, intent=business_intent)

    if not gold_files:
        audit.log("reporter", "error", detail="No gold files to report on")
        trace.fail("No gold files provided")
        return ""

    trace.set_plan(
        "Load Gold tables into DuckDB; infer measure/dimension/aggregation/sort direction "
        "from the business question and column names; GROUP BY and render."
    )

    agg_func, agg_label = _pick_aggregation(business_intent)
    direction = _pick_sort_direction(business_intent)

    conn = duckdb.connect(":memory:")
    combined_df = pd.DataFrame()
    measure: str | None = None
    dims: list = []
    value_col: str | None = None
    is_count = agg_func == "COUNT"
    try:
        frames = [pd.read_parquet(fp) for fp in gold_files]
        combined_df = pd.concat(frames, ignore_index=True, sort=False) if frames else pd.DataFrame()
        conn.register("gold", combined_df)

        measure = _pick_measure(combined_df, business_intent)
        dims = _pick_dimensions(combined_df, measure, business_intent)
        value_col = "record_count" if is_count else measure
        trace.log_step(
            f"Selected aggregation={agg_func} measure={measure!r} dimensions={dims!r} direction={direction}"
        )

        result_df = pd.DataFrame()
        sql_query = "-- No suitable dimension found in the Gold table"
        if dims and (measure or is_count):
            primary_dim = dims[0]
            agg_expr = "COUNT(*)" if is_count else f'{agg_func}("{measure}")'
            sql_query = (
                f'SELECT "{primary_dim}", {agg_expr} AS "{value_col}" FROM gold '
                f'GROUP BY "{primary_dim}" ORDER BY "{value_col}" {direction} LIMIT 10'
            )
            try:
                result_df = conn.execute(sql_query).fetchdf()
            except Exception as e:
                print(f"[REPORTER] DuckDB query failed, falling back to pandas groupby: {e}")
                if is_count:
                    result_df = combined_df.groupby(primary_dim, dropna=False).size().reset_index(name=value_col)
                else:
                    pandas_agg = {"SUM": "sum", "AVG": "mean", "MAX": "max", "MIN": "min"}[agg_func]
                    result_df = combined_df.groupby(primary_dim, dropna=False)[measure].agg(pandas_agg).reset_index()
                    result_df.columns = [primary_dim, value_col]
                result_df = result_df.sort_values(value_col, ascending=(direction == "ASC")).head(10)
            trace.log_step(f"Query returned {len(result_df)} row(s)")
    finally:
        conn.close()

    analysis_result = _build_analysis(result_df, dims, value_col, business_intent, agg_label, direction, is_count)

    direct_answer = analysis_result.get("direct_answer", {})
    if direct_answer.get("approach") is None:
        if dims:
            agg_verb = _AGG_VERB[agg_func]
            direct_answer["why"] = (
                f"Computed by grouping the Gold table by {dims[0]} and {agg_verb} "
                f"{'all rows' if is_count else measure}, sorted {_DIRECTION_WORD[direction]}."
            )
            direct_answer["approach"] = sql_query
        else:
            direct_answer["why"] = "No suitable measure/dimension pair was detected by the automatic heuristics."
            direct_answer["approach"] = "N/A"

    charts_html = []
    for idx, chart_spec in enumerate(analysis_result.get("charts", []), 1):
        chart_html = generate_chart_from_spec(result_df, chart_spec, idx)
        if chart_html:
            charts_html.append(chart_html)
    print(f"[REPORTER] Generated {len(charts_html)} chart(s)")

    detailed_analysis = analysis_result.get("detailed_analysis", "No additional analysis provided.")

    answer_html = f"""
    <div class="answer-section">
        <p>{direct_answer.get('answer', 'No answer provided')}</p>
    </div>
    """

    query_code_escaped = sql_query.replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    approach_html = f"""
    <div class="approach-section">
        <h3>Query Code</h3>
        <pre class="code-block"><code>{query_code_escaped}</code></pre>
        <h3>Query Description</h3>
        <p>{direct_answer.get('approach', 'No methodology provided')}</p>
    </div>
    """

    charts_section = "\n".join(charts_html) if charts_html else "<p>No charts generated.</p>"

    full_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Executive Report - {run_id[:8]}</title>
        <script src="https://cdn.plot.ly/plotly-latest.min.js"></script>
        <style>
            body {{
                font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
                max-width: 1200px;
                margin: 0 auto;
                padding: 20px;
                background-color: #f5f5f5;
                color: #333;
            }}
            .header {{
                background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white;
                padding: 30px;
                border-radius: 10px;
                margin-bottom: 30px;
            }}
            .header h1 {{ margin: 0; font-size: 32px; }}
            .header p {{ margin: 10px 0 0 0; opacity: 0.9; }}
            .section {{
                background: white;
                padding: 25px;
                border-radius: 8px;
                margin-bottom: 20px;
                box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            }}
            .section h2 {{
                color: #667eea;
                border-bottom: 3px solid #667eea;
                padding-bottom: 10px;
                margin-top: 0;
            }}
            .answer-section {{
                background: #e8f4f8;
                padding: 20px;
                border-radius: 8px;
                border-left: 4px solid #28a745;
            }}
            .answer-section p {{ margin: 0; line-height: 1.6; font-size: 16px; color: #333; }}
            .approach-section {{ margin: 20px 0; }}
            .approach-section h3 {{ color: #667eea; font-size: 16px; margin: 20px 0 10px 0; }}
            .code-block {{
                background: #f5f5f5;
                border: 1px solid #ddd;
                border-radius: 6px;
                padding: 15px;
                overflow-x: auto;
                font-family: 'Courier New', monospace;
                font-size: 13px;
                line-height: 1.4;
                color: #333;
                margin: 0 0 15px 0;
            }}
            .code-block code {{ color: #667eea; }}
            .approach-section p {{ line-height: 1.6; color: #555; margin: 0 0 15px 0; }}
            .chart-container {{ margin: 20px 0; }}
            .footer {{
                text-align: center;
                color: #999;
                margin-top: 40px;
                padding-top: 20px;
                border-top: 1px solid #eee;
            }}
        </style>
    </head>
    <body>
        <div class="header">
            <h1>Executive Report</h1>
            <p><strong>Business Question:</strong> {business_intent}</p>
        </div>
        <div class="section">
            <h2>Answer</h2>
            {answer_html}
        </div>
        <div class="section">
            <h2>Approach &amp; Query</h2>
            {approach_html}
        </div>
        <div class="section">
            <h2>Visual Evidence</h2>
            <div class="chart-container">
                {charts_section}
            </div>
        </div>
        <div class="section">
            <h2>Detailed Analysis</h2>
            <p style="line-height: 1.8;">{detailed_analysis}</p>
        </div>
        <div class="footer">
            <p>Generated by IDAMP (Intent-Driven Agentic Medallion Pipeline) — deterministic, no LLM/API key</p>
            <p>Report Date: {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M:%S')}</p>
        </div>
    </body>
    </html>
    """

    report_path = str(REPORTS_DIR / f"report_{run_id[:8]}.html")
    print(f"[REPORTER] Saving HTML report → {report_path}")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(full_html)

    json_path = str(REPORTS_DIR / f"report_{run_id[:8]}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(analysis_result, f, indent=2)

    store_document(
        doc_id=f"report_{run_id}",
        text=json.dumps(analysis_result),
        metadata={"type": "report", "run_id": run_id, "intent": business_intent},
    )

    audit.log("reporter", "completed", report_path=report_path)
    trace.set_output(report_path=report_path).complete()
    print(f"[REPORTER] Done — {report_path}")
    return report_path
