""" CLI entrypoint for Phase 1.

Usage:
    python run.py [config_path] [discover_limit]

Example:
    python run.py config/product_profile.json 25
"""
import logging
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.config import ConfigError  # noqa: E402
from src.pipeline import run_pipeline  # noqa: E402

if __name__ == "__main__":
    cfg = sys.argv[1] if len(sys.argv) > 1 else "config/product_profile.json"
    try:
        limit = int(sys.argv[2]) if len(sys.argv) > 2 else 25
    except ValueError:
        sys.exit(f"discover_limit must be a positive integer, got {sys.argv[2]!r}")
    if limit < 1:
        sys.exit(f"discover_limit must be a positive integer, got {limit}")
    try:
        run_pipeline(cfg, limit)
    except ConfigError as exc:
        sys.exit(str(exc))
