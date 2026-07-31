"""Silver layer agent — deterministic version (no LLM, no API key required).

Reads each Bronze Parquet, applies the approved Silver STTM cleansing rules
(null handling, deduplication, type casting, date standardisation), injects
a surrogate key, and writes Silver Parquet artifacts.

I/O contract (UNCHANGED — UI and orchestrator safe):
    execute_silver(input_files, sttm_path, run_id, task_description) -> list[str]
"""

import os
import pandas as pd
from core.config import SILVER_DIR
from core.audit import AuditLogger
from core.observability import AgentTrace


# ---------------------------------------------------------------------------
# Pure Python execution — unchanged from the original implementation
# ---------------------------------------------------------------------------

def _apply_silver_rules(input_files: list[str], sttm_path: str, run_id: str) -> list[str]:
    """Read each Bronze Parquet, apply STTM cleansing rules, inject surrogate key, write Silver Parquet."""
    audit = AuditLogger(run_id)
    audit.log("silver_agent", "started", input_files=input_files, sttm_path=sttm_path)

    sttm_df = pd.read_csv(sttm_path).fillna("")
    output_paths = []

    for file_path in input_files:
        df = pd.read_parquet(file_path)
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

            if source_col and target_col and source_col in df.columns and source_col != target_col:
                df = df.rename(columns={source_col: target_col})

            working_col = target_col if target_col in df.columns else source_col

            if "deduplic" in logic:
                subset = [working_col] if working_col in df.columns else None
                df = df.drop_duplicates(subset=subset)
                continue

            if not working_col or working_col not in df.columns:
                continue

            try:
                if "drop null" in logic or "remove null" in logic:
                    df = df.dropna(subset=[working_col])
                elif "fill null" in logic and "mean" in logic:
                    df[working_col] = df[working_col].fillna(
                        pd.to_numeric(df[working_col], errors="coerce").mean()
                    )
                elif "fill null" in logic and "median" in logic:
                    df[working_col] = df[working_col].fillna(
                        pd.to_numeric(df[working_col], errors="coerce").median()
                    )
                elif "fill null" in logic and "mode" in logic:
                    mode_val = df[working_col].mode()
                    if not mode_val.empty:
                        df[working_col] = df[working_col].fillna(mode_val.iloc[0])
                elif "fill null" in logic or "default" in logic:
                    df[working_col] = df[working_col].fillna("")

                if "date" in logic or "datetime" in logic:
                    _date_fmts = [
                        "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
                        "%Y%m%d", "%d-%b-%Y", "%d-%B-%Y",
                        "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S",
                        "%m-%d-%Y", "%d.%m.%Y",
                    ]
                    _parsed = None
                    for _fmt in _date_fmts:
                        try:
                            _try = pd.to_datetime(df[working_col], format=_fmt, errors="coerce")
                            if _try.notna().sum() > 0:
                                _parsed = _try
                                break
                        except Exception:
                            continue
                    df[working_col] = _parsed if _parsed is not None else pd.to_datetime(
                        df[working_col], errors="coerce"
                    )
                elif "integer" in logic:
                    df[working_col] = pd.to_numeric(df[working_col], errors="coerce").astype("Int64")
                elif "float" in logic or "decimal" in logic or "numeric" in logic:
                    df[working_col] = pd.to_numeric(df[working_col], errors="coerce")
                elif "text" in logic:
                    df[working_col] = df[working_col].astype(str)

                if "lowercase" in logic:
                    df[working_col] = df[working_col].astype(str).str.lower()
                elif "uppercase" in logic:
                    df[working_col] = df[working_col].astype(str).str.upper()
                elif "title case" in logic:
                    df[working_col] = df[working_col].astype(str).str.title()

                if "strip" in logic or "trim" in logic:
                    df[working_col] = df[working_col].astype(str).str.strip()
            except (ValueError, TypeError):
                pass

        # Inject surrogate primary key as first column
        pk_col = f"pk_{file_stem}_silver_id"
        if pk_col not in df.columns:
            df.insert(0, pk_col, range(1, len(df) + 1))

        # Filter columns to approved Silver targets + system metadata columns
        # Be defensive: allow missing 'target_column' by falling back to an empty set.
        if "target_column" in file_rules.columns:
            approved_target_cols = set(file_rules["target_column"].unique())
        else:
            approved_target_cols = set()
        columns_to_keep = [
            c for c in df.columns
            if c in approved_target_cols or c.startswith("_") or c.startswith("pk_")
        ]
        if pk_col in columns_to_keep:
            columns_to_keep.remove(pk_col)
            columns_to_keep.insert(0, pk_col)
        df = df[columns_to_keep]

        filename = os.path.basename(file_path).replace("_bronze.parquet", "_silver.parquet")
        output_path = str(SILVER_DIR / filename)
        df.to_parquet(output_path, index=False)
        output_paths.append(output_path)

        audit.log(
            "silver_agent", "file_processed",
            input_file=file_path,
            output_file=output_path,
            input_shape=list(original_shape),
            output_shape=list(df.shape),
        )

    audit.log("silver_agent", "completed", output_files=output_paths)
    return output_paths


# ---------------------------------------------------------------------------
# Public entry point — I/O contract UNCHANGED
# ---------------------------------------------------------------------------

def execute_silver(
    input_files: list[str],
    sttm_path: str,
    run_id: str,
    task_description: str,
) -> list[str]:
    """Silver agent entry point — deterministic, no LLM required.

    Args:
        input_files: Bronze Parquet file paths to cleanse.
        sttm_path: Path to the approved Silver STTM CSV.
        run_id: Unique identifier for this pipeline run.
        task_description: High-level goal (kept for logging parity; unused for logic).

    Returns:
        list[str]: Silver Parquet output file paths.
    """
    trace = AgentTrace("silver_agent", run_id)
    trace.set_input(input_files=input_files, sttm_path=sttm_path)
    trace.set_plan("Apply approved Silver STTM rules: null handling, dedup, type/date standardisation, surrogate key.")

    print(f"[SILVER] Cleansing {len(input_files)} file(s)")
    try:
        output_paths = _apply_silver_rules(input_files, sttm_path, run_id)
    except Exception as e:
        trace.fail(str(e))
        raise

    trace.log_step(f"Wrote {len(output_paths)} Silver Parquet file(s)")
    trace.set_output(output_paths=output_paths).complete()
    return output_paths
