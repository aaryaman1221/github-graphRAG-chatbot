"""Manual bootstrap helper for a target repository."""

from __future__ import annotations

import os

import mysql.connector

from app_config import get_db_config, load_environment, parse_repo_full_name
from github_monitor import bootstrap_repo


def get_db():
    return mysql.connector.connect(**get_db_config())


def main() -> int:
    load_environment()
    repo_name = parse_repo_full_name(os.getenv("TARGET_REPO", "aaryaman1221/SecureBank"))
    print(f"🚀 Forcing manual bootstrap for {repo_name}...")
    bootstrap_repo(get_db, repo_name)
    print("✅ Complete! You can now refresh your Streamlit app.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

