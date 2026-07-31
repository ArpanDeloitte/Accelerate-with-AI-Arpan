"""Tests for profiler.py — fully deterministic, no LLM required."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json
import pandas as pd
from unittest.mock import patch

from agents.profiler import (
    _compute_stats,
    _build_analysis,
    _semantic_meaning,
    _find_join_keys,
    profile_multiple_datasets,
)


def test_compute_stats_shapes(tmp_path):
    csv_path = tmp_path / "widgets.csv"
    pd.DataFrame({"widget_id": ["W1", "W2"], "price": [10.0, 20.0]}).to_csv(csv_path, index=False)
    stats = _compute_stats([str(csv_path)])
    assert "widgets" in stats["datasets"]
    assert stats["datasets"]["widgets"]["shape"]["rows"] == 2
    assert stats["datasets"]["widgets"]["shape"]["columns"] == 2


def test_semantic_meaning_heuristics():
    assert _semantic_meaning("product_id", "object") == "Unique identifier column"
    assert _semantic_meaning("transaction_date", "object") == "Date/time column"
    assert _semantic_meaning("total_amount", "float64") == "Monetary value column"
    assert _semantic_meaning("quantity", "int64") == "Quantity/count column"
    assert _semantic_meaning("region", "object") == "Geographic/location column"
    assert _semantic_meaning("category", "object") == "Categorical grouping column"


def test_find_join_keys_detects_shared_id_column():
    datasets = {
        "products": {"columns": {"product_id": {}, "category": {}}},
        "sales": {"columns": {"product_id": {}, "amount": {}}},
    }
    join_keys = _find_join_keys(datasets)
    assert any(jk["left_column"] == "product_id" and jk["confidence"] == "high" for jk in join_keys)


def test_build_analysis_flags_nulls(tmp_path):
    csv_path = tmp_path / "widgets.csv"
    pd.DataFrame({"widget_id": ["W1", "W2"], "price": [10.0, None]}).to_csv(csv_path, index=False)
    stats = _compute_stats([str(csv_path)])
    analysis = _build_analysis(stats)
    assert any("price" in note for note in analysis["quality_notes"])


def test_profile_multiple_datasets_writes_valid_json(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.profiler.PROFILES_DIR", tmp_path)
    csv_path = tmp_path / "widgets.csv"
    pd.DataFrame({"widget_id": ["W1"], "price": [1.0]}).to_csv(csv_path, index=False)

    with patch("agents.profiler.AuditLogger"):
        profile_path = profile_multiple_datasets([str(csv_path)], run_id="test-run", task_description="test")

    assert profile_path.endswith(".json")
    with open(profile_path, "r", encoding="utf-8") as f:
        profile = json.load(f)
    assert "analysis" in profile
    assert "widgets" in profile["datasets"]
