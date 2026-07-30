"""
QueryGenie — Web Edition (backend)
=================================================

A thin FastAPI web layer that serves the single-file frontend (index.html) over
HTTP so the generator can run on a shared web server / Kubernetes instead of only
as a local file opened from disk.

ALL SQL generation happens **client-side** inside index.html (plain JavaScript),
so this server does almost nothing: it serves the page at "/" and exposes
"/api/health" for container liveness/readiness probes. This mirrors the
`web_app:app` + `/api/health` + uvicorn convention used by the sibling
LineageIQ-Web service so the same Dockerfile / OKE pipeline applies unchanged.

Run (hosted / shared server):
    uvicorn web_app:app --host 0.0.0.0 --port 8000

Run (local, opens your browser):
    python web_app.py

NOTE — secrets stay local: the live-DB feature (the db-bridge/ Flask service) and
any Oracle wallet / database credentials are LOCAL-ONLY. They are deliberately
NOT imported here and NOT shipped in the container image (see .dockerignore).
The deployed web app runs the fully-offline generator; live-DB browsing remains a
local-only capability.
"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = Path(__file__).resolve().parent
# index.html may sit right next to this file or under ./static — support both.
STATIC_DIR = (BASE_DIR / "static") if (BASE_DIR / "static" / "index.html").exists() else BASE_DIR

app = FastAPI(title="QueryGenie — Web Edition")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def index():
    """Serve the generator UI shell (loads qg-app.min.js)."""
    return (STATIC_DIR / "index.html").read_text(encoding="utf-8")


@app.get("/qg-app.min.js")
def app_bundle():
    """Serve the app's JavaScript bundle referenced by index.html."""
    from fastapi.responses import Response
    return Response((STATIC_DIR / "qg-app.min.js").read_text(encoding="utf-8"),
                    media_type="application/javascript")


@app.get("/api/health")
def health():
    """Health endpoint used by the Kubernetes readiness/liveness probes."""
    return {"ok": True, "app": "odi-sql-generator"}


# Serve the external app bundle + any extra static assets. index.html loads its
# JavaScript from lib/qg-app.js, so /lib must be served here. Mounts are kept
# last so "/" and "/api/*" always win.
if (BASE_DIR / "static").is_dir():
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


def main():
    """Local convenience launcher: start uvicorn on 127.0.0.1:8000 and open a browser."""
    import threading
    import webbrowser
    import uvicorn

    host, port = "127.0.0.1", 8000
    url = f"http://127.0.0.1:{port}"  # loopback only - never exposed

    print("=" * 60)
    print("  QueryGenie — Web Edition")
    print("=" * 60)
    print(f"  Server starting at : {url}")
    print("  Your browser will open automatically.")
    print("  Keep this window open while you use the app.")
    print("  Close this window to stop the server.")
    print("=" * 60)

    threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
