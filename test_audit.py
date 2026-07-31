"""Tests for core/audit.py — plain JSONL append/read, no LLM involved."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.audit import AuditLogger


def test_log_then_get_logs_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr("core.audit.AUDIT_DIR", tmp_path)
    audit = AuditLogger("run-audit-1")
    audit.log("profiler", "started", input_files=["a.csv"])
    audit.log("profiler", "completed", output_file="profile.json")

    logs = audit.get_logs()
    assert len(logs) == 2
    assert logs[0]["action"] == "started"
    assert logs[1]["output_file"] == "profile.json"
    assert all(entry["run_id"] == "run-audit-1" for entry in logs)


def test_get_logs_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr("core.audit.AUDIT_DIR", tmp_path)
    audit = AuditLogger("run-audit-2")
    assert audit.get_logs() == []
