"""Foreground bootstrap smoke test."""

from __future__ import annotations

import logging
import os

import mysql.connector

from app_config import get_db_config, load_environment, parse_repo_full_name
from github_monitor import bootstrap_repo

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
load_environment()


def get_test_db():
    return mysql.connector.connect(**get_db_config())


def main() -> int:
    repo = parse_repo_full_name(os.getenv("TARGET_REPO", "aaryaman1221/SecureBank"))
    print("Starting manual foreground bootstrap...")
    bootstrap_repo(get_test_db, repo)
    print("Finished!")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

