# GitHub Chatbot

GitHub webhook chatbot with a Streamlit UI, repo bootstrapper, and Graph RAG-style query layer.

## What this release layout gives you

- A repeatable install via `pyproject.toml`
- Shared environment configuration via `.env`
- A single CLI surface for UI, webhook server, and repo bootstrap
- Repo-agnostic bootstrap support using `owner/repo`
- **Currently works only on python projects**

## Quick Start

1. Create a virtual environment.
2. Install the project:

```bash
pip install -e .[dev]
```

3. Copy `.env.example` to `.env` and fill in the values.
4. Start the webhook receiver:

```bash
github-chatbot webhook
```

5. Start the UI:

```bash
github-chatbot ui
```

6. Bootstrap a repository:

```bash
github-chatbot bootstrap owner/repo
```

## Required configuration

Set these values in `.env` or your deployment environment:

- `DB_HOST`
- `DB_PORT`
- `DB_USER`
- `DB_PASSWORD`
- `DB_NAME`
- `GITHUB_WEBHOOK_SECRET`
- `GITHUB_TOKEN`

Optional values:

- `LLM_API_KEY`
- `LLM_MODEL`
- `WEBHOOK_PORT`
- `BOOTSTRAP_MAX_COMMITS`
- `GRAPH_FILE`

## Testing and Maintainence
- ignore nuke_and_rebuild.py, kickstart.py, test_boot.py, test_graph.py, they are to test bootstrapping and graph generation

## Deployment notes

- The webhook receiver expects GitHub events at `/webhooks/github`.
- Use HTTPS in production.
- Use a real MySQL instance for persistence.
- `GRAPH_FILE` can be pointed at a mounted volume if you want the graph to survive redeploys.

## Things to Note

- Use proper filenames or function names for best result (ex: Who made changes to webhook server -> Who made changes to webhook_server.py)
- generation slow due to graph being used for even the smallest query (project emphasis on graph)

