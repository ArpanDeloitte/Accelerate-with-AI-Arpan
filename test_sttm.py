"""Tests for sttm_generator.py — fully deterministic, no LLM required."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from unittest.mock import patch

from agents.profiler import profile_multiple_datasets
from agents.bronze_agent import execute_bronze
from agents.sttm_generator import generate_bronze_sttm, generate_silver_sttm, generate_gold_sttm


def _profile(tmp_path, monkeypatch, df, name="widgets"):
    monkeypatch.setattr("agents.profiler.PROFILES_DIR", tmp_path)
    csv_path = tmp_path / f"{name}.csv"
    df.to_csv(csv_path, index=False)
    with patch("agents.profiler.AuditLogger"):
        return profile_multiple_datasets([str(csv_path)], run_id="sttm-test", task_description="test"), str(csv_path)


def test_bronze_sttm_covers_every_column_plus_metadata(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.sttm_generator.STTM_DIR", tmp_path)
    profile_path, _ = _profile(tmp_path, monkeypatch, pd.DataFrame({"widget_id": ["W1"], "price": [1.0]}))

    with patch("agents.sttm_generator.AuditLogger"):
        sttm_path = generate_bronze_sttm(profile_path, run_id="sttm-test", task_description="test")

    rows = pd.read_csv(sttm_path)
    assert {"widget_id", "price"}.issubset(set(rows["source_column"]))
    assert "_load_timestamp" in set(rows["target_column"])
    assert "_source_file" in set(rows["target_column"])


def test_silver_sttm_adds_surrogate_key_and_dedup_rule(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.sttm_generator.STTM_DIR", tmp_path)
    monkeypatch.setattr("agents.bronze_agent.BRONZE_DIR", tmp_path)

    profile_path, csv_path = _profile(
        tmp_path, monkeypatch, pd.DataFrame({"widget_id": ["W1", "W2"], "price": [1.0, 2.0]})
    )
    with patch("agents.sttm_generator.AuditLogger"):
        bronze_sttm_path = generate_bronze_sttm(profile_path, run_id="sttm-test", task_description="test")
    with patch("agents.bronze_agent.AuditLogger"):
        bronze_paths = execute_bronze([csv_path], bronze_sttm_path, "sttm-test", "test")

    with patch("agents.sttm_generator.AuditLogger"):
        silver_sttm_path = generate_silver_sttm(bronze_paths, bronze_sttm_path, "sttm-test", "test")

    rows = pd.read_csv(silver_sttm_path)
    assert any(rows["target_column"].str.startswith("pk_"))
    assert any(rows["transformation_logic"].str.contains("Deduplicate", na=False))
    # source_table must match the bronze parquet's filename stem so silver_agent's
    # per-file STTM filter actually picks these rows up during execution.
    expected_source = Path(bronze_paths[0]).stem
    assert expected_source in set(rows["source_table"])


def test_gold_sttm_passes_through_every_silver_column(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.sttm_generator.STTM_DIR", tmp_path)
    silver_path = tmp_path / "widgets_silver.parquet"
    pd.DataFrame({"pk_widgets_silver_id": [1, 2], "widget_id": ["W1", "W2"], "price": [1.0, 2.0]}).to_parquet(
        silver_path, index=False
    )

    with patch("agents.sttm_generator.AuditLogger"):
        gold_sttm_path = generate_gold_sttm(
            [str(silver_path)], "unused_silver_sttm.csv", "What is total price?", "sttm-test", "test"
        )

    rows = pd.read_csv(gold_sttm_path)
    assert "pk_gold_id" in set(rows["target_column"])
    assert {"widget_id", "price"}.issubset(set(rows["target_column"]))
    assert set(rows["target_table"].dropna().unique()) == {"gold_output"}
