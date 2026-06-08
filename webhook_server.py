"""
webhook_server.py
-----------------
Standalone GitHub webhook receiver.

Runs independently from the banking API (app.py). This is the server you
point your GitHub webhook URL at. It handles signature verification,
event processing, and auto-bootstrap on first connection per repo.

Usage:
    python webhook_server.py                     # default port 5002
    WEBHOOK_PORT=9000 python webhook_server.py   # custom port

Required env vars (loaded from .env):
    DB_HOST, DB_PASSWORD, DB_NAME          — MySQL connection
    GITHUB_WEBHOOK_SECRET                  — webhook signature verification
    GITHUB_TOKEN                           — API access for bootstrap/backfill

Optional:
    WEBHOOK_PORT         — port to listen on (default 5002)
    DB_PORT              — MySQL port (default 3306)
    DB_USER              — MySQL user (default root)
    LLM_API_KEY          — enables LLM summaries instead of heuristic
    BOOTSTRAP_MAX_COMMITS — commit backfill cap (default 500)
"""

import logging
import os
import time

from flask import Flask, jsonify, request

from app_config import get_db_config, load_environment

load_environment()

import mysql.connector
from github_monitor import (
    enqueue_bootstrap,
    enqueue_github_event,
    ensure_github_tables,
    get_bootstrap_status,
    set_bootstrap_status,
    verify_github_signature,
)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger("webhook_server")

app = Flask(__name__)

WEBHOOK_PORT = int(os.getenv("WEBHOOK_PORT", 5002))


def _db_config():
    return get_db_config()


def get_db():
    return mysql.connector.connect(**_db_config())


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    """Quick liveness probe — also verifies DB connectivity."""
    try:
        conn = get_db()
        conn.close()
        db_ok = True
    except Exception as e:
        db_ok = False
        logger.warning("Health check DB failure: %s", e)

    return jsonify({
        "status": "ok" if db_ok else "degraded",
        "database": "connected" if db_ok else "unreachable",
    }), 200 if db_ok else 503


# ---------------------------------------------------------------------------
# Webhook receiver  — this is what you point GitHub at
# ---------------------------------------------------------------------------

@app.route("/webhooks/github", methods=["POST"])
def github_webhook():
    """Receives GitHub webhook events, verifies signature, enqueues processing.

    Configure your GitHub webhook to POST to:
        http://<your-host>:<WEBHOOK_PORT>/webhooks/github

    Set Content-Type to application/json and enter your GITHUB_WEBHOOK_SECRET.
    Subscribe to at least: push, pull_request.
    """
    raw_body = request.get_data()
    delivery_id = request.headers.get(
        "X-GitHub-Delivery", str(int(time.time() * 1000))
    )
    event_type = request.headers.get("X-GitHub-Event", "unknown")
    secret = os.getenv("GITHUB_WEBHOOK_SECRET")

    # --- Signature verification ---
    sig_header = request.headers.get("X-Hub-Signature-256")
    if not verify_github_signature(raw_body, sig_header, secret):
        logger.warning(
            "Rejected webhook %s — invalid signature", delivery_id
        )
        return jsonify({"error": "Invalid GitHub signature"}), 401

    # --- Filter event types ---
    payload = request.get_json(silent=True) or {}
    if event_type not in {"push", "pull_request", "pull_request_review", "pull_request_review_comment"}:
        logger.info("Ignored event type: %s (delivery %s)", event_type, delivery_id)
        return jsonify({
            "message": f"Ignored event type: {event_type}",
        }), 200

    # --- Enqueue for background processing ---
    repo_full_name = (payload.get("repository") or {}).get("full_name", "unknown")
    logger.info(
        "Received %s event for %s (delivery %s)",
        event_type, repo_full_name, delivery_id,
    )

    enqueue_github_event(get_db, payload, event_type, delivery_id)

    # --- Auto-bootstrap on first webhook per repo ---
    if repo_full_name != "unknown":
        try:
            status = get_bootstrap_status(get_db, repo_full_name)
            if not status or status.get("status") not in ("in_progress", "completed"):
                enqueue_bootstrap(get_db, repo_full_name)
                logger.info("Auto-triggered bootstrap for %s", repo_full_name)
        except Exception as e:
            logger.warning("Bootstrap auto-trigger check failed: %s", e)

    return jsonify({
        "message": "Webhook received and queued for processing",
        "delivery_id": delivery_id,
        "repository": repo_full_name,
        "event_type": event_type,
    }), 202


# ---------------------------------------------------------------------------
# Bootstrap management endpoints
# ---------------------------------------------------------------------------

@app.route("/github/bootstrap/status", methods=["GET"])
def bootstrap_status_endpoint():
    """Check bootstrap status for a repo.

    GET /github/bootstrap/status?repo=owner/repo
    """
    repo = request.args.get("repo", "").strip()
    if not repo:
        return jsonify({"error": "repo query parameter is required"}), 400

    status = get_bootstrap_status(get_db, repo)
    if not status:
        return jsonify({
            "repository_full_name": repo,
            "status": None,
            "message": "Never bootstrapped",
        }), 200

    # Serialize datetimes
    for key in ("started_at", "completed_at", "created_at"):
        if status.get(key) and hasattr(status[key], "isoformat"):
            status[key] = status[key].isoformat()

    return jsonify(status), 200


@app.route("/github/bootstrap", methods=["POST"])
def bootstrap_trigger():
    """Manually trigger or force-restart bootstrap for a repo.

    POST /github/bootstrap
    Body: {"repo": "owner/repo", "force": false}
    """
    body = request.get_json(silent=True) or {}
    repo = (body.get("repo") or "").strip()
    force = body.get("force", False)
    if not repo:
        return jsonify({"error": "repo is required in request body"}), 400

    existing = get_bootstrap_status(get_db, repo)

    if existing and existing.get("status") == "in_progress":
        return jsonify({
            "message": "Bootstrap already in progress",
            "status": "in_progress",
        }), 200

    if existing and existing.get("status") == "completed" and not force:
        return jsonify({
            "message": "Bootstrap already completed. Send force=true to re-run.",
            "status": "completed",
        }), 200

    # Reset status so bootstrap_repo doesn't skip it
    if force and existing:
        set_bootstrap_status(get_db, repo, "pending", detail="Forced re-bootstrap")

    enqueue_bootstrap(get_db, repo)
    logger.info("Bootstrap manually triggered for %s (force=%s)", repo, force)

    return jsonify({
        "message": f"Bootstrap started for {repo}",
        "status": "in_progress",
    }), 202


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def _startup():
    """Run once at import time — create tables if they don't exist."""
    try:
        ensure_github_tables(get_db)
        logger.info("Database tables verified")
    except Exception as e:
        logger.error("Failed to initialize database tables: %s", e)
        raise


_startup()


if __name__ == "__main__":
    logger.info("Starting GitHub webhook server on port %d", WEBHOOK_PORT)
    logger.info(
        "Configure your GitHub webhook URL to: "
        "http://<your-host>:%d/webhooks/github", WEBHOOK_PORT
    )
    app.run(host="0.0.0.0", port=WEBHOOK_PORT, debug=False)
