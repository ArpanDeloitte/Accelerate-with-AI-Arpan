"""Tests for silver_agent.py — fully deterministic, no LLM/mocking required."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import pytest
from unittest.mock import patch


def _make_silver_sttm(tmp_path, rows):
    sttm_path = tmp_path / "sttm_silver.csv"
    pd.DataFrame(rows).to_csv(sttm_path, index=False)
    return sttm_path


def _passthrough_sttm(tmp_path, source_table="test_bronze.parquet", columns=("id", "value")):
    rows = [
        {"source_table": source_table, "source_column": col, "target_column": col,
         "transformation_type": "Direct", "transformation_logic": "Passthrough"}
        for col in columns
    ]
    return _make_silver_sttm(tmp_path, rows)


class TestApplySilverRules:

    def test_output_written_for_each_input(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agents.silver_agent.SILVER_DIR", tmp_path)
        bronze_path = tmp_path / "test_bronze.parquet"
        pd.DataFrame({"id": [1, 2], "value": [10.0, 20.0]}).to_parquet(bronze_path, index=False)
        sttm_path = _passthrough_sttm(tmp_path)

        from agents.silver_agent import _apply_silver_rules
        with patch("agents.silver_agent.AuditLogger"):
            results = _apply_silver_rules([str(bronze_path)], str(sttm_path), "run-20")

        assert len(results) == 1
        assert pd.read_parquet(results[0]).shape[0] == 2

    def test_null_handling_fill_mean(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agents.silver_agent.SILVER_DIR", tmp_path)
        bronze_path = tmp_path / "test_bronze.parquet"
        pd.DataFrame({"id": [1, 2, 3], "value": [10.0, None, 30.0]}).to_parquet(bronze_path, index=False)

        sttm_path = _make_silver_sttm(tmp_path, [
            {"source_table": "test_bronze.parquet", "source_column": "id", "target_column": "id",
             "transformation_type": "Direct", "transformation_logic": "Passthrough"},
            {"source_table": "test_bronze.parquet", "source_column": "value", "target_column": "value",
             "transformation_type": "Direct", "transformation_logic": "fill null with mean"},
        ])

        from agents.silver_agent import _apply_silver_rules
        with patch("agents.silver_agent.AuditLogger"):
            results = _apply_silver_rules([str(bronze_path)], str(sttm_path), "run-21")

        df = pd.read_parquet(results[0])
        assert df["value"].isnull().sum() == 0
        assert df["value"].iloc[1] == pytest.approx(20.0)

    def test_dedup_removes_duplicate_rows(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agents.silver_agent.SILVER_DIR", tmp_path)
        bronze_path = tmp_path / "test_bronze.parquet"
        pd.DataFrame({"id": [1, 1, 2], "value": [5.0, 5.0, 6.0]}).to_parquet(bronze_path, index=False)

        sttm_path = _make_silver_sttm(tmp_path, [
            {"source_table": "test_bronze.parquet", "source_column": "id", "target_column": "id",
             "transformation_type": "Direct", "transformation_logic": "deduplicate rows"},
        ])

        from agents.silver_agent import _apply_silver_rules
        with patch("agents.silver_agent.AuditLogger"):
            results = _apply_silver_rules([str(bronze_path)], str(sttm_path), "run-22")

        assert pd.read_parquet(results[0]).shape[0] == 2

    def test_surrogate_key_injected_as_first_column(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agents.silver_agent.SILVER_DIR", tmp_path)
        bronze_path = tmp_path / "widgets_bronze.parquet"
        pd.DataFrame({"widget_id": ["W1", "W2"]}).to_parquet(bronze_path, index=False)
        sttm_path = _passthrough_sttm(tmp_path, source_table="widgets_bronze.parquet", columns=("widget_id",))

        from agents.silver_agent import _apply_silver_rules
        with patch("agents.silver_agent.AuditLogger"):
            results = _apply_silver_rules([str(bronze_path)], str(sttm_path), "run-23")

        df = pd.read_parquet(results[0])
        assert df.columns[0] == "pk_widgets_bronze_silver_id"


class TestExecuteSilver:

    def test_execute_silver_returns_paths(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agents.silver_agent.SILVER_DIR", tmp_path)
        bronze_path = tmp_path / "test_bronze.parquet"
        pd.DataFrame({"id": [1], "value": [9.0]}).to_parquet(bronze_path, index=False)
        sttm_path = _passthrough_sttm(tmp_path)

        from agents.silver_agent import execute_silver
        with patch("agents.silver_agent.AuditLogger"):
            results = execute_silver([str(bronze_path)], str(sttm_path), "run-30", task_description="test")

        assert isinstance(results, list)
        assert len(results) == 1
