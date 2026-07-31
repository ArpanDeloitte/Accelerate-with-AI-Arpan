"""STTM (Source-to-Target Mapping) generator — deterministic version (no LLM).

Bronze, Silver, and Gold rules are derived from the profiled schema and the
Bronze/Silver Parquet metadata via fixed heuristics instead of an LLM call.
Gold maps every Silver column straight through into a single wide
`gold_output` table — `gold_agent.py` auto-joins the Silver tables on shared
`*_id` columns, and `reporter.py` picks the measure/dimension to answer the
business question at query time.

I/O contract (UNCHANGED — orchestrator safe):
    generate_bronze_sttm(profile_path, run_id, task_description) -> str
    generate_silver_sttm(bronze_output_paths, bronze_sttm_path, run_id, task_description) -> str
    generate_gold_sttm(silver_output_paths, silver_sttm_path, business_intent, run_id, task_description) -> str
"""

import json
import os
import re
import pandas as pd
from core.config import STTM_DIR
from core.audit import AuditLogger
from core.observability import AgentTrace

_ID_PATTERN = re.compile(r"(^|_)id$")
_DATE_PATTERN = re.compile(r"date|_dt$|timestamp")


# ---------------------------------------------------------------------------
# Bronze — mechanical column mapping, every source column covered
# ---------------------------------------------------------------------------

def _bronze_rows_for_dataset(dataset_name: str, columns: dict) -> list[dict]:
    rows = []
    for col, info in columns.items():
        dtype = info.get("dtype", "")
        col_lower = col.lower()
        if "int" in dtype:
            logic = "Convert to integer whole number"
        elif "float" in dtype:
            logic = "Convert to float decimal numeric"
        elif _DATE_PATTERN.search(col_lower):
            logic = "Convert to date format"
        else:
            logic = "Convert to text format"
        rows.append({
            "source_schema": "", "source_table": dataset_name, "source_column": col,
            "target_schema": "", "target_table": dataset_name, "target_column": col,
            "transformation_type": "Indirect", "transformation_logic": logic,
        })
    rows.append({
        "source_schema": "", "source_table": dataset_name, "source_column": "",
        "target_schema": "", "target_table": dataset_name, "target_column": "_load_timestamp",
        "transformation_type": "Indirect",
        "transformation_logic": "Current UTC timestamp injected at load time",
    })
    rows.append({
        "source_schema": "", "source_table": dataset_name, "source_column": "",
        "target_schema": "", "target_table": dataset_name, "target_column": "_source_file",
        "transformation_type": "Indirect",
        "transformation_logic": "Source file path injected at load time",
    })
    return rows


def generate_bronze_sttm(profile_path: str, run_id: str, task_description: str) -> str:
    """Bronze STTM — intent-agnostic, maps every profiled column mechanically."""
    print(f"[STTM] Generating Bronze STTM for run_id: {run_id}")
    trace = AgentTrace("sttm_bronze", run_id)
    trace.set_input(profile_path=profile_path)
    audit = AuditLogger(run_id)
    audit.log("sttm_generator", "started_bronze", profile_path=profile_path)

    with open(profile_path, "r", encoding="utf-8") as f:
        profile = json.load(f)

    trace.set_plan("Map every source column to Bronze mechanically (rename/type-cast); add lineage metadata rows.")
    rows: list[dict] = []
    for dataset_name, ds in profile.get("datasets", {}).items():
        rows.extend(_bronze_rows_for_dataset(dataset_name, ds.get("columns", {})))
    trace.log_step(f"Built {len(rows)} Bronze STTM row(s) across {len(profile.get('datasets', {}))} dataset(s)")

    sttm_path = str(STTM_DIR / f"sttm_bronze_{run_id[:8]}.csv")
    pd.DataFrame(rows).to_csv(sttm_path, index=False)
    audit.log("sttm_generator", "completed_bronze", output_file=sttm_path, row_count=len(rows))
    trace.set_output(sttm_path=sttm_path, row_count=len(rows)).complete()
    print(f"[STTM] Bronze STTM saved: {sttm_path} ({len(rows)} rows)")
    return sttm_path


# ---------------------------------------------------------------------------
# Silver — cleansing rules per Bronze column, surrogate key, dedup
# ---------------------------------------------------------------------------

def _silver_rows_for_table(bronze_stem: str, target_stem: str, columns: dict) -> list[dict]:
    """Build cleansing rows for one Bronze table.

    `bronze_stem` (e.g. "products_bronze") must match the Bronze Parquet's
    filename stem so silver_agent.py's per-file STTM filter picks these rows
    up; `target_stem` (e.g. "products") is only used for readability.
    """
    rows = [{
        "source_schema": "", "source_table": bronze_stem, "source_column": "",
        "target_schema": "", "target_table": target_stem, "target_column": f"pk_{target_stem}_silver_id",
        "transformation_type": "Indirect",
        "transformation_logic": "Auto-generated sequential surrogate primary key starting from 1",
    }]

    # First id-like column, in original column order, is treated as this
    # table's own natural identifier (e.g. transaction_id for a fact table).
    dedup_key = next((c for c in columns if _ID_PATTERN.search(c.lower())), None)

    for col, dtype in columns.items():
        if col.startswith("_"):
            continue
        col_lower = col.lower()
        if _ID_PATTERN.search(col_lower):
            logic = "Cast to text/string type; no null handling (identifier column)"
        elif _DATE_PATTERN.search(col_lower):
            logic = "Fill null with mode then standardise date format to YYYY-MM-DD"
        elif "float" in dtype or "int" in dtype:
            logic = "Fill null values with mean; cast to numeric type"
        else:
            logic = "Fill null values with mode; strip whitespace and standardise text format"
        rows.append({
            "source_schema": "", "source_table": bronze_stem, "source_column": col,
            "target_schema": "", "target_table": target_stem, "target_column": col,
            "transformation_type": "Indirect", "transformation_logic": logic,
        })

    if dedup_key:
        rows.append({
            "source_schema": "", "source_table": bronze_stem, "source_column": dedup_key,
            "target_schema": "", "target_table": target_stem, "target_column": dedup_key,
            "transformation_type": "Indirect",
            "transformation_logic": f"Deduplicate rows based on {dedup_key}",
        })
    return rows


def generate_silver_sttm(
    bronze_output_paths: list[str],
    bronze_sttm_path: str,
    run_id: str,
    task_description: str,
) -> str:
    """Silver STTM — intent-agnostic, standard cleansing applied to every column."""
    print(f"[STTM] Generating Silver STTM for run_id: {run_id}")
    trace = AgentTrace("sttm_silver", run_id)
    trace.set_input(bronze_paths=bronze_output_paths)
    audit = AuditLogger(run_id)
    audit.log("sttm_generator", "started_silver", bronze_paths=bronze_output_paths)

    trace.set_plan("Cleanse every Bronze column: null handling, dedup, type/date standardisation; surrogate key first.")
    rows: list[dict] = []
    for bp in bronze_output_paths:
        bronze_stem = os.path.splitext(os.path.basename(bp))[0]  # e.g. "products_bronze"
        target_stem = bronze_stem.replace("_bronze", "")  # e.g. "products"
        df = pd.read_parquet(bp)
        columns = {c: str(df[c].dtype) for c in df.columns}
        rows.extend(_silver_rows_for_table(bronze_stem, target_stem, columns))
    trace.log_step(f"Built {len(rows)} Silver STTM row(s) across {len(bronze_output_paths)} Bronze table(s)")

    sttm_path = str(STTM_DIR / f"sttm_silver_{run_id[:8]}.csv")
    pd.DataFrame(rows).to_csv(sttm_path, index=False)
    audit.log("sttm_generator", "completed_silver", output_file=sttm_path, row_count=len(rows))
    trace.set_output(sttm_path=sttm_path, row_count=len(rows)).complete()
    print(f"[STTM] Silver STTM saved: {sttm_path} ({len(rows)} rows)")
    return sttm_path


# ---------------------------------------------------------------------------
# Gold — passthrough every Silver column into one wide analytics table
# ---------------------------------------------------------------------------

def generate_gold_sttm(
    silver_output_paths: list[str],
    silver_sttm_path: str,
    business_intent: str,
    run_id: str,
    task_description: str,
) -> str:
    """Gold STTM — deterministic passthrough into a single wide analytics table.

    Without an LLM to interpret free-text business intent, every Silver
    column is mapped straight through into one `gold_output` table.
    gold_agent.py auto-joins the Silver tables on shared *_id columns, and
    reporter.py picks the measure/dimension columns to answer the question.
    """
    print(f"[STTM] Generating Gold STTM for run_id: {run_id}")
    trace = AgentTrace("sttm_gold", run_id)
    trace.set_input(silver_paths=silver_output_paths, business_intent=business_intent)
    audit = AuditLogger(run_id)
    audit.log("sttm_generator", "started_gold", silver_paths=silver_output_paths, business_intent=business_intent)

    trace.set_plan("Passthrough every Silver column into one Gold table; gold_agent auto-joins on shared id columns.")
    rows: list[dict] = [{
        "source_schema": "", "source_table": "", "source_column": "",
        "target_schema": "", "target_table": "gold_output", "target_column": "pk_gold_id",
        "transformation_type": "Indirect",
        "transformation_logic": "Auto-generated sequential surrogate primary key starting from 1",
    }]
    for sp in silver_output_paths:
        stem = os.path.basename(sp).replace("_silver.parquet", "")
        df = pd.read_parquet(sp)
        for col in df.columns:
            rows.append({
                "source_schema": "", "source_table": stem, "source_column": col,
                "target_schema": "", "target_table": "gold_output", "target_column": col,
                "transformation_type": "Direct", "transformation_logic": "Passthrough",
            })
    trace.log_step(f"Built {len(rows)} Gold STTM row(s) across {len(silver_output_paths)} Silver table(s)")

    sttm_path = str(STTM_DIR / f"sttm_gold_{run_id[:8]}.csv")
    pd.DataFrame(rows).to_csv(sttm_path, index=False)
    audit.log("sttm_generator", "completed_gold", output_file=sttm_path, row_count=len(rows))
    trace.set_output(sttm_path=sttm_path, row_count=len(rows)).complete()
    print(f"[STTM] Gold STTM saved: {sttm_path} ({len(rows)} rows)")
    return sttm_path
