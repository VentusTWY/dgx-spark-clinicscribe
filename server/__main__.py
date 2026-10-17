"""python3 -m server  ->  ClinicScribe API + UI on http://0.0.0.0:8080 (PORT to change)."""
import os

import uvicorn

from .app import app

uvicorn.run(app, host=os.environ.get("HOST", "0.0.0.0"), port=int(os.environ.get("PORT", "8080")), log_level="warning")
