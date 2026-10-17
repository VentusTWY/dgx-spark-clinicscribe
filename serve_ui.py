"""Serve the ClinicScribe UI with FastAPI static files.

    python serve_ui.py            # http://localhost:8000  (in-browser mock data)

The real backend mounts the same folder after its API routes, e.g.:

    app.mount("/", StaticFiles(directory="ui", html=True), name="ui")

Once GET /jobs answers, the page switches from mock data to the live API.
"""
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

UI_DIR = Path(__file__).parent / "ui"

app = FastAPI(title="ClinicScribe UI")
app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8000)
