"""Tests for gold_agent.py — fully deterministic, no LLM/mocking required."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from unittest.mock import patch


def _make_gold_sttm(tmp_path, rows):
    sttm_path = tmp_path / "sttm_gold.csv"
    pd.DataFrame(rows).to_csv(sttm_path, index=False)
    return sttm_path


def _passthrough_sttm(tmp_path, source_table="test_silver.parquet", columns=("id", "value")):
    rows = [
        {"source_table": source_table, "source_column": col, "target_column": col,
         "target_table": "gold_output", "transformation_type": "Direct", "transformation_logic": "Passthrough"}
        for col in columns
    ]
    return _make_gold_sttm(tmp_path, rows)


class TestApplyGoldRules:

    def test_output_written_for_target_table(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agents.gold_agent.GOLD_DIR", tmp_path)
        silver_path = tmp_path / "test_silver.parquet"
        pd.DataFrame({"id": [1, 2], "value": [10.0, 20.0]}).to_parquet(silver_path, index=False)
        sttm_path = _passthrough_sttm(tmp_path)

        from agents.gold_agent import _apply_gold_rules
        with patch("agents.gold_agent.AuditLogger"):
            results = _apply_gold_rules([str(silver_path)], str(sttm_path), "run-40")

        assert len(results) == 1
        assert pd.read_parquet(results[0]).shape[0] == 2

    def test_multiple_target_tables(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agents.gold_agent.GOLD_DIR", tmp_path)
        silver_path = tmp_path / "test_silver.parquet"
        pd.DataFrame({"id": [1, 2], "revenue": [100.0, 200.0], "cost": [50.0, 80.0]}).to_parquet(silver_path, index=False)

        sttm_path = _make_gold_sttm(tmp_path, [
            {"source_table": "test_silver.parquet", "source_column": "id", "target_column": "id",
             "target_table": "table_a", "transformation_type": "Direct", "transformation_logic": "Passthrough"},
            {"source_table": "test_silver.parquet", "source_column": "revenue", "target_column": "revenue",
             "target_table": "table_a", "transformation_type": "Direct", "transformation_logic": "Passthrough"},
            {"source_table": "test_silver.parquet", "source_column": "id", "target_column": "id",
             "target_table": "table_b", "transformation_type": "Direct", "transformation_logic": "Passthrough"},
            {"source_table": "test_silver.parquet", "source_column": "cost", "target_column": "cost",
             "target_table": "table_b", "transformation_type": "Direct", "transformation_logic": "Passthrough"},
        ])

        from agents.gold_agent import _apply_gold_rules
        with patch("agents.gold_agent.AuditLogger"):
            results = _apply_gold_rules([str(silver_path)], str(sttm_path), "run-42")

        assert len(results) == 2

    def test_column_subset_applied(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agents.gold_agent.GOLD_DIR", tmp_path)
        silver_path = tmp_path / "test_silver.parquet"
        pd.DataFrame({"keep": [1, 2], "drop": [3, 4]}).to_parquet(silver_path, index=False)

        sttm_path = _make_gold_sttm(tmp_path, [
            {"source_table": "test_silver.parquet", "source_column": "keep", "target_column": "keep",
             "target_table": "gold_output", "transformation_type": "Direct", "transformation_logic": "Passthrough"},
        ])

        from agents.gold_agent import _apply_gold_rules
        with patch("agents.gold_agent.AuditLogger"):
            results = _apply_gold_rules([str(silver_path)], str(sttm_path), "run-43")

        df = pd.read_parquet(results[0])
        assert "keep" in df.columns
        assert "drop" not in df.columns

    def test_auto_joins_two_silver_tables_on_shared_id(self, tmp_path, monkeypatch):
        """Two Silver tables sharing a *_id column must be merged into one Gold row set."""
        monkeypatch.setattr("agents.gold_agent.GOLD_DIR", tmp_path)
        products_path = tmp_path / "products_silver.parquet"
        sales_path = tmp_path / "sales_silver.parquet"
        pd.DataFrame({"product_id": ["P1", "P2"], "category": ["A", "B"]}).to_parquet(products_path, index=False)
        pd.DataFrame({"product_id": ["P1", "P1", "P2"], "amount": [10.0, 20.0, 30.0]}).to_parquet(sales_path, index=False)

        sttm_path = _make_gold_sttm(tmp_path, [
            {"source_table": "products", "source_column": "product_id", "target_column": "product_id",
             "target_table": "gold_output", "transformation_type": "Direct", "transformation_logic": "Passthrough"},
            {"source_table": "products", "source_column": "category", "target_column": "category",
             "target_table": "gold_output", "transformation_type": "Direct", "transformation_logic": "Passthrough"},
            {"source_table": "sales", "source_column": "amount", "target_column": "amount",
             "target_table": "gold_output", "transformation_type": "Direct", "transformation_logic": "Passthrough"},
        ])

        from agents.gold_agent import _apply_gold_rules
        with patch("agents.gold_agent.AuditLogger"):
            results = _apply_gold_rules([str(products_path), str(sales_path)], str(sttm_path), "run-44")

        df = pd.read_parquet(results[0])
        assert set(df.columns) >= {"product_id", "category", "amount"}
        assert df.shape[0] == 3  # 3 sales rows, joined to product category


class TestExecuteGold:

    def test_execute_gold_returns_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agents.gold_agent.GOLD_DIR", tmp_path)
        silver_path = tmp_path / "test_silver.parquet"
        pd.DataFrame({"id": [1], "value": [9.0]}).to_parquet(silver_path, index=False)
        sttm_path = _passthrough_sttm(tmp_path)

        from agents.gold_agent import execute_gold
        with patch("agents.gold_agent.AuditLogger"):
            results = execute_gold([str(silver_path)], str(sttm_path), "run-50", task_description="test")

        assert isinstance(results, list)
        assert len(results) == 1
