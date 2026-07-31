"""Tests for bronze_agent.py — fully deterministic, no LLM/mocking required."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from unittest.mock import patch


def _make_sttm_csv(tmp_path, extra_rows=None):
    rows = [
        {"source_table": "test.csv", "source_column": "id", "target_column": "id",
         "transformation_type": "Direct", "transformation_logic": "Passthrough"},
        {"source_table": "test.csv", "source_column": "value", "target_column": "value",
         "transformation_type": "Direct", "transformation_logic": "Passthrough"},
        {"source_table": "test.csv", "source_column": "", "target_column": "_load_timestamp",
         "transformation_type": "Indirect", "transformation_logic": "Current UTC timestamp"},
        {"source_table": "test.csv", "source_column": "", "target_column": "_source_file",
         "transformation_type": "Indirect", "transformation_logic": "Source file path"},
    ]
    if extra_rows:
        rows.extend(extra_rows)
    sttm_path = tmp_path / "sttm_bronze.csv"
    pd.DataFrame(rows).to_csv(sttm_path, index=False)
    return sttm_path


class TestApplyBronzeRules:

    def test_metadata_columns_added(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agents.bronze_agent.BRONZE_DIR", tmp_path)
        csv_path = tmp_path / "test.csv"
        pd.DataFrame({"id": [1, 2], "value": [10, 20]}).to_csv(csv_path, index=False)
        sttm_path = _make_sttm_csv(tmp_path)

        from agents.bronze_agent import _apply_bronze_rules
        with patch("agents.bronze_agent.AuditLogger"):
            results = _apply_bronze_rules([str(csv_path)], str(sttm_path), "run-1")

        assert len(results) == 1
        df = pd.read_parquet(results[0])
        meta_cols = set(df.columns)
        assert meta_cols & {"_load_timestamp", "load_timestamp"}
        assert meta_cols & {"_source_file", "source_file"}
        assert df.shape[0] == 2

    def test_row_count_preserved(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agents.bronze_agent.BRONZE_DIR", tmp_path)
        csv_path = tmp_path / "test.csv"
        pd.DataFrame({"id": range(50), "value": range(50)}).to_csv(csv_path, index=False)
        sttm_path = _make_sttm_csv(tmp_path)

        from agents.bronze_agent import _apply_bronze_rules
        with patch("agents.bronze_agent.AuditLogger"):
            results = _apply_bronze_rules([str(csv_path)], str(sttm_path), "run-2")

        assert pd.read_parquet(results[0]).shape[0] == 50

    def test_column_rename_applied(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agents.bronze_agent.BRONZE_DIR", tmp_path)
        csv_path = tmp_path / "test.csv"
        pd.DataFrame({"old_name": [1, 2]}).to_csv(csv_path, index=False)

        sttm_path = tmp_path / "sttm_bronze.csv"
        pd.DataFrame([
            {"source_table": "test.csv", "source_column": "old_name", "target_column": "new_name",
             "transformation_type": "Indirect", "transformation_logic": "Rename"},
            {"source_table": "test.csv", "source_column": "", "target_column": "_load_timestamp",
             "transformation_type": "Indirect", "transformation_logic": "Current UTC timestamp"},
            {"source_table": "test.csv", "source_column": "", "target_column": "_source_file",
             "transformation_type": "Indirect", "transformation_logic": "Source file path"},
        ]).to_csv(sttm_path, index=False)

        from agents.bronze_agent import _apply_bronze_rules
        with patch("agents.bronze_agent.AuditLogger"):
            results = _apply_bronze_rules([str(csv_path)], str(sttm_path), "run-3")

        df = pd.read_parquet(results[0])
        assert "new_name" in df.columns
        assert "old_name" not in df.columns

    def test_multiple_files_produce_multiple_outputs(self, tmp_path, monkeypatch):
        monkeypatch.setattr("agents.bronze_agent.BRONZE_DIR", tmp_path)
        csv_a = tmp_path / "a.csv"
        csv_b = tmp_path / "b.csv"
        pd.DataFrame({"id": [1]}).to_csv(csv_a, index=False)
        pd.DataFrame({"id": [2]}).to_csv(csv_b, index=False)

        sttm_path = tmp_path / "sttm_bronze.csv"
        pd.DataFrame([
            {"source_table": "", "source_column": "", "target_column": "_load_timestamp",
             "transformation_type": "Indirect", "transformation_logic": "Current UTC timestamp"},
            {"source_table": "", "source_column": "", "target_column": "_source_file",
             "transformation_type": "Indirect", "transformation_logic": "Source file path"},
        ]).to_csv(sttm_path, index=False)

        from agents.bronze_agent import _apply_bronze_rules
        with patch("agents.bronze_agent.AuditLogger"):
            results = _apply_bronze_rules([str(csv_a), str(csv_b)], str(sttm_path), "run-4")

        assert len(results) == 2


class TestExecuteBronze:

    def test_execute_bronze_returns_paths(self, tmp_path, monkeypatch):
        """execute_bronze is deterministic — no LLM/mocking needed."""
        monkeypatch.setattr("agents.bronze_agent.BRONZE_DIR", tmp_path)
        csv_path = tmp_path / "test.csv"
        pd.DataFrame({"id": [1], "value": [9]}).to_csv(csv_path, index=False)
        sttm_path = _make_sttm_csv(tmp_path)

        from agents.bronze_agent import execute_bronze
        with patch("agents.bronze_agent.AuditLogger"):
            results = execute_bronze([str(csv_path)], str(sttm_path), "run-10", task_description="test")

        assert isinstance(results, list)
        assert len(results) == 1
        assert pd.read_parquet(results[0]).shape[0] == 1
