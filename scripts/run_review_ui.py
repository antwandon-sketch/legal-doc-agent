#!/usr/bin/env python3
"""Run the review UI on PORT (default 5004, see .env)."""
import sys
from pathlib import Path

import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.config import PORT  # noqa: E402

if __name__ == "__main__":
    uvicorn.run("src.app.main:app", host="127.0.0.1", port=PORT, reload=False)
