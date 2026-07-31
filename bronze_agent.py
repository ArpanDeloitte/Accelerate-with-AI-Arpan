"""Bronze layer agent — deterministic version (no LLM, no API key required).

Reads each raw CSV, applies the approved Bronze STTM rules (column renaming,
type casting, metadata injection), and writes Bronze Parquet artifacts.

I/O contract (UNCHANGED — UI and orchestrator safe):
    execute_bronze(input_files, sttm_path, run_id, task_description) -> list[str]
"""

import os
import pandas as pd
from datetime import datetime, timezone
from core.config import BRONZE_DIR
from core.audit import AuditLogger
from core.observability import AgentTrace


# ---------------------------------------------------------------------------
# Pure Python execution — unchanged from the original implementation
# ---------------------------------------------------------------------------

def _apply_bronze_rules(input_files: list[str], sttm_path: str, run_id: str) -> list[str]:
    """Read each input CSV, apply STTM rename/type/metadata rules, write Bronze Parquet."""
    audit = AuditLogger(run_id)
    audit.log("bronze_agent", "started", input_files=input_files, sttm_path=sttm_path)

    sttm_df = pd.read_csv(sttm_path).fillna("")
    output_paths = []

    for file_path in input_files:
        df = pd.read_csv(file_path)
        original_shape = df.shape
        file_name = os.path.basename(file_path)
        file_stem = os.path.splitext(file_name)[0]
        file_rules = (
            sttm_df[sttm_df["source_table"].astype(str).isin(["", file_name, file_stem])]
            if "source_table" in sttm_df.columns
            else sttm_df
        )

        for _, rule in file_rules.iterrows():
            source_col = str(rule.get("source_column", "")).strip()
            target_col = str(rule.get("target_column", "")).strip()
            logic = str(rule.get("transformation_logic", "")).lower()

            if target_col and target_col.lower() in {"_load_timestamp", "load_timestamp"}:
                df[target_col] = datetime.now(timezone.utc).isoformat()
                continue
            if target_col and target_col.lower() in {"_source_file", "source_file"}:
                df[target_col] = file_path
                continue
            if source_col and target_col and source_col in df.columns and source_col != target_col:
                df = df.rename(columns={source_col: target_col})

            working_col = target_col if target_col in df.columns else source_col
            if not working_col or working_col not in df.columns:
                continue

            try:
                if "text format" in logic or ("convert" in logic and "text" in logic):
                    df[working_col] = df[working_col].astype(str)
                elif "integer" in logic or "whole number" in logic:
                    df[working_col] = pd.to_numeric(df[working_col], errors="coerce").astype("Int64")
                elif "float" in logic or "decimal" in logic or "numeric" in logic:
                    df[working_col] = pd.to_numeric(df[working_col], errors="coerce")
                elif "date" in logic or "datetime" in logic:
                    df[working_col] = pd.to_datetime(df[working_col], errors="coerce")
            except (ValueError, TypeError):
                pass

        if "_load_timestamp" not in df.columns and "load_timestamp" not in df.columns:
            df["_load_timestamp"] = datetime.now(timezone.utc).isoformat()
        if "_source_file" not in df.columns and "source_file" not in df.columns:
            df["_source_file"] = file_path

        filename = os.path.basename(file_path).replace(".csv", "_bronze.parquet")
        output_path = str(BRONZE_DIR / filename)
        df.to_parquet(output_path, index=False)
        output_paths.append(output_path)

        audit.log(
            "bronze_agent", "file_processed",
            input_file=file_path,
            output_file=output_path,
            input_shape=list(original_shape),
            output_shape=list(df.shape),
        )

    audit.log("bronze_agent", "completed", output_files=output_paths)
    return output_paths


# ---------------------------------------------------------------------------
# Public entry point — I/O contract UNCHANGED
# ---------------------------------------------------------------------------

def execute_bronze(
    input_files: list[str],
    sttm_path: str,
    run_id: str,
    task_description: str,
) -> list[str]:
    """Bronze agent entry point — deterministic, no LLM required.

    Args:
        input_files: Raw CSV file paths to ingest.
        sttm_path: Path to the approved Bronze STTM CSV.
        run_id: Unique identifier for this pipeline run.
        task_description: High-level goal (kept for logging parity; unused for logic).

    Returns:
        list[str]: Bronze Parquet output file paths.
    """
    trace = AgentTrace("bronze_agent", run_id)
    trace.set_input(input_files=input_files, sttm_path=sttm_path)
    trace.set_plan("Apply approved Bronze STTM rules to each raw CSV: rename, type-cast, inject lineage metadata.")

    print(f"[BRONZE] Ingesting {len(input_files)} file(s)")
    try:
        output_paths = _apply_bronze_rules(input_files, sttm_path, run_id)
    except Exception as e:
        trace.fail(str(e))
        raise

    trace.log_step(f"Wrote {len(output_paths)} Bronze Parquet file(s)")
    trace.set_output(output_paths=output_paths).complete()
    return output_paths
