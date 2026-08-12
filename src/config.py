"""Shared configuration, loaded from .env / environment."""
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data" / "cuad"
RAW_DIR = DATA_DIR / "raw"
REVIEW_QUEUE_DIR = REPO_ROOT / "review_queue"
EVAL_REPORTS_DIR = REPO_ROOT / "eval_reports"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5")
PORT = int(os.environ.get("PORT", "5004"))

# Extractions below this confidence are routed to the review queue instead
# of being treated as auto-approved.
CONFIDENCE_THRESHOLD = float(os.environ.get("CONFIDENCE_THRESHOLD", "0.75"))
