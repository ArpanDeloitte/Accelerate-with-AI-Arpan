"""End-to-end pipeline test — exercises all four orchestrator phases against
small in-memory CSVs, exactly as the Streamlit UI would drive them, with no
LLM/API key involved at any step."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
from unittest.mock import patch

from agents.orchestrator import (
    run_until_bronze_sttm,
    run_bronze_to_silver_sttm,
    run_silver_to_gold_sttm,
    run_gold_and_report,
)


def _write_sample_csvs(tmp_path):
    products = tmp_path / "products.csv"
    sales = tmp_path / "sales.csv"
    pd.DataFrame({
        "product_id": ["P1", "P2", "P3"],
        "product_name": ["Widget", "Gadget", "Gizmo"],
        "category": ["Tools", "Tools", "Toys"],
    }).to_csv(products, index=False)
    pd.DataFrame({
        "transaction_id": ["T1", "T2", "T3", "T4"],
        "product_id": ["P1", "P1", "P2", "P3"],
        "total_amount": [10.0, 20.0, 30.0, 5.0],
    }).to_csv(sales, index=False)
    return [str(products), str(sales)]


def test_full_pipeline_runs_without_llm(tmp_path, monkeypatch):
    for mod, attr in [
        ("core.audit", "AUDIT_DIR"),
        ("agents.profiler", "PROFILES_DIR"),
        ("agents.sttm_generator", "STTM_DIR"),
        ("agents.bronze_agent", "BRONZE_DIR"),
        ("agents.silver_agent", "SILVER_DIR"),
        ("agents.gold_agent", "GOLD_DIR"),
        ("agents.reporter", "REPORTS_DIR"),
    ]:
        monkeypatch.setattr(f"{mod}.{attr}", tmp_path)
    monkeypatch.setattr("agents.orchestrator.store_document", lambda **kwargs: None)
    monkeypatch.setattr("agents.reporter.store_document", lambda **kwargs: None)

    uploaded_files = _write_sample_csvs(tmp_path)
    business_intent = "What is total sales revenue by product category?"

    state = run_until_bronze_sttm(uploaded_files, business_intent)
    assert state["status"] == "awaiting_bronze_sttm_approval", state.get("error")
    assert Path(state["sttm_bronze_path"]).exists()

    state = run_bronze_to_silver_sttm(state)
    assert state["status"] == "awaiting_silver_sttm_approval", state.get("error")
    assert len(state["bronze_output_paths"]) == 2

    state = run_silver_to_gold_sttm(state)
    assert state["status"] == "awaiting_gold_sttm_approval", state.get("error")
    assert len(state["silver_output_paths"]) == 2

    state = run_gold_and_report(state)
    assert state["status"] == "completed", state.get("error")
    assert len(state["gold_output_paths"]) == 1
    assert Path(state["report_path"]).exists()

    report_html = Path(state["report_path"]).read_text(encoding="utf-8")
    assert "Executive Report" in report_html
