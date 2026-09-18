"""Load the repository environment consistently in every process entry point."""

from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_project_environment() -> None:
    load_dotenv(PROJECT_ROOT / ".env")
