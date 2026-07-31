"""Local, dependency-free stand-in for semantic memory — no API key, no vector DB.

Stores documents as JSON lines under data/memory/documents.jsonl and does a
keyword-overlap lookup instead of embedding similarity. Not a real vector
store, but keeps the same store_document/query_memory contract other modules
rely on, with zero external services.
"""

import json
from core.config import MEMORY_DIR

_STORE_PATH = MEMORY_DIR / "documents.jsonl"


def _read_all() -> list[dict]:
    if not _STORE_PATH.exists():
        return []
    with open(_STORE_PATH, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def store_document(doc_id: str, text: str, metadata: dict | None = None) -> None:
    """Store (or replace) a document in the local store."""
    try:
        docs = [d for d in _read_all() if d.get("id") != doc_id]
        docs.append({"id": doc_id, "document": text, "metadata": metadata or {}})
        with open(_STORE_PATH, "w", encoding="utf-8") as f:
            for d in docs:
                f.write(json.dumps(d) + "\n")
    except Exception:
        # Keep pipeline running even if the local memory store is unavailable.
        pass


def query_memory(query_text: str, n_results: int = 5) -> list[dict]:
    """Return the documents whose text shares the most words with query_text."""
    try:
        docs = _read_all()
        if not docs:
            return []
        query_words = set(query_text.lower().split())

        def score(doc: dict) -> int:
            return len(query_words & set(doc.get("document", "").lower().split()))

        docs.sort(key=score, reverse=True)
        return docs[:n_results]
    except Exception:
        return []
