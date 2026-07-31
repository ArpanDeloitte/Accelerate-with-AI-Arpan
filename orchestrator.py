"""Workflow orchestrator for the Intent-Driven Medallion pipeline — deterministic version.

No Supervisor LLM is used for tool selection: every phase always executes the
same fixed sequence of specialist agents, so orchestration here is plain
Python control flow. Each specialist agent (profiler, sttm_generator,
bronze/silver/gold, reporter) is itself fully deterministic — see their
module docstrings for how they replace the LLM's reasoning with heuristics.

Four HITL-gated phases, matching the original design:

Phase 1 — Profile & Bronze STTM
    profile the raw data, then generate Bronze ingestion rules for review.

Phase 2 — Bronze Execution & Silver STTM
    ingest approved Bronze rules, then generate Silver cleansing rules for review.

Phase 3 — Silver Execution & Gold STTM
    cleanse Bronze outputs, then generate Gold materialisation rules for review.

Phase 4 — Gold Execution & Report
    materialise Gold tables, then produce the executive report.

UI contract (UNCHANGED — app/streamlit_app.py reads these):
    run_until_bronze_sttm(uploaded_files, business_intent) -> PipelineState
    run_bronze_to_silver_sttm(state) -> PipelineState
    run_silver_to_gold_sttm(state) -> PipelineState
    run_gold_and_report(state) -> PipelineState

PipelineState keys read by UI (UNCHANGED):
    run_id, status, error, sttm_bronze_path, sttm_silver_path, sttm_gold_path, report_path
"""

import uuid
import traceback
from typing import TypedDict
from core.audit import AuditLogger
from core.memory import store_document
from agents.profiler import profile_multiple_datasets
from agents.sttm_generator import generate_bronze_sttm, generate_silver_sttm, generate_gold_sttm
from agents.bronze_agent import execute_bronze
from agents.silver_agent import execute_silver
from agents.gold_agent import execute_gold
from agents.reporter import generate_report


# ---------------------------------------------------------------------------
# Pipeline state — keys UNCHANGED, UI reads them directly
# ---------------------------------------------------------------------------

class PipelineState(TypedDict):
    """State flowing through the pipeline. Keys read by Streamlit UI must not change."""
    run_id: str
    status: str
    uploaded_files: list[str]
    business_intent: str
    profile_path: str
    sttm_bronze_path: str
    sttm_silver_path: str
    sttm_gold_path: str
    bronze_sttm_approved: bool
    silver_sttm_approved: bool
    gold_sttm_approved: bool
    bronze_output_paths: list[str]
    silver_output_paths: list[str]
    gold_output_paths: list[str]
    report_path: str
    error: str


# ---------------------------------------------------------------------------
# Pipeline entry points — signatures UNCHANGED, UI calls these directly
# ---------------------------------------------------------------------------

def run_until_bronze_sttm(uploaded_files: list[str], business_intent: str) -> PipelineState:
    """Phase 1: profile the raw data and generate Bronze STTM, then pause for HITL approval."""
    run_id = str(uuid.uuid4())
    audit = AuditLogger(run_id)
    audit.log(
        "orchestrator", "pipeline_started",
        intent=business_intent, status="started", phase="upload",
    )
    store_document(
        doc_id=f"intent_{run_id}",
        text=business_intent,
        metadata={"type": "business_intent", "run_id": run_id},
    )

    state: PipelineState = {
        "run_id": run_id,
        "status": "profiling",
        "uploaded_files": uploaded_files,
        "business_intent": business_intent,
        "profile_path": "",
        "sttm_bronze_path": "",
        "sttm_silver_path": "",
        "sttm_gold_path": "",
        "bronze_sttm_approved": False,
        "silver_sttm_approved": False,
        "gold_sttm_approved": False,
        "bronze_output_paths": [],
        "silver_output_paths": [],
        "gold_output_paths": [],
        "report_path": "",
        "error": "",
    }

    try:
        audit.log("orchestrator", "profile_requested", status="in_progress", phase="profiling")
        profile_path = profile_multiple_datasets(
            file_paths=uploaded_files,
            run_id=run_id,
            task_description=f"Profile the uploaded raw CSV files for business intent: {business_intent}",
        )
        audit.log("orchestrator", "profile_completed", status="success", phase="profiling", output_path=profile_path)

        audit.log("orchestrator", "bronze_sttm_requested", status="in_progress", phase="bronze_sttm")
        sttm_bronze_path = generate_bronze_sttm(
            profile_path=profile_path,
            run_id=run_id,
            task_description="Generate a complete Bronze STTM covering every column.",
        )
        audit.log("orchestrator", "bronze_sttm_completed", status="success", phase="bronze_sttm", output_path=sttm_bronze_path)

        state.update({
            "profile_path": profile_path,
            "sttm_bronze_path": sttm_bronze_path,
            "status": "awaiting_bronze_sttm_approval",
        })
    except Exception as e:
        state.update({
            "error": f"Phase 1 failed: {e}\n{traceback.format_exc()}",
            "status": "failed",
        })
        audit.log("orchestrator", "phase1_failed", status="failed", phase="phase1", detail=str(e))

    return state


def run_bronze_to_silver_sttm(state: PipelineState) -> PipelineState:
    """Phase 2: execute Bronze ingestion and generate Silver STTM, then pause for HITL approval."""
    audit = AuditLogger(state["run_id"])
    state["bronze_sttm_approved"] = True
    state["error"] = ""

    try:
        audit.log("orchestrator", "bronze_execution_requested", status="in_progress", phase="bronze_execution")
        bronze_output_paths = execute_bronze(
            input_files=state["uploaded_files"],
            sttm_path=state["sttm_bronze_path"],
            run_id=state["run_id"],
            task_description="Ingest raw CSV files into Bronze Parquet using the approved STTM rules.",
        )
        audit.log(
            "orchestrator", "bronze_execution_completed", status="success",
            phase="bronze_execution", output_files=bronze_output_paths,
        )

        audit.log("orchestrator", "silver_sttm_requested", status="in_progress", phase="silver_sttm")
        sttm_silver_path = generate_silver_sttm(
            bronze_output_paths=bronze_output_paths,
            bronze_sttm_path=state["sttm_bronze_path"],
            run_id=state["run_id"],
            task_description="Generate a complete Silver STTM cleansing every Bronze column.",
        )
        audit.log("orchestrator", "silver_sttm_completed", status="success", phase="silver_sttm", output_path=sttm_silver_path)

        state.update({
            "bronze_output_paths": bronze_output_paths,
            "sttm_silver_path": sttm_silver_path,
            "status": "awaiting_silver_sttm_approval",
        })
    except Exception as e:
        state.update({
            "error": f"Phase 2 failed: {e}\n{traceback.format_exc()}",
            "status": "failed",
        })
        audit.log("orchestrator", "phase2_failed", status="failed", phase="phase2", detail=str(e))

    return state


def run_silver_to_gold_sttm(state: PipelineState) -> PipelineState:
    """Phase 3: execute Silver cleansing and generate Gold STTM, then pause for HITL approval."""
    audit = AuditLogger(state["run_id"])
    state["silver_sttm_approved"] = True
    state["error"] = ""

    try:
        audit.log("orchestrator", "silver_execution_requested", status="in_progress", phase="silver_execution")
        silver_output_paths = execute_silver(
            input_files=state["bronze_output_paths"],
            sttm_path=state["sttm_silver_path"],
            run_id=state["run_id"],
            task_description="Cleanse Bronze Parquet files into Silver Parquet using the approved STTM rules.",
        )
        audit.log(
            "orchestrator", "silver_execution_completed", status="success",
            phase="silver_execution", output_files=silver_output_paths,
        )

        audit.log("orchestrator", "gold_sttm_requested", status="in_progress", phase="gold_sttm")
        sttm_gold_path = generate_gold_sttm(
            silver_output_paths=silver_output_paths,
            silver_sttm_path=state["sttm_silver_path"],
            business_intent=state["business_intent"],
            run_id=state["run_id"],
            task_description="Generate a complete Gold STTM materialising analytics-ready tables.",
        )
        audit.log("orchestrator", "gold_sttm_completed", status="success", phase="gold_sttm", output_path=sttm_gold_path)

        state.update({
            "silver_output_paths": silver_output_paths,
            "sttm_gold_path": sttm_gold_path,
            "status": "awaiting_gold_sttm_approval",
        })
    except Exception as e:
        state.update({
            "error": f"Phase 3 failed: {e}\n{traceback.format_exc()}",
            "status": "failed",
        })
        audit.log("orchestrator", "phase3_failed", status="failed", phase="phase3", detail=str(e))

    return state


def run_gold_and_report(state: PipelineState) -> PipelineState:
    """Phase 4: execute Gold materialisation and generate the executive report."""
    audit = AuditLogger(state["run_id"])
    state["gold_sttm_approved"] = True
    state["error"] = ""

    try:
        audit.log("orchestrator", "gold_execution_requested", status="in_progress", phase="gold_execution")
        gold_output_paths = execute_gold(
            input_files=state["silver_output_paths"],
            sttm_path=state["sttm_gold_path"],
            run_id=state["run_id"],
            task_description="Materialise Gold Parquet tables using the approved STTM rules.",
        )
        audit.log(
            "orchestrator", "gold_execution_completed", status="success",
            phase="gold_execution", output_files=gold_output_paths,
        )

        audit.log("orchestrator", "report_requested", status="in_progress", phase="report")
        report_path = generate_report(
            gold_files=gold_output_paths,
            business_intent=state["business_intent"],
            run_id=state["run_id"],
            task_description="Answer the business question from the Gold tables and render an HTML report.",
        )
        audit.log("orchestrator", "report_completed", status="success", phase="report", output_path=report_path)

        state.update({
            "gold_output_paths": gold_output_paths,
            "report_path": report_path,
            "status": "completed",
        })
    except Exception as e:
        state.update({
            "error": f"Phase 4 failed: {e}\n{traceback.format_exc()}",
            "status": "failed",
        })
        audit.log("orchestrator", "phase4_failed", status="failed", phase="phase4", detail=str(e))

    return state
