# GitHub Chatbot

GitHub Chatbot is a Streamlit app that uses a Graph-RAG system plus webhook service that indexes repository activity, stores change summaries in MySQL, and lets you ask questions about commits, pull requests, authors, files, and dependency impact.

It supports:

- Python
- Go
- JavaScript and TypeScript
- Several other common codebases with import-based structure

## What it does

- Receives GitHub webhook events and stores them in MySQL
- Builds a repository graph from commits, files, directories, and dependency edges
- Bootstraps a repository by scanning the tree, file contents, and commit history
- Answers repository questions from stored summaries and graph traversal
- Falls back to heuristic summaries when no LLM key is configured

## Project Layout

- `launcher.py` provides the `github-chatbot` CLI
- `github_chatbot.py` runs the Streamlit UI
- `webhook_server.py` runs the GitHub webhook receiver
- `github_monitor.py` contains bootstrap, graph, summary, and query logic
- `app_config.py` loads environment settings and DB config

## Requirements

- Python 3.10+
- MySQL database
- GitHub personal access token with repo read access
- GitHub webhook secret
- Optional: `LLM_API_KEY` for model-generated summaries

## Install

1. Create and activate a virtual environment.
2. Install the project:

```bash
pip install -e .[dev]
```

3. Copy the example environment file:

```bash
cp .env.example .env
```

4. Fill in the values in `.env`.

## Configuration

The application reads configuration from environment variables or `.env`.

Required:

- `DB_HOST`
- `DB_PORT`
- `DB_USER`
- `DB_PASSWORD`
- `DB_NAME`
- `GITHUB_WEBHOOK_SECRET`
- `GITHUB_TOKEN`

Optional:

- `LLM_API_KEY`
- `LLM_MODEL`
- `WEBHOOK_PORT`
- `BOOTSTRAP_MAX_COMMITS`
- `GRAPH_FILE`

Defaults from `.env.example`:

- MySQL host: `localhost`
- MySQL port: `3306`
- Webhook port: `5002`
- LLM model: `gemini-1.5-flash`
- Bootstrap commit cap: `500`
- Graph file: `codebase_graph.json`

## Running The App

Use the CLI entrypoint:

```bash
github-chatbot ui
```

This starts the Streamlit interface for querying repositories.

```bash
github-chatbot webhook
```

This starts the Flask webhook receiver. By default it listens on port `5002`.

```bash
github-chatbot bootstrap owner/repo
```

This performs a manual bootstrap of a repository and begins indexing it into MySQL and the graph file.

You can also run the modules directly if needed:

```bash
python github_chatbot.py
python webhook_server.py
```

## Recommended Workflow

1. Start the webhook server.
2. Configure a GitHub webhook to send `push`, `pull_request`, `pull_request_review`, and `pull_request_review_comment` events.
3. Bootstrap the repository once.
4. Open the Streamlit UI and ask questions against the indexed history.

## GitHub Webhook Setup

Set your webhook target to:

```text
http://<your-host>:<WEBHOOK_PORT>/webhooks/github
```

The server verifies `X-Hub-Signature-256` with `GITHUB_WEBHOOK_SECRET`.

Useful webhook-related endpoints:

- `GET /health` for a basic liveness and DB check
- `GET /github/bootstrap/status?repo=owner/repo` to inspect bootstrap progress
- `POST /github/bootstrap` to trigger or force re-bootstrap

Example bootstrap request body:

```json
{
  "repo": "owner/repo",
  "force": false
}
```

## Bootstrap Process

Bootstrap runs in three phases:

1. Scan the repository tree and add files and directories to the graph
2. Fetch file contents and extract dependency edges from imports or module manifests
3. Backfill commit history and store summaries for historical lookup

For Go repositories, the bootstrapper now understands:

- `*.go` files
- `go.mod`
- `go.work`
- grouped import blocks
- grouped `require`, `use`, and `replace` blocks

## Supported Query Types

The UI and graph layer support questions like:

- What changed in the last push?
- Which files affect a given module?
- Who touched a specific file?
- What commit introduced a function or dependency?
- What is the blast radius of a commit SHA?

## Notes On Summaries

- If `LLM_API_KEY` is set, the app uses the configured model to generate summaries.
- If no key is present, it falls back to a deterministic heuristic summary.
- The graph file path can be changed with `GRAPH_FILE`.

## Development Notes

- The repository contains local smoke tests for bootstrap and graph behavior.
- `test_boot.py` expects a reachable MySQL instance.
- `test_graph.py` is useful for checking whether the graph file is being read and written correctly.

## Troubleshooting

- If the UI says the database is unavailable, verify `DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, and `DB_NAME`.
- If webhook requests are rejected, confirm the webhook secret matches `GITHUB_WEBHOOK_SECRET`.
- If a repository is not showing up in the UI, bootstrap it or send a webhook event for it first.
- If graph queries return nothing, confirm `GRAPH_FILE` points to the file you expect and that bootstrap completed successfully.

## Example `.env`

```bash
DB_HOST=localhost
DB_PORT=3306
DB_USER=root
DB_PASSWORD=change-me
DB_NAME=github_chatbot

GITHUB_WEBHOOK_SECRET=change-me
GITHUB_TOKEN=change-me

LLM_API_KEY=
LLM_MODEL=gemini-1.5-flash

WEBHOOK_PORT=5002
BOOTSTRAP_MAX_COMMITS=500
GRAPH_FILE=codebase_graph.json
```
