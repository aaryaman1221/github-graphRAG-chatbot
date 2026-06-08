"""Shared configuration helpers for the GitHub chatbot project."""

from __future__ import annotations

import os
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent
ENV_FILE = ROOT_DIR / ".env"


def load_environment() -> None:
    """Load local environment overrides if python-dotenv is installed."""
    try:
        from dotenv import load_dotenv
    except ImportError:
        return

    load_dotenv(dotenv_path=ENV_FILE)


def get_db_config(default_database: str = "github_chatbot") -> dict[str, object]:
    """Return a mysql.connector-compatible configuration dictionary."""
    return {
        "host": os.getenv("DB_HOST", "localhost"),
        "port": int(os.getenv("DB_PORT", 3306)),
        "user": os.getenv("DB_USER", "root"),
        "password": os.environ.get("DB_PASSWORD", ""),
        "database": os.getenv("DB_NAME", default_database),
    }


def get_graph_file(default_name: str = "codebase_graph.json") -> str:
    """Return the graph file location, configurable via GRAPH_FILE."""
    return os.getenv("GRAPH_FILE", str(ROOT_DIR / default_name))


def parse_repo_full_name(value: str) -> str:
    """Normalize and validate an owner/repo string."""
    repo = (value or "").strip()
    if not repo or "/" not in repo:
        raise ValueError("Repository must be in owner/repo format")
    owner, name = repo.split("/", 1)
    if not owner.strip() or not name.strip():
        raise ValueError("Repository must be in owner/repo format")
    return f"{owner.strip()}/{name.strip()}"

