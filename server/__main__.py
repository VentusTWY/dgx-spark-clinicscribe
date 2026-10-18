"""python3 -m server  ->  ClinicScribe API + UI on http://127.0.0.1:8080.

PORT changes the port. HOST=0.0.0.0 opens it to the network; there is no login, so only do that on a
network you trust (anyone who can reach the port can read and upload consults)."""
import os

import uvicorn

from .app import app

uvicorn.run(app, host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8080")), log_level="warning")
