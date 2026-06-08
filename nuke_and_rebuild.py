"""Dangerous local reset helper for development only."""

from __future__ import annotations

import os

import mysql.connector

from app_config import get_db_config, load_environment, parse_repo_full_name
from github_monitor import GRAPH_FILE, bootstrap_repo

load_environment()


def get_db():
    return mysql.connector.connect(**get_db_config())


def verify_timestamps():
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    cursor.execute(
        "SELECT commit_sha, created_at FROM github_summaries ORDER BY created_at DESC LIMIT 3"
    )
    rows = cursor.fetchall()
    print("\n🔍 Top 3 Newest Commits in DB (Should be your recent ones, not the first commit!):")
    for r in rows:
        print(f" - SHA: {r['commit_sha'][:8]} | DB Timestamp: {r['created_at']}")
    conn.close()


def main() -> int:
    print("1. Trashing old knowledge graph...")
    if os.path.exists(GRAPH_FILE):
        os.remove(GRAPH_FILE)
        print("   Deleted codebase_graph.json")

    print("2. Nuking database tables...")
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute("TRUNCATE TABLE github_events")
    cursor.execute("TRUNCATE TABLE github_summaries")
    cursor.execute("TRUNCATE TABLE github_bootstrap_status")
    conn.commit()
    cursor.close()
    conn.close()
    print("   Database wiped clean.")

    print("3. Starting fresh bootstrap (this will take a minute)...")
    repo_name = parse_repo_full_name(os.getenv("TARGET_REPO", "aaryaman1221/SecureBank"))
    bootstrap_repo(get_db, repo_name)

    print("✅ Rebuild complete!")
    verify_timestamps()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
