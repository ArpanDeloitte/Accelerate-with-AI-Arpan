"""Data profiling agent — deterministic version (no LLM, no API key required).

Reads each CSV, computes column-level statistics with pandas, and derives
semantic meanings / candidate join keys / data-quality notes via name- and
dtype-based heuristics instead of an LLM call.

I/O contract (UNCHANGED — UI and orchestrator safe):
    profile_dataset(file_path, run_id, task_description) -> str
    profile_multiple_datasets(file_paths, run_id, task_description) -> str
"""

import json
import os
import re
import pandas as pd
from core.config import PROFILES_DIR
from core.audit import AuditLogger
from core.observability import AgentTrace

_ID_PATTERN = re.compile(r"(^|_)id$")
_DATE_PATTERN = re.compile(r"date|_dt$|timestamp")
_MONEY_PATTERN = re.compile(r"price|amount|revenue|total|cost|sales")
_QTY_PATTERN = re.compile(r"quantity|qty|count")
_NAME_PATTERN = re.compile(r"name")
_LOCATION_PATTERN = re.compile(r"region|city|state|country|zone")
_CATEGORY_PATTERN = re.compile(r"category|segment|type|method|status")


# ---------------------------------------------------------------------------
# Heuristic semantic analysis — replaces the LLM's job
# ---------------------------------------------------------------------------

def _semantic_meaning(column: str, dtype: str) -> str:
    """Guess a column's business meaning from its name and dtype."""
    col_lower = column.lower()
    if _ID_PATTERN.search(col_lower):
        return "Unique identifier column"
    if _DATE_PATTERN.search(col_lower):
        return "Date/time column"
    if _MONEY_PATTERN.search(col_lower):
        return "Monetary value column"
    if _QTY_PATTERN.search(col_lower):
        return "Quantity/count column"
    if _NAME_PATTERN.search(col_lower):
        return "Descriptive name column"
    if _LOCATION_PATTERN.search(col_lower):
        return "Geographic/location column"
    if _CATEGORY_PATTERN.search(col_lower):
        return "Categorical grouping column"
    if "float" in dtype or "int" in dtype:
        return "Numeric measure column"
    return "Descriptive text column"


def _find_join_keys(datasets: dict) -> list[dict]:
    """Flag columns with the same name across datasets as candidate join keys."""
    join_keys = []
    names = list(datasets.keys())
    for i, left in enumerate(names):
        for right in names[i + 1:]:
            left_cols = set(datasets[left]["columns"].keys())
            right_cols = set(datasets[right]["columns"].keys())
            for col in sorted(left_cols & right_cols):
                confidence = "high" if _ID_PATTERN.search(col.lower()) else "medium"
                join_keys.append({
                    "left_dataset": left, "left_column": col,
                    "right_dataset": right, "right_column": col,
                    "confidence": confidence,
                })
    return join_keys


def _quality_notes(dataset_name: str, columns: dict) -> list[str]:
    notes = []
    for col, info in columns.items():
        if info.get("null_pct", 0) > 0:
            notes.append(f"{dataset_name}.{col}: {info['null_pct']}% null values")
        if info.get("min") is not None and info["min"] < 0:
            notes.append(f"{dataset_name}.{col}: contains negative values (min={info['min']})")
    return notes


def _build_analysis(stats: dict) -> dict:
    semantic_meanings = {}
    quality_notes: list[str] = []
    for name, ds in stats["datasets"].items():
        semantic_meanings[name] = {
            col: _semantic_meaning(col, info["dtype"]) for col, info in ds["columns"].items()
        }
        quality_notes.extend(_quality_notes(name, ds["columns"]))
    return {
        "semantic_meanings": semantic_meanings,
        "join_keys": _find_join_keys(stats["datasets"]),
        "quality_notes": quality_notes or ["No data quality issues detected."],
    }


# ---------------------------------------------------------------------------
# Pure statistics — unchanged from the original implementation
# ---------------------------------------------------------------------------

def _compute_stats(file_paths: list[str]) -> dict:
    """Full column-level statistics across all CSV files. No LLM."""
    combined_profile: dict = {"files": [], "datasets": {}}
    for fp in file_paths:
        try:
            df = pd.read_csv(fp)
        except Exception as e:
            print(f"[PROFILER] Could not read {fp}: {e}")
            continue
        dataset_name = os.path.basename(fp).replace(".csv", "")
        combined_profile["files"].append(fp)
        ds_profile: dict = {
            "file": fp,
            "shape": {"rows": df.shape[0], "columns": df.shape[1]},
            "columns": {},
        }
        for col in df.columns:
            col_info: dict = {
                "dtype": str(df[col].dtype),
                "null_count": int(df[col].isnull().sum()),
                "null_pct": round(df[col].isnull().mean() * 100, 2),
                "unique_count": int(df[col].nunique()),
            }
            if df[col].dtype in ["int64", "float64"]:
                col_info["min"] = float(df[col].min()) if not df[col].isnull().all() else None
                col_info["max"] = float(df[col].max()) if not df[col].isnull().all() else None
                col_info["mean"] = float(df[col].mean()) if not df[col].isnull().all() else None
            else:
                col_info["sample_values"] = df[col].dropna().head(5).tolist()
            ds_profile["columns"][col] = col_info
        combined_profile["datasets"][dataset_name] = ds_profile
    return combined_profile


# ---------------------------------------------------------------------------
# Public entry points — I/O contract UNCHANGED
# ---------------------------------------------------------------------------

def profile_dataset(file_path: str, run_id: str, task_description: str) -> str:
    """Profile a single CSV file. Delegates to profile_multiple_datasets."""
    return profile_multiple_datasets([file_path], run_id, task_description)


def profile_multiple_datasets(file_paths: list[str], run_id: str, task_description: str) -> str:
    """Profiler agent entry point — deterministic, no LLM required.

    Args:
        file_paths: CSV file paths to profile.
        run_id: Unique identifier for this pipeline run.
        task_description: High-level goal (kept for logging parity; unused for logic).

    Returns:
        str: Path to the saved combined profile JSON.
    """
    trace = AgentTrace("profiler", run_id)
    trace.set_input(file_paths=file_paths)

    audit = AuditLogger(run_id)
    print(f"[PROFILER] Started — files: {file_paths}")
    audit.log("profiler", "started_multi", input_files=file_paths)

    trace.set_plan(
        "Inspect each CSV, compute column statistics, then derive semantic "
        "meaning, candidate join keys, and quality notes via heuristics."
    )
    stats = _compute_stats(file_paths)
    trace.log_step(f"Computed statistics for {len(stats['datasets'])} dataset(s)")

    analysis = _build_analysis(stats)
    trace.log_step(
        f"Derived {len(analysis['join_keys'])} candidate join key(s) and "
        f"{len(analysis['quality_notes'])} quality note(s)"
    )

    combined_profile = stats
    combined_profile["analysis"] = analysis

    profile_filename = f"profile_combined_{pd.Timestamp.now().strftime('%Y%m%d_%H%M%S')}.json"
    profile_path = str(PROFILES_DIR / profile_filename)
    print(f"[PROFILER] Saving profile → {profile_path}")
    with open(profile_path, "w", encoding="utf-8") as f:
        json.dump(combined_profile, f, indent=2)

    audit.log("profiler", "completed_multi", output_file=profile_path)
    trace.set_output(profile_path=profile_path).complete()
    print(f"[PROFILER] Done — {profile_path}")
    return profile_path
