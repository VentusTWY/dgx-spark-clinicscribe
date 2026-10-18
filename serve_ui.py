"""Serve the ClinicScribe UI with FastAPI static files.

    python serve_ui.py            # http://localhost:8080  (in-browser mock data)

Set PORT to change the port. (On the event ATOM, Lightning is on 9000 and 8000 belongs to another service.)

The real backend mounts the same folder after its API routes, e.g.:

    app.mount("/", StaticFiles(directory="ui", html=True), name="ui")

Once GET /jobs answers, the page switches from mock data to the live API.
"""
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

UI_DIR = Path(__file__).parent / "ui"

app = FastAPI(title="ClinicScribe UI")
app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")

if __name__ == "__main__":
    uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8080")))
