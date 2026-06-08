"""Small CLI surface for running the release package."""

from __future__ import annotations

import argparse
import subprocess
import sys

from app_config import get_db_config, load_environment, parse_repo_full_name


def _run_python_module(module: str, *args: str) -> int:
    command = [sys.executable, "-m", module, *args]
    completed = subprocess.run(command, check=False)
    return completed.returncode


def run_ui() -> int:
    load_environment()
    return _run_python_module("streamlit", "run", "github_chatbot.py")


def run_webhook() -> int:
    load_environment()
    return _run_python_module("webhook_server")


def run_bootstrap(repo: str) -> int:
    load_environment()
    repo_full_name = parse_repo_full_name(repo)

    from github_monitor import bootstrap_repo
    import mysql.connector

    def get_db():
        return mysql.connector.connect(**get_db_config())

    bootstrap_repo(get_db, repo_full_name)
    return 0


def main_ui() -> int:
    return run_ui()


def main_webhook() -> int:
    return run_webhook()


def main_bootstrap() -> int:
    parser = argparse.ArgumentParser(prog="github-chatbot-bootstrap")
    parser.add_argument("repo", help="owner/repo to index")
    args = parser.parse_args()
    return run_bootstrap(args.repo)


def main() -> int:
    parser = argparse.ArgumentParser(prog="github-chatbot")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("ui", help="Start the Streamlit UI")
    subparsers.add_parser("webhook", help="Start the webhook receiver")
    bootstrap_parser = subparsers.add_parser("bootstrap", help="Bootstrap a repo")
    bootstrap_parser.add_argument("repo", help="owner/repo to index")

    args = parser.parse_args()

    if args.command == "ui":
        return run_ui()
    if args.command == "webhook":
        return run_webhook()
    if args.command == "bootstrap":
        return run_bootstrap(args.repo)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
