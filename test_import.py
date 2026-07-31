"""Sanity check: every module must import cleanly with no API key and no network access."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))


def test_imports():
    import core.config  # noqa: F401
    import core.audit  # noqa: F401
    import core.memory  # noqa: F401
    import core.observability  # noqa: F401
    import agents.profiler  # noqa: F401
    import agents.sttm_generator  # noqa: F401
    import agents.bronze_agent  # noqa: F401
    import agents.silver_agent  # noqa: F401
    import agents.gold_agent  # noqa: F401
    import agents.reporter  # noqa: F401
    import agents.orchestrator  # noqa: F401


if __name__ == "__main__":
    test_imports()
    print("SUCCESS! All imports worked with no API key required.")
