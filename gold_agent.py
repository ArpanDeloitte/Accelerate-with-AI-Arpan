"""Gold layer agent — deterministic version (no LLM, no API key required).

Loads Silver Parquet files as source tables, groups the approved Gold STTM
rules by target table, auto-joins sources on shared `*_id` columns, applies
any renames/aggregations declared in the STTM, injects a surrogate key, and
writes Gold Parquet artifacts.

I/O contract:
    execute_gold(input_files, sttm_path, run_id, task_description) -> list[str]
"""

import os
import pandas as pd
from core.config import GOLD_DIR
from core.audit import AuditLogger
from core.observability import AgentTrace


# ---------------------------------------------------------------------------
# Pure Python execution — unchanged from the original implementation
# ---------------------------------------------------------------------------

def _apply_gold_rules(input_files: list[str], sttm_path: str, run_id: str) -> list[str]:
    """Load Silver tables, group STTM by target_table, apply joins/renames/aggs, write Gold Parquet."""
    audit = AuditLogger(run_id)
    audit.log("gold_agent", "started", input_files=input_files, sttm_path=sttm_path)

    sttm_df = pd.read_csv(sttm_path).fillna("")

    # Load all Silver files into a dict keyed by source table name
    source_dataframes = {}
    for file_path in input_files:
        source_name = os.path.basename(file_path).replace("_silver.parquet", "")
        source_dataframes[source_name] = pd.read_parquet(file_path)

    print(f"[GOLD] Loaded {len(source_dataframes)} Silver source tables: {list(source_dataframes.keys())}")

    target_tables = sttm_df.groupby("target_table")
    print(f"[GOLD] Creating {len(target_tables)} Gold target tables: {list(target_tables.groups.keys())}")

    output_paths = []

    for target_table_name, table_rules in target_tables:
        target_table_name = str(target_table_name).strip()
        if not target_table_name:
            continue

        print(f"[GOLD] Processing target table: {target_table_name} ({len(table_rules)} rules)")

        source_tables_needed = table_rules["source_table"].unique()
        source_tables_needed = [
            str(s).strip().replace("_silver.parquet", "").replace("_silver", "")
            for s in source_tables_needed if str(s).strip()
        ]
        available_sources = {
            src: source_dataframes[src]
            for src in source_tables_needed
            if src in source_dataframes
        }

        if not available_sources:
            print(f"[GOLD] No matching source data for {target_table_name}, skipping")
            continue

        available_sources_list = list(available_sources.items())
        df = available_sources_list[0][1].copy()

        if len(available_sources) > 1:
            remaining = dict(available_sources_list[1:])
            metadata_cols = {"_load_timestamp", "_source_file", "_row_inserted", "_row_updated"}
            # Repeatedly merge in whichever remaining table currently shares an
            # id column with the accumulated result, and defer tables that
            # don't share a key yet (e.g. two unrelated dimension tables) —
            # a later merge may introduce the missing shared key. Only union
            # (concat) whatever is left once no more merges are possible, so
            # table order in the STTM never silently breaks the join.
            while remaining:
                merged_any = False
                for source_name in list(remaining.keys()):
                    source_df = remaining[source_name]
                    common_cols = [col for col in df.columns if col in source_df.columns]
                    id_cols = [
                        col for col in common_cols
                        if (col.endswith("_id") or col == "id") and col not in metadata_cols
                    ]
                    if id_cols:
                        df = df.merge(source_df, on=id_cols, how="outer", suffixes=("", "_dup"))
                        dup_cols = [col for col in df.columns if col.endswith("_dup")]
                        if dup_cols:
                            df = df.drop(columns=dup_cols)
                        del remaining[source_name]
                        merged_any = True
                if not merged_any:
                    for source_df in remaining.values():
                        df = pd.concat([df, source_df], ignore_index=True, sort=False)
                    remaining = {}

        # Apply transformations
        group_by_cols: list[str] = []
        agg_map: dict[str, str] = {}
        rename_map: dict[str, str] = {}

        for _, rule in table_rules.iterrows():
            source_col = str(rule.get("source_column", "")).strip()
            target_col = str(rule.get("target_column", "")).strip()
            logic = str(rule.get("transformation_logic", "")).lower()
            transformation_type = str(rule.get("transformation_type", "")).lower()

            if source_col and target_col and source_col in df.columns and source_col != target_col:
                rename_map[source_col] = target_col

            if source_col and source_col in df.columns and (
                transformation_type == "direct" or "group by" in logic
            ):
                if source_col not in group_by_cols:
                    group_by_cols.append(source_col)

            if source_col and source_col in df.columns:
                if "sum" in logic:
                    agg_map[source_col] = "sum"
                elif "average" in logic or "avg" in logic or "mean" in logic:
                    agg_map[source_col] = "mean"
                elif "count" in logic:
                    agg_map[source_col] = "count"
                elif "max" in logic:
                    agg_map[source_col] = "max"
                elif "min" in logic:
                    agg_map[source_col] = "min"

        if rename_map:
            valid_renames = {s: t for s, t in rename_map.items() if s in df.columns}
            if valid_renames:
                df = df.rename(columns=valid_renames)
                group_by_cols = [rename_map.get(col, col) for col in group_by_cols]
                agg_map = {rename_map.get(col, col): func for col, func in agg_map.items()}

        valid_group_by = [col for col in group_by_cols if col in df.columns]
        valid_agg_map = {col: func for col, func in agg_map.items() if col in df.columns}
        if valid_group_by and valid_agg_map:
            agg_only = {col: func for col, func in valid_agg_map.items() if col not in valid_group_by}
            if agg_only:
                df = df.groupby(valid_group_by, dropna=False, as_index=False).agg(agg_only)

        target_columns = set(table_rules["target_column"].unique())
        columns_to_keep = [
            c for c in df.columns
            if c in target_columns or c.startswith("_") or c.startswith("pk_")
        ]
        if columns_to_keep:
            df = df[columns_to_keep]

        pk_col = "pk_gold_id"
        if pk_col not in df.columns:
            df.insert(0, pk_col, range(1, len(df) + 1))

        output_filename = f"{target_table_name}.parquet"
        output_path = str(GOLD_DIR / output_filename)
        df.to_parquet(output_path, index=False)
        output_paths.append(output_path)

        print(f"[GOLD] Created {target_table_name}: {df.shape[0]} rows x {df.shape[1]} columns")
        audit.log(
            "gold_agent", "table_created",
            target_table=target_table_name,
            output_file=output_path,
            shape=list(df.shape),
        )

    audit.log(
        "gold_agent", "completed",
        output_files=output_paths,
        table_count=len(output_paths),
    )
    return output_paths


# ---------------------------------------------------------------------------
# Public entry point — I/O contract UNCHANGED
# ---------------------------------------------------------------------------

def execute_gold(
    input_files: list[str],
    sttm_path: str,
    run_id: str,
    task_description: str,
) -> list[str]:
    """Gold agent entry point — deterministic, no LLM required.

    Business intent is already baked into the Gold STTM by sttm_generator.py,
    so this executor doesn't need to interpret it — it just applies the
    approved joins/renames/aggregations mechanically.

    Args:
        input_files: Silver Parquet file paths to materialise.
        sttm_path: Path to the approved Gold STTM CSV.
        run_id: Unique identifier for this pipeline run.
        task_description: High-level goal (kept for logging parity; unused for logic).

    Returns:
        list[str]: Gold Parquet output file paths.
    """
    trace = AgentTrace("gold_agent", run_id)
    trace.set_input(input_files=input_files, sttm_path=sttm_path)
    trace.set_plan("Auto-join Silver tables on shared id columns per the approved Gold STTM; inject surrogate key.")

    print(f"[GOLD] Materialising from {len(input_files)} Silver table(s)")
    try:
        output_paths = _apply_gold_rules(input_files, sttm_path, run_id)
    except Exception as e:
        trace.fail(str(e))
        raise

    trace.log_step(f"Wrote {len(output_paths)} Gold Parquet table(s)")
    trace.set_output(output_paths=output_paths).complete()
    return output_paths
