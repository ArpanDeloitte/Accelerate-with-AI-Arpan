"""Tests for reporter.py — fully deterministic, no LLM required."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from unittest.mock import patch

from agents.reporter import _pick_measure, _pick_dimensions, generate_report


def test_pick_measure_prefers_amount_over_generic_numeric():
    df = pd.DataFrame({"pk_gold_id": [1, 2], "quantity": [1, 2], "total_amount": [10.0, 20.0]})
    assert _pick_measure(df) == "total_amount"


def test_pick_measure_excludes_id_columns():
    df = pd.DataFrame({"pk_gold_id": [1, 2], "product_id": [1, 2], "score": [5.0, 6.0]})
    assert _pick_measure(df) == "score"


def test_pick_dimensions_prefers_named_categories():
    df = pd.DataFrame({
        "pk_gold_id": [1, 2, 3],
        "category": ["A", "B", "A"],
        "random_text": ["x", "y", "z"],
        "total_amount": [1.0, 2.0, 3.0],
    })
    dims = _pick_dimensions(df, measure="total_amount")
    assert dims[0] == "category"


def test_generate_report_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.reporter.REPORTS_DIR", tmp_path)
    gold_path = tmp_path / "gold_output.parquet"
    pd.DataFrame({
        "pk_gold_id": [1, 2, 3, 4],
        "category": ["Electronics", "Electronics", "Apparel", "Apparel"],
        "total_amount": [100.0, 200.0, 50.0, 25.0],
    }).to_parquet(gold_path, index=False)

    with patch("agents.reporter.AuditLogger"), patch("agents.reporter.store_document"):
        report_path = generate_report(
            [str(gold_path)], "Which category has the highest sales?", "report-test", "test"
        )

    assert Path(report_path).exists()
    html = Path(report_path).read_text(encoding="utf-8")
    assert "Executive Report" in html
    assert "Electronics" in html  # top category by total_amount must appear in the answer

    assert any(tmp_path.glob("report_*.json"))


def test_generate_report_handles_no_gold_files(tmp_path, monkeypatch):
    monkeypatch.setattr("agents.reporter.REPORTS_DIR", tmp_path)
    with patch("agents.reporter.AuditLogger"):
        result = generate_report([], "any question", "report-empty", "test")
    assert result == ""
