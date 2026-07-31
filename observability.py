"""Agent observability logger — deterministic version (no LLM message parsing).

Captures inputs, an explicit plan, step-by-step actions, outputs, and timing
for every agent invocation. Writes a structured JSON trace to data/traces/
alongside the existing audit logs. Same call contract every agent module uses.

Usage in any agent::

    trace = AgentTrace("bronze_agent", run_id)
    trace.set_input(input_files=input_files, sttm_path=sttm_path)
    trace.set_plan("Apply approved STTM rules to each input file.")
    ...
    trace.log_step("Wrote 3 Bronze Parquet files")
    trace.set_output(output_paths=output_paths).complete()
"""

import json
import time
from pathlib import Path
from datetime import datetime, timezone
from typing import Any

TRACES_DIR = Path("data/traces")
TRACES_DIR.mkdir(parents=True, exist_ok=True)


class AgentTrace:
    """Collects observability data for a single (deterministic) agent invocation.

    Captures:
    - agent name, run_id, start timestamp
    - inputs passed to the agent
    - an explicit stated plan
    - step-by-step log lines describing what the agent actually did
    - final outputs produced
    - wall-clock duration and terminal status (success / failed)
    """

    def __init__(self, agent_name: str, run_id: str):
        self.agent_name = agent_name
        self.run_id = run_id
        self.start_time = time.time()
        self.trace: dict[str, Any] = {
            "agent": agent_name,
            "run_id": run_id,
            "started_at": datetime.now(timezone.utc).isoformat(),
            "input": {},
            "plan": "",
            "steps": [],
            "output": {},
            "duration_seconds": 0.0,
            "status": "started",
        }

    def set_input(self, **kwargs) -> "AgentTrace":
        """Record the inputs this agent received from its caller."""
        self.trace["input"] = dict(kwargs)
        return self

    def set_plan(self, plan: str) -> "AgentTrace":
        """Record the agent's stated plan before it acts."""
        self.trace["plan"] = plan
        return self

    def log_step(self, description: str) -> "AgentTrace":
        """Record one step of what the agent actually did."""
        self.trace["steps"].append(description)
        return self

    def set_output(self, **kwargs) -> "AgentTrace":
        """Record the final outputs this agent produced."""
        self.trace["output"] = dict(kwargs)
        return self

    def complete(self, status: str = "success") -> dict:
        """Finalise the trace, append to disk, print a one-line summary."""
        self.trace["duration_seconds"] = round(time.time() - self.start_time, 3)
        self.trace["status"] = status
        self._save()
        self._print_summary()
        return self.trace

    def fail(self, error: str) -> dict:
        """Record failure reason, finalise, and save."""
        self.trace["error"] = error
        return self.complete(status="failed")

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _save(self):
        """Append this trace as a JSON entry to the per-agent trace file for this run."""
        path = TRACES_DIR / f"trace_{self.agent_name}_{self.run_id[:8]}.json"
        existing: list = []
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                existing = data if isinstance(data, list) else [data]
            except Exception:
                existing = []
        existing.append(self.trace)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, default=str)

    def _print_summary(self):
        d = self.trace["duration_seconds"]
        print(
            f"[OBSERVE][{self.agent_name}] status={self.trace['status']} "
            f"duration={d}s steps={len(self.trace['steps'])}"
        )
        if self.trace.get("plan"):
            print(f"[OBSERVE][{self.agent_name}] plan_preview={self.trace['plan'][:150]}")
