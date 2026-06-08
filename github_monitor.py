import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from datetime import datetime

import mysql.connector
import requests
import networkx as nx
from networkx.readwrite import json_graph

from tqdm import tqdm
from app_config import get_graph_file

logger = logging.getLogger(__name__)

GITHUB_API_BASE = "https://api.github.com"
GRAPH_FILE = get_graph_file()
BOOTSTRAP_MAX_COMMITS = int(os.getenv("BOOTSTRAP_MAX_COMMITS", 500))

# File extensions to scan for full-file dependency analysis
SOURCE_EXTENSIONS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".go", ".java", ".rs",
    ".rb", ".php", ".c", ".cpp", ".h", ".hpp", ".cs", ".swift",
    ".kt", ".scala", ".r", ".R", ".vue", ".svelte",
}

# Patterns for identifying entry-point files
ENTRY_POINT_NAMES = {
    "main", "index", "app", "server", "__main__", "manage",
    "wsgi", "asgi", "cli", "entrypoint",
}

# Directory names that indicate shared utilities
UTILITY_DIRS = {
    "utils", "util", "lib", "libs", "common", "shared",
    "helpers", "helper", "core", "pkg", "internal",
}

#  All IGNORED methods to remove unnecessary files from initial bootstrapping process
IGNORED_DIRECTORIES = {
    "node_modules", "venv", ".venv", "env", "dist", "build", ".next", ".git"
}

IGNORED_FILENAMES = {
    ".ds_store", "cargo.lock", "gemfile.lock", "package-lock.json",
    "poetry.lock", "pnpm-lock.yaml", "yarn.lock",
}
IGNORED_SUFFIXES = (
    ".bin", ".dll", ".exe", ".gif", ".gz", ".ico", ".jpeg", ".jpg",
    ".lock", ".mp3", ".mp4", ".pdf", ".png", ".so", ".svg", ".tar",
    ".tgz", ".ttf", ".woff", ".woff2", ".zip",
)

def ensure_github_tables(get_db):
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS github_events (
                id INT AUTO_INCREMENT PRIMARY KEY,
                delivery_id VARCHAR(255) UNIQUE,
                event_type VARCHAR(64) NOT NULL,
                repository_full_name VARCHAR(255) NOT NULL,
                actor_login VARCHAR(255),
                commit_sha VARCHAR(128),
                pr_number INT,
                source_url VARCHAR(1024),
                payload_json LONGTEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS github_summaries (
                id INT AUTO_INCREMENT PRIMARY KEY,
                delivery_id VARCHAR(255),
                event_type VARCHAR(64) NOT NULL,
                repository_full_name VARCHAR(255) NOT NULL,
                actor_login VARCHAR(255),
                commit_sha VARCHAR(128),
                pr_number INT,
                source_url VARCHAR(1024),
                summary_text LONGTEXT NOT NULL,
                diff_text LONGTEXT,
                files_json LONGTEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()
    ensure_bootstrap_table(get_db)

def verify_github_signature(raw_body, signature_header, secret):
    if not secret:
        return True
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header.split("=", 1)[1])

def _github_headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GITHUB_API_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

def _is_noise_file(filename):
    lower_path = filename.lower().replace("\\", "/")
    
    # 1. Check directories (do this first, as it catches entire folders of noise quickly)
    path_parts = set(lower_path.split("/"))
    if path_parts.intersection(IGNORED_DIRECTORIES):
        return True

    # Extract just the filename for the next checks
    name = lower_path.rsplit("/", 1)[-1]
    
    # 2. Check exact filenames
    if name in IGNORED_FILENAMES:
        return True
        
    # 3. Check suffixes
    return name.endswith(IGNORED_SUFFIXES)

def _truncate(text, limit=5000):
    if not text:
        return ""
    return text if len(text) <= limit else f"{text[:limit]}\n... [truncated]"

def _fetch_json(url, params=None):
    response = requests.get(url, headers=_github_headers(), params=params, timeout=30)
    response.raise_for_status()
    return response.json()

def fetch_commit_files(repo_full_name, sha):
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/commits/{sha}"
    payload = _fetch_json(url)
    files = payload.get("files", []) or []
    return payload, files

def fetch_pull_request_files(repo_full_name, pr_number):
    all_files = []
    page = 1
    while True:
        url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/pulls/{pr_number}/files"
        files = _fetch_json(url, params={"per_page": 100, "page": page})
        if not files:
            break
        all_files.extend(files)
        if len(files) < 100:
            break
        page += 1
    return all_files


# filter and ranker function for llm processing, increase max_files for bigger repositories or detailed analysis
def build_compact_diff(files, max_files=12):
    filtered = []
    for item in files:
        filename = item.get("filename") or item.get("path") or "unknown"
        if _is_noise_file(filename):
            continue
        patch = item.get("patch")
        if not patch:
            continue
        filtered.append(
            {
                "filename": filename,
                "status": item.get("status", "modified"),
                "additions": item.get("additions", 0),
                "deletions": item.get("deletions", 0),
                "changes": item.get("changes", 0),
                "patch": _truncate(patch, 4000),
            }
        )
    filtered.sort(key=lambda x: x.get("changes", 0), reverse=True)
    return filtered[:max_files]

# formatter - dictionaries generated by build_compact_diff-> single highly readable block of text
def render_diff_text(files):
    sections = []
    for item in files:
        sections.append(
            "\n".join(
                [
                    f"File: {item['filename']}",
                    f"Status: {item.get('status', 'modified')}",
                    f"Additions: {item.get('additions', 0)}",
                    f"Deletions: {item.get('deletions', 0)}",
                    "Patch:",
                    item.get("patch", ""),
                ]
            )
        )
    return "\n\n".join(sections)

def _llm_client_config():
    api_key = os.getenv("LLM_API_KEY") 
    model = os.getenv("LLM_MODEL", "gemini-1.5-flash")
    return api_key, model

def summarize_with_llm(repo_full_name, event_type, actor_login, meta, compact_files, raw_diff):
    api_key, model = _llm_client_config()
    
    file_overview = "\n".join(
        [
            f"- {item['filename']} ({item.get('status', 'modified')} | +{item.get('additions', 0)} / -{item.get('deletions', 0)})"
            for item in compact_files
        ]
    ) or "- No text patches were available."

    user_prompt = f"""
Repository: {repo_full_name}
Event: {event_type}
Actor: {actor_login or 'unknown'}
Metadata: {json.dumps(meta, ensure_ascii=True, default=str)}

Files changed:
{file_overview}

Diff:
{_truncate(raw_diff, 20000)}
"""

    if not api_key:
        return heuristic_summary(repo_full_name, event_type, compact_files, meta)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    
    payload = {
        "systemInstruction": {
            "parts": [{"text": "You are an expert senior developer reviewing code changes. Explain what changed and why it matters in plain English. Do not recite code lines. Focus on behavior, architecture, bug fixes, and risks. Keep it to at most 3 short paragraphs."}]
        },
        "contents": [{
            "parts": [{"text": user_prompt}]
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 1500
        }
    }

    try:
        # EXPONENTIAL BACKOFF RETRY LOOP
        for attempt in range(5):
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            
            if response.status_code == 429:
                sleep_time = 2 ** attempt  # Wait 1, 2, 4, 8, 16 seconds
                logger.warning("429 Rate limit hit in summarize_with_llm. Retrying in %ss...", sleep_time)
                time.sleep(sleep_time)
                continue
                
            response.raise_for_status()
            break  # Success, exit the retry loop
        else:
            # Exhausted retries
            response.raise_for_status()
            
        data = response.json()
        generated_text = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])[0].get("text", "").strip()
        return generated_text or heuristic_summary(repo_full_name, event_type, compact_files, meta)
        
    except Exception as exc:
        logger.exception("LLM summary failed: %s", exc)
        return heuristic_summary(repo_full_name, event_type, compact_files, meta)

def heuristic_summary(repo_full_name, event_type, compact_files, meta):
    if not compact_files:
        return f"{event_type.title()} event in {repo_full_name}. GitHub did not provide text patches for this change."
    parts = []
    for item in compact_files[:5]:
        filename = item["filename"]
        status = item.get("status", "modified")
        additions = item.get("additions", 0)
        deletions = item.get("deletions", 0)
        parts.append(f"{filename} ({status}, +{additions}/-{deletions})")
    extra = ""
    if len(compact_files) > 5:
        extra = f" and {len(compact_files) - 5} more file(s)"
    action = meta.get("action")
    action_part = f" action={action}" if action else ""
    return (
        f"{event_type.title()} event in {repo_full_name}{action_part}. "
        f"Key files touched: {', '.join(parts)}{extra}. "
        "This likely changes behavior in the listed areas and should be reviewed for impact and regressions."
    )

def persist_event(get_db, delivery_id, event_type, payload, summary_record=None):
    conn = get_db()
    cursor = conn.cursor()
    try:
        repository = payload.get("repository") or {}
        repo_full_name = repository.get("full_name") or "unknown/unknown"
        actor_login = (payload.get("sender") or {}).get("login")
        commit_sha = payload.get("after") or ((payload.get("pull_request") or {}).get("head") or {}).get("sha")
        pr_number = (payload.get("pull_request") or {}).get("number")
        source_url = repository.get("html_url")
        cursor.execute(
            """
            INSERT INTO github_events
            (delivery_id, event_type, repository_full_name, actor_login, commit_sha, pr_number, source_url, payload_json)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                event_type = VALUES(event_type),
                repository_full_name = VALUES(repository_full_name),
                actor_login = VALUES(actor_login),
                commit_sha = VALUES(commit_sha),
                pr_number = VALUES(pr_number),
                source_url = VALUES(source_url),
                payload_json = VALUES(payload_json)
            """,
            (
                delivery_id, event_type, repo_full_name, actor_login,
                commit_sha, pr_number, source_url, json.dumps(payload, default=str),
            ),
        )

        if summary_record:
            cursor.execute(
                """
                INSERT INTO github_summaries
                (delivery_id, event_type, repository_full_name, actor_login, commit_sha, pr_number, source_url, summary_text, diff_text, files_json)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON DUPLICATE KEY UPDATE delivery_id = delivery_id
                """,
                (
                    delivery_id, event_type, repo_full_name, actor_login,
                    commit_sha, pr_number, source_url, summary_record["summary_text"],
                    summary_record.get("diff_text"), summary_record.get("files_json"),
                ),
            )
        conn.commit()
    finally:
        cursor.close()
        conn.close()

# ==========================================
# GRAPH RAG UTILS (Phases 1, 2, and 3)
# ==========================================

# Re-entrant so a caller that already holds the lock can safely call helpers
# that also acquire it (prevents future footguns without any runtime cost).
_graph_lock = threading.RLock()
_graph_cache: "nx.DiGraph | None" = None  # in-process singleton


def _load_from_disk() -> "nx.DiGraph":
    """Read the graph from disk.  Caller MUST hold _graph_lock."""
    if os.path.exists(GRAPH_FILE):
        try:
            with open(GRAPH_FILE, "r") as f:
                data = json.load(f)
                return json_graph.node_link_graph(data)
        except Exception as e:
            logger.error("Failed to load graph: %s", e)
    return nx.DiGraph()


def _save_graph_locked(G: "nx.DiGraph") -> None:
    """Persist *G* to disk atomically and update the cache.
    Caller MUST hold _graph_lock."""
    global _graph_cache
    _graph_cache = G
    tmp = GRAPH_FILE + ".tmp"
    data = json_graph.node_link_data(G)
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, GRAPH_FILE)


def _get_graph() -> "nx.DiGraph":
    """Return the in-process singleton, loading from disk on first call.
    Caller MUST hold _graph_lock."""
    global _graph_cache
    if _graph_cache is None:
        _graph_cache = _load_from_disk()
    return _graph_cache


def load_graph() -> "nx.DiGraph":
    """Return the in-process graph singleton (backward-compatible for read callers)."""
    with _graph_lock:
        return _get_graph()


def save_graph(G):
    """Saves the graph state to disk atomically to prevent corruption."""
    with _graph_lock:
        tmp = GRAPH_FILE + ".tmp"
        
        # 1. Convert the NetworkX graph object to a JSON-serializable dictionary
        data = json_graph.node_link_data(G)
        
        # 2. Write the data to a temporary file first
        with open(tmp, 'w') as f:
            json.dump(data, f, indent=2)
            
        # 3. Atomically replace the old graph file with the new one
        os.replace(tmp, GRAPH_FILE)

def update_knowledge_graph(repo_full_name, commit_sha, modified_files, dependencies,
                           actor_login=None, committed_at=None):
    """Integrates extracted dependencies and modified files into the global knowledge graph.

    The entire load-mutate-save sequence is executed under _graph_lock so
    concurrent webhook threads cannot overwrite each other's edges.
    """
    with _graph_lock:
        G = _get_graph()

        commit_node = f"commit:{commit_sha}"
        G.add_node(
            commit_node,
            type="commit",
            repo=repo_full_name,
            author=actor_login or "",
            timestamp=committed_at or "",
        )

        # Author node + AUTHORED edge
        if actor_login:
            author_node = f"author:{actor_login}"
            if not G.has_node(author_node):
                G.add_node(author_node, type="author")
            if not G.has_edge(author_node, commit_node):
                G.add_edge(author_node, commit_node, relationship="AUTHORED")

        # Link the commit to every file it modified
        for filename in modified_files:
            file_node = f"file:{filename}"
            if not G.has_node(file_node):
                G.add_node(file_node, type="file", repo=repo_full_name)
            if not G.has_edge(commit_node, file_node):
                G.add_edge(commit_node, file_node, relationship="MODIFIED", timestamp=committed_at or "")

        # Map the downstream module dependencies
        for source, rel, target in dependencies:
            source_node = f"file:{source}"
            target_node = f"module:{target}"
            if not G.has_node(source_node):
                G.add_node(source_node, type="file", repo=repo_full_name)
            if not G.has_node(target_node):
                G.add_node(target_node, type="module")
            if not G.has_edge(source_node, target_node):
                G.add_edge(source_node, target_node, relationship=rel)

        save_graph(G)
        logger.info("Graph Updated | Nodes: %d | Edges: %d",
                    G.number_of_nodes(), G.number_of_edges())

def extract_file_dependencies(compact_files):
    """Scans code diff patches to extract import/require statements."""
    dependencies = []
    patterns = [
        r"^\+?\s*from\s+([a-zA-Z0-9_.-]+)\s+import",
        r"^\+?\s*import\s+([a-zA-Z0-9_.-]+)",
        r"from\s+['\"]([^'\"]+)['\"]",
        r"require\(['\"]([^'\"]+)['\"]\)"
    ]
    
    for item in compact_files:
        source_file = item.get("filename")
        patch = item.get("patch", "")
        if not patch or not source_file:
            continue
            
        for line in patch.split('\n'):
            if not line.startswith('+') and not line.startswith(' '):
                continue
                
            for pattern in patterns:
                match = re.search(pattern, line)
                if match:
                    target_module = match.group(1)
                    edge = (source_file, "DEPENDS_ON", target_module)
                    if edge not in dependencies:
                        dependencies.append(edge)
    return dependencies

# ==========================================
# GRAPH RAG — INTENT CLASSIFICATION + TRAVERSAL
# ==========================================

# Matches any 7–40 character hex string that looks like a commit SHA.
_SHA_RE = re.compile(r'\b[0-9a-f]{7,40}\b', re.IGNORECASE)

_INTENT_PATTERNS = [
    ("blast_radius", re.compile(
        r"\b(impact|impacted|affect|affected|break|depend|downstream|ripple|what.{0,20}uses|which.{0,20}import|if.{0,20}change)\b",
        re.I,
    )),
    ("author_query", re.compile(
        r"\b(who|author|wrote|pushed|committed.by|changes.by)\b",
        re.I,
    )),
    ("file_history", re.compile(
        r"\b(history|when.{0,10}added|what.{0,20}changed.in|commits.{0,10}touching|touched|modified)\b",
        re.I,
    )),
    ("module_origin", re.compile(
        r"\b(where.{0,20}import|where.{0,20}used|what.{0,20}uses|which.{0,20}depend|added.{0,10}import|introduced)\b",
        re.I,
    )),
]


def classify_query_intent(question):
    """
    Returns one of: 'blast_radius', 'author_query', 'file_history',
    'module_origin', or 'recent' (skip the graph, use SQL).

    A commit SHA anywhere in the question always maps to 'blast_radius'
    so the caller seeds the graph from that specific commit node.
    """
    # Detect commit SHA anywhere in the question — highest priority.
    if _SHA_RE.search(question):
        logger.info("Query intent classified as 'blast_radius' (SHA detected) for: %s", question)
        return "blast_radius"

    for intent, pattern in _INTENT_PATTERNS:
        if pattern.search(question):
            logger.info("Query intent classified as '%s' for: %s", intent, question)
            return intent
    return "recent"


def _load_graph_safe():
    """Loads the graph, returning None on any failure or if empty."""
    if not os.path.exists(GRAPH_FILE):
        logger.warning("Graph file not found at %s — graph queries will return empty", GRAPH_FILE)
        return None
    G = load_graph()
    if not G or not G.nodes:
        logger.warning("Graph loaded but is empty")
        return None
    return G

def get_blast_radius_from_commit_sha(sha, max_hops=2):
    """
    Given a commit SHA, find all files it modified, then find all
    other commits that touched those same files.
    """
    G = _load_graph_safe()
    if G is None:
        return []

    # Try exact match first, then prefix match for short SHAs
    commit_node = f"commit:{sha}"
    if not G.has_node(commit_node):
        matches = [n for n in G.nodes 
                   if n.startswith("commit:") and n[7:].startswith(sha)]
        if not matches:
            logger.info("blast_radius_sha: no commit node for %s", sha)
            return []
        commit_node = matches[0]

    # Step 1: find all files this commit modified
    modified_files = [
        n for n in G.successors(commit_node)
        if G.nodes[n].get("type") == "file"
    ]

    if not modified_files:
        logger.info("blast_radius_sha: commit %s has no file edges", sha)
        return []

    logger.info("blast_radius_sha: commit touched %d files", len(modified_files))

    # Step 2: find all commits that touched those same files
    sibling_commits = set()
    
    # FIX: Ensure the commit we actually asked about is in the results!
    sibling_commits.add(commit_node.replace("commit:", ""))
    for file_node in modified_files:
        for src in G.predecessors(file_node):
            if src.startswith("commit:") and src != commit_node:
                sibling_commits.add(src.replace("commit:", ""))

    # Step 3: optionally go one hop deeper via shared modules
    if max_hops >= 2:
        for file_node in modified_files:
            for mod_node in G.successors(file_node):
                if G.nodes[mod_node].get("type") == "module":
                    for sibling_file in G.predecessors(mod_node):
                        if G.nodes[sibling_file].get("type") == "file":
                            for src in G.predecessors(sibling_file):
                                if src.startswith("commit:") and src != commit_node:
                                    sibling_commits.add(src.replace("commit:", ""))

    logger.info("blast_radius_sha: found %d related commits", len(sibling_commits))
    return list(sibling_commits)

def get_blast_radius_commits(keyword, max_hops=2):
    """
    Forward traversal: given a file or module name, find all commits that
    touched files which depend on it — i.e. blast radius of a change.

    Graph path:
        module:{keyword} <-DEPENDS_ON- file:X <-MODIFIED- commit:Y
    """
    G = _load_graph_safe()
    if G is None:
        return []

    seed_nodes = [n for n in G.nodes if keyword.lower() in n.lower()]
    if not seed_nodes:
        logger.info("blast_radius: no graph nodes matching '%s'", keyword)
        return []

    commits = set()
    for seed in seed_nodes:
        node_type = G.nodes[seed].get("type", "")

        if node_type == "module":
            # Files that import this module
            for file_node in G.predecessors(seed):
                if G.nodes[file_node].get("type") == "file":
                    for src in G.predecessors(file_node):
                        if src.startswith("commit:"):
                            commits.add(src.replace("commit:", ""))

        elif node_type == "file":
            # Direct commits to this file
            for src in G.predecessors(seed):
                if src.startswith("commit:"):
                    commits.add(src.replace("commit:", ""))
            # Sibling files that share a dependency (2-hop)
            if max_hops >= 2:
                for mod_node in G.successors(seed):
                    if G.nodes[mod_node].get("type") == "module":
                        for sibling in G.predecessors(mod_node):
                            if sibling != seed and G.nodes[sibling].get("type") == "file":
                                for src in G.predecessors(sibling):
                                    if src.startswith("commit:"):
                                        commits.add(src.replace("commit:", ""))

    logger.info("blast_radius: found %d commits for keyword '%s'", len(commits), keyword)
    return list(commits)


def get_commits_by_author(keyword):
    """
    Author traversal: find all commits by an author whose login
    contains the keyword.

    Graph path:
        author:{keyword} -AUTHORED-> commit:Y

    Falls back to scanning the author attribute on commit nodes for
    graphs built before author nodes were added.
    """
    G = _load_graph_safe()
    if G is None:
        return []

    commits = set()

    # Primary: walk AUTHORED edges from matching author nodes
    for node in G.nodes:
        if node.startswith("author:") and keyword.lower() in node.lower():
            for commit_node in G.successors(node):
                if commit_node.startswith("commit:"):
                    commits.add(commit_node.replace("commit:", ""))

    # Fallback: scan author attribute on commit nodes (older graph format)
    if not commits:
        for node, attrs in G.nodes(data=True):
            if (node.startswith("commit:")
                    and keyword.lower() in str(attrs.get("author", "")).lower()):
                commits.add(node.replace("commit:", ""))

    logger.info("author_query: found %d commits for keyword '%s'", len(commits), keyword)
    return list(commits)


def get_file_history_commits(keyword):
    """
    File history traversal: find all commits that ever modified a file
    whose path contains the keyword.

    Graph path:
        commit:Y -MODIFIED-> file:{keyword}
    """
    G = _load_graph_safe()
    if G is None:
        return []

    file_nodes = [
        n for n in G.nodes
        if n.startswith("file:") and keyword.lower() in n.lower()
    ]
    if not file_nodes:
        logger.info("file_history: no file nodes matching '%s'", keyword)
        return []

    commits = set()
    for file_node in file_nodes:
        for src in G.predecessors(file_node):
            if src.startswith("commit:"):
                commits.add(src.replace("commit:", ""))

    logger.info("file_history: found %d commits for keyword '%s'", len(commits), keyword)
    return list(commits)


def get_commits_from_graph(keyword, max_hops=2, intent="auto"):
    """
    Unified entry point for all graph-based commit lookups.

    Parameters
    ----------
    keyword : str
    Technical term extracted from the user's question.  When the
    keyword is a commit SHA (7-40 hex chars) the function seeds the
    graph directly from that commit node and returns its blast radius
    rather than doing a slower string-match traversal.
    max_hops : int
    Traversal depth. Only used by blast_radius mode.
    intent : str
    One of 'blast_radius', 'author_query', 'file_history',
    'module_origin', or 'auto'. When 'auto', infers the right
    traversal from the keyword's shape.
    """
    if not keyword:
        return []

    # If the keyword looks like a SHA, route it directly to our dedicated SHA traverser
    if _SHA_RE.fullmatch(keyword):
        # FIX: Force max_hops=1 to prevent standard library supernode explosions
        return get_blast_radius_from_commit_sha(keyword, max_hops=1)

    # FIX: The "Who Touched This File?" Override
    # If the keyword is a technical artifact (file/module), force a file search 
    # even if the user asked "Who" (author_query).
    if "." in keyword or "/" in keyword or "_" in keyword:
        result = get_file_history_commits(keyword)
        if result:
            return result
        return get_blast_radius_commits(keyword, max_hops)
            
    if intent == "blast_radius":
        return get_blast_radius_commits(keyword, max_hops)

    if intent == "author_query":
        human_matches = get_commits_by_author(keyword)
        if human_matches:
            return human_matches
        # If no human matches, fall back to finding the file they asked about!
        return get_file_history_commits(keyword) or get_blast_radius_commits(keyword, max_hops)

    if intent in ("file_history", "module_origin"):
        result = get_file_history_commits(keyword)
        return result if result else get_blast_radius_commits(keyword, max_hops)

    # intent == "auto": infer from keyword shape
    if "." in keyword or "/" in keyword or "_" in keyword:
        result = get_file_history_commits(keyword)
        if result:
            return result
        return get_blast_radius_commits(keyword, max_hops)

    # Generic keyword — try all modes, return first non-empty
    for fn in (get_file_history_commits,
               lambda k: get_blast_radius_commits(k, max_hops),
               get_commits_by_author):
        result = fn(keyword)
        if result:
            return result

    return []

def fetch_summaries_by_commits(get_db, commit_shas):
    """Fetches full summaries from MySQL for specific SHAs."""
    if not commit_shas:
        return []
        
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        format_strings = ','.join(['%s'] * len(commit_shas))
        query = f"""
            SELECT delivery_id, event_type, repository_full_name, actor_login, commit_sha, pr_number,
                   source_url, summary_text, diff_text, files_json, created_at
            FROM github_summaries
            WHERE commit_sha IN ({format_strings})
            ORDER BY created_at DESC
        """
        cursor.execute(query, tuple(commit_shas))
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

# ==========================================
# PROCESSING WORKERS
# ==========================================

def _process_single_commit(get_db, repo_full_name, sha, actor_login, delivery_id):
    """Processes a single commit: fetches files, extracts deps, persists summary."""
    commit_payload, files = fetch_commit_files(repo_full_name, sha)
    compact_files = build_compact_diff(files)

    # Extract committed_at timestamp for graph edges
    committed_at = (
        (commit_payload.get("commit") or {})
        .get("author", {})
        .get("date", "")
    )

    modified_files_list = [item["filename"] for item in compact_files]
    dependencies = extract_file_dependencies(compact_files)
    logger.info("Graph Extraction - Found %d dependency edges.", len(dependencies))
    update_knowledge_graph(
        repo_full_name, sha, modified_files_list, dependencies,
        actor_login=actor_login,
        committed_at=committed_at,
    )

    raw_diff = render_diff_text(compact_files)
    meta = {
        "event_type": "push",
        "commit_message": (commit_payload.get("commit") or {}).get("message"),
    }
    summary_text = summarize_with_llm(
        repo_full_name, "push", actor_login, meta, compact_files, raw_diff
    )
    source_url = commit_payload.get("html_url") or ""

    summary_record = {
        "summary_text": summary_text,
        "diff_text": raw_diff,
        "files_json": json.dumps(compact_files, ensure_ascii=True, default=str),
    }

    event_payload = {
        "repository": {"full_name": repo_full_name, "html_url": source_url},
        "sender": {"login": actor_login},
        "after": sha,
    }
    commit_delivery = f"{delivery_id}-{sha[:8]}"
    persist_event(get_db, commit_delivery, "push", event_payload, summary_record)

    return {
        "commit_sha": sha,
        "summary_text": summary_text,
        "source_url": source_url,
    }


def process_github_event(get_db, payload, event_type, delivery_id):
    repository = payload.get("repository") or {}
    repo_full_name = repository.get("full_name")
    if not repo_full_name:
        raise ValueError("Missing repository.full_name in webhook payload")

    actor_login = (payload.get("sender") or {}).get("login")

    if event_type == "pull_request":
        pr = payload.get("pull_request") or {}
        pr_number = pr.get("number")
        if not pr_number:
            raise ValueError("Missing pull_request.number in webhook payload")
        files = fetch_pull_request_files(repo_full_name, pr_number)
        commit_sha = ((pr.get("head") or {}).get("sha")) or payload.get("after")
        source_url = pr.get("html_url") or repository.get("html_url")

        compact_files = build_compact_diff(files)

        modified_files_list = [item["filename"] for item in compact_files]
        dependencies = extract_file_dependencies(compact_files)
        logger.info(f"Graph Extraction - Found {len(dependencies)} dependency edges.")
        update_knowledge_graph(repo_full_name, commit_sha, modified_files_list, dependencies)

        raw_diff = render_diff_text(compact_files)
        meta = {
            "delivery_id": delivery_id,
            "event_type": event_type,
            "action": payload.get("action"),
            "head_commit": payload.get("head_commit", {}),
            "ref": payload.get("ref"),
        }
        summary_text = summarize_with_llm(
            repo_full_name, event_type, actor_login, meta, compact_files, raw_diff
        )
        summary_record = {
            "summary_text": summary_text,
            "diff_text": raw_diff,
            "files_json": json.dumps(compact_files, ensure_ascii=True, default=str),
        }
        persist_event(get_db, delivery_id, event_type, payload, summary_record)

        return {
            "repository_full_name": repo_full_name,
            "commit_sha": commit_sha,
            "pr_number": pr_number,
            "summary_text": summary_text,
            "source_url": source_url,
        }

    if event_type == "pull_request_review":
        review = payload.get("review") or {}
        pr = payload.get("pull_request") or {}
        pr_number = pr.get("number")
        commit_sha = review.get("commit_id") or ((pr.get("head") or {}).get("sha"))
        source_url = review.get("html_url") or pr.get("html_url") or repository.get("html_url")
        review_state = review.get("state", "")  # approved, changes_requested, commented
        review_body = review.get("body") or ""

        # Fetch the PR's files so the LLM has diff context for the review
        compact_files = []
        raw_diff = ""
        if pr_number:
            try:
                files = fetch_pull_request_files(repo_full_name, pr_number)
                compact_files = build_compact_diff(files)
                raw_diff = render_diff_text(compact_files)
                modified_files_list = [item["filename"] for item in compact_files]
                dependencies = extract_file_dependencies(compact_files)
                if commit_sha:
                    update_knowledge_graph(repo_full_name, commit_sha, modified_files_list, dependencies)    
            
            except Exception as e:
                logger.warning("Failed to fetch PR files for review context: %s", e)

        meta = {
            "delivery_id": delivery_id,
            "event_type": event_type,
            "action": payload.get("action"),
            "review_state": review_state,
            "review_body": _truncate(review_body, 2000),
            "pr_title": pr.get("title"),
            "pr_number": pr_number,
        }
        summary_text = summarize_with_llm(
            repo_full_name, event_type, actor_login, meta, compact_files, raw_diff
        )
        summary_record = {
            "summary_text": summary_text,
            "diff_text": raw_diff,
            "files_json": json.dumps(compact_files, ensure_ascii=True, default=str),
        }
        persist_event(get_db, delivery_id, event_type, payload, summary_record)

        return {
            "repository_full_name": repo_full_name,
            "commit_sha": commit_sha,
            "pr_number": pr_number,
            "summary_text": summary_text,
            "source_url": source_url,
        }

    if event_type == "pull_request_review_comment":
        comment = payload.get("comment") or {}
        pr = payload.get("pull_request") or {}
        pr_number = pr.get("number")
        commit_sha = comment.get("commit_id") or ((pr.get("head") or {}).get("sha"))
        source_url = comment.get("html_url") or pr.get("html_url") or repository.get("html_url")
        comment_body = comment.get("body") or ""
        comment_path = comment.get("path") or ""
        diff_hunk = comment.get("diff_hunk") or ""

        # Build a synthetic compact_files from the comment's diff hunk
        compact_files = []
        if comment_path and diff_hunk:
            compact_files = [{
                "filename": comment_path,
                "status": "commented",
                "additions": 0,
                "deletions": 0,
                "changes": 0,
                "patch": _truncate(diff_hunk, 4000),
            }]

        raw_diff = render_diff_text(compact_files)

        meta = {
            "delivery_id": delivery_id,
            "event_type": event_type,
            "action": payload.get("action"),
            "comment_body": _truncate(comment_body, 2000),
            "comment_path": comment_path,
            "pr_title": pr.get("title"),
            "pr_number": pr_number,
            "in_reply_to_id": comment.get("in_reply_to_id"),
        }
        summary_text = summarize_with_llm(
            repo_full_name, event_type, actor_login, meta, compact_files, raw_diff
        )
        summary_record = {
            "summary_text": summary_text,
            "diff_text": raw_diff,
            "files_json": json.dumps(compact_files, ensure_ascii=True, default=str),
        }
        persist_event(get_db, delivery_id, event_type, payload, summary_record)

        # Also update graph: the commented file is relevant to this commit
        if comment_path and commit_sha:
            with _graph_lock:
                G = _get_graph()
                file_node = f"file:{comment_path}"
                commit_node = f"commit:{commit_sha}"
                if not G.has_node(file_node):
                    G.add_node(file_node, type="file", repo=repo_full_name)
                if not G.has_node(commit_node):
                    G.add_node(commit_node, type="commit", repo=repo_full_name)
                G.add_edge(commit_node, file_node, relationship="REVIEWED")
                _save_graph_locked(G)

        return {
            "repository_full_name": repo_full_name,
            "commit_sha": commit_sha,
            "pr_number": pr_number,
            "summary_text": summary_text,
            "source_url": source_url,
        }

    # Push event — iterate over ALL commits in the push
    commits_in_push = payload.get("commits", [])
    results = []

    if commits_in_push:
        for c in commits_in_push:
            sha = c.get("id")
            if sha:
                try:
                    result = _process_single_commit(
                        get_db, repo_full_name, sha, actor_login, delivery_id
                    )
                    results.append(result)
                except Exception as e:
                    logger.warning("Error processing commit %s in push: %s", sha[:8], e)
    else:
        # Fallback: payload["after"] only (e.g. tag pushes, force-pushes with no commits array)
        commit_sha = payload.get("after")
        if not commit_sha:
            raise ValueError("Missing commit SHA in webhook payload")
        result = _process_single_commit(
            get_db, repo_full_name, commit_sha, actor_login, delivery_id
        )
        results.append(result)

    # Return info about the head commit
    head_result = results[-1] if results else {}
    return {
        "repository_full_name": repo_full_name,
        "commit_sha": head_result.get("commit_sha"),
        "pr_number": None,
        "summary_text": head_result.get("summary_text", ""),
        "source_url": head_result.get("source_url", ""),
        "commits_processed": len(results),
    }

def enqueue_github_event(get_db, payload, event_type, delivery_id):
    worker = threading.Thread(
        target=_run_github_event_worker,
        args=(get_db, payload, event_type, delivery_id),
        daemon=True,
    )
    worker.start()

def _run_github_event_worker(get_db, payload, event_type, delivery_id):
    try:
        process_github_event(get_db, payload, event_type, delivery_id)
        logger.info("Processed GitHub event %s (%s)", delivery_id, event_type)
    except mysql.connector.Error:
        logger.exception("Database error while processing GitHub event %s", delivery_id)
    except Exception as exc:
        logger.exception("GitHub event processing failed for %s: %s", delivery_id, exc)


def fetch_recent_summaries(get_db, repository_full_name=None, limit=10, keyword=None):
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        query = """
            SELECT delivery_id, event_type, repository_full_name, actor_login, commit_sha, pr_number,
                   source_url, summary_text, diff_text, files_json, created_at
            FROM github_summaries
        """
        clauses = []
        params = []

        if repository_full_name:
            clauses.append("repository_full_name = %s")
            params.append(repository_full_name)

        if keyword:
            keyword_like = f"%{keyword}%"
            clauses.append("(summary_text LIKE %s OR diff_text LIKE %s OR files_json LIKE %s OR commit_sha LIKE %s)")
            params.extend([keyword_like, keyword_like, keyword_like, keyword_like])

        if clauses:
            query += " WHERE " + " AND ".join(clauses)

        query += " ORDER BY created_at DESC LIMIT %s"
        params.append(limit)
        cursor.execute(query, tuple(params))
        return cursor.fetchall()
    finally:
        cursor.close()
        conn.close()

#answer from summaries upgraded

def answer_from_summaries(question, summaries):
    api_key = os.getenv("LLM_API_KEY")
    model = os.getenv("LLM_MODEL", "gemini-1.5-flash")

    # Top 3 results get full diff context; rest get summary only
    detailed = summaries[:3]
    brief = summaries[3:]

    condensed_context = "\n\n".join(
        [
            f"Repo: {item['repository_full_name']}\n"
            f"Author: {item.get('actor_login', 'Unknown')}\n"
            f"Date: {item.get('created_at', 'Unknown')}\n"
            f"Commit: {item.get('commit_sha')}\n"
            f"Summary: {item['summary_text']}\n"
            f"Files: {item.get('files_json')}\n"
            f"Diff:\n{str(item.get('diff_text', ''))[:3000]}"
            for item in detailed
        ] + [
            f"Repo: {item['repository_full_name']}\n"
            f"Author: {item.get('actor_login', 'Unknown')}\n"
            f"Date: {item.get('created_at', 'Unknown')}\n"
            f"Commit: {item.get('commit_sha', 'Unknown')}\n"
            f"Summary: {item['summary_text']}"
            for item in brief
        ]
    )

    if not api_key:
        return "API Key missing. Cannot generate answer."

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    
    payload = {
        "systemInstruction": {
            "parts": [{"text": "You are a senior engineering assistant powered by a Graph RAG architecture. Answer the user's question using the provided GitHub change summaries. If the user asks a broad question, explain that you ran a graph impact analysis on the most recent commit, and describe how the provided commits are structurally related through shared files or modules. Be direct, analytical, and conversational."}]
        },
        "contents": [{
            "parts": [{"text": f"Question: {question}\n\nRelevant change summaries:\n{condensed_context}"}]
        }],
        "generationConfig": {
            "temperature": 0.2,
            "maxOutputTokens": 4000
        }
    }

    try:
        # EXPONENTIAL BACKOFF RETRY LOOP
        for attempt in range(5):
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            
            if response.status_code == 429:
                sleep_time = 2 ** attempt  # Wait 1, 2, 4, 8, 16 seconds
                logger.warning("429 Rate limit hit in answer_from_summaries. Retrying in %ss...", sleep_time)
                time.sleep(sleep_time)
                continue
                
            response.raise_for_status()
            break  # Success, exit the retry loop
        else:
            # If the loop completes without breaking, we've exhausted our retries
            response.raise_for_status()

        data = response.json()
        
        # Safely extract text
        try:
            generated_text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            
            # Check if it was cut off by a limit
            finish_reason = data["candidates"][0].get("finishReason")
            if finish_reason != "STOP":
                generated_text += f"\n\n*(Note: Generation halted abruptly due to: {finish_reason})*"
                
            return generated_text
        except (KeyError, IndexError):
            return f"Error extracting text. Raw data: {data}"
        
    except Exception as exc:
        logger.exception("LLM chat answer failed: %s", exc)
        return f"API Error: {exc}"

def extract_search_term(question):
    """
    Extracts a technical keyword suitable for graph traversal.

    Priority order:
      1. Commit SHA  — most precise possible graph seed; returned as-is.
      2. Code artifact (contains '.', '_', or '/') — file/module name.
      3. None        — caller falls back to SQL.
    """
    # 1. Commit SHA — return it directly so get_commits_from_graph can
    #    do an exact commit-node lookup instead of a slow string scan.
    sha_match = _SHA_RE.search(question)
    if sha_match:
        sha = sha_match.group(0).lower()
        logger.info("Extracted commit SHA '%s' from: '%s'", sha, question)
        return sha

    words = re.findall(r"[A-Za-z0-9_./-]+", question.lower())

    stop_words = {
        "what", "when", "where", "why", "how", "did", "the", "and", "for", "from",
        "with", "this", "that", "on", "in", "to", "of", "a", "an", "is", "are",
        "were", "was", "we", "they", "our", "my", "me", "them",
        "commit", "commits", "repo", "repository", "please", "can", "you", "show",
        "give", "there", "any", "some", "last", "latest", "recent", "changed",
        "files", "code", "push", "branch", "main", "master", "get", "list",
    }

    filtered = [w for w in words if len(w) > 2 and w not in stop_words]

    # 2. Code artifact — file/module name with a separator character.
    for word in filtered:
        if "." in word or "_" in word or "/" in word:
            logger.info("Extracted technical artifact: '%s' from: '%s'", word, question)
            return word

    # 3. NEW: General Keyword Fallback
    # If no dots/underscores, just return the first highly relevant technical word
    if filtered:
        logger.info("Extracted general keyword: '%s' from: '%s'", filtered[0], question)
        return filtered[0]

    return None

    # For author queries, a short alphanumeric token is a valid keyword
    # (GitHub logins don't contain dots/underscores necessarily)
    # but we only do this when intent is already author_query — the
    # chatbot handles that by passing intent explicitly to get_commits_from_graph.
    return None

def build_summary_query_result(question, summaries):
    keyword = extract_search_term(question)
    if keyword:
        scored = []
        for item in summaries:
            haystack = " ".join(
                [
                    str(item.get("repository_full_name", "")),
                    str(item.get("event_type", "")),
                    str(item.get("commit_sha", "")),
                    str(item.get("summary_text", "")),
                    str(item.get("diff_text", "")),
                    str(item.get("files_json", "")),
                ]
            ).lower()
            score = haystack.count(keyword.lower())
            if score:
                scored.append((score, item))
        if scored:
            scored.sort(key=lambda pair: pair[0], reverse=True)
            return [item for _, item in scored]
    return summaries


# ==========================================
# BOOTSTRAP ENGINE (Gaps 1, 2, 3)
# ==========================================

def ensure_bootstrap_table(get_db):
    """Creates the github_bootstrap_status table if it doesn't exist."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS github_bootstrap_status (
                id INT AUTO_INCREMENT PRIMARY KEY,
                repository_full_name VARCHAR(255) UNIQUE NOT NULL,
                status ENUM('pending', 'in_progress', 'completed', 'failed') DEFAULT 'pending',
                commits_processed INT DEFAULT 0,
                files_scanned INT DEFAULT 0,
                error_detail TEXT,
                started_at TIMESTAMP NULL,
                completed_at TIMESTAMP NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def get_bootstrap_status(get_db, repo_full_name):
    """Returns the bootstrap status dict for a repo, or None if never started."""
    conn = get_db()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute(
            "SELECT * FROM github_bootstrap_status WHERE repository_full_name = %s",
            (repo_full_name,)
        )
        return cursor.fetchone()
    finally:
        cursor.close()
        conn.close()


def set_bootstrap_status(get_db, repo_full_name, status, detail="",
                         commits_processed=None, files_scanned=None):
    """Upserts the bootstrap status for a repo."""
    conn = get_db()
    cursor = conn.cursor()
    try:
        now = datetime.utcnow()
        started_at = now if status == "in_progress" else None
        completed_at = now if status in ("completed", "failed") else None

        cursor.execute(
            """
            INSERT INTO github_bootstrap_status
                (repository_full_name, status, error_detail, commits_processed,
                 files_scanned, started_at, completed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            ON DUPLICATE KEY UPDATE
                status = VALUES(status),
                error_detail = VALUES(error_detail),
                commits_processed = COALESCE(VALUES(commits_processed), commits_processed),
                files_scanned = COALESCE(VALUES(files_scanned), files_scanned),
                started_at = COALESCE(VALUES(started_at), started_at),
                completed_at = VALUES(completed_at)
            """,
            (repo_full_name, status, detail,
             commits_processed or 0, files_scanned or 0,
             started_at, completed_at),
        )
        conn.commit()
    finally:
        cursor.close()
        conn.close()


def _get_default_branch(repo_full_name):
    """Fetches the default branch name for a repo."""
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}"
    data = _fetch_json(url)
    return data.get("default_branch", "main")


# ------------------------------------------------------------------
# Gap 3: Repo-wide file tree awareness
# ------------------------------------------------------------------

def scan_repo_tree(repo_full_name):
    """Fetches the full file tree and adds file/directory nodes to the graph.

    Returns the list of source-file paths suitable for full-content scanning.
    """
    default_branch = _get_default_branch(repo_full_name)
    url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/git/trees/{default_branch}"
    data = _fetch_json(url, params={"recursive": "1"})

    tree = data.get("tree", [])
    if not tree:
        logger.warning("scan_repo_tree: empty tree for %s", repo_full_name)
        return []

    source_files = []
    directories_seen = set()

    # Hold the lock for the full loop: the tree scan is pure CPU with no I/O
    # or sleeps, so the lock is released quickly and webhook threads are not
    # meaningfully delayed.
    with _graph_lock:
        G = _get_graph()

        for item in tree:
            path = item.get("path", "")
            item_type = item.get("type")  # "blob" or "tree"

            if any(f"/{d}/" in f"/{path}/" for d in IGNORED_DIRECTORIES):
                continue

            if _is_noise_file(path):
                continue

            if item_type == "tree":
                # Directory node
                dir_node = f"directory:{path}"
                dir_basename = path.rsplit("/", 1)[-1].lower()
                is_utility = dir_basename in UTILITY_DIRS
                G.add_node(dir_node, type="directory", repo=repo_full_name,
                           utility=is_utility)
                directories_seen.add(path)

                # Link to parent directory
                if "/" in path:
                    parent_path = path.rsplit("/", 1)[0]
                    parent_node = f"directory:{parent_path}"
                    G.add_edge(parent_node, dir_node, relationship="CONTAINS")

            elif item_type == "blob":
                # File node
                file_node = f"file:{path}"
                name_no_ext = path.rsplit("/", 1)[-1].rsplit(".", 1)[0].lower()
                ext = ("." + path.rsplit(".", 1)[1].lower()) if "." in path else ""
                is_entry = name_no_ext in ENTRY_POINT_NAMES
                G.add_node(file_node, type="file", repo=repo_full_name,
                           entry_point=is_entry)

                # Link to parent directory
                if "/" in path:
                    parent_path = path.rsplit("/", 1)[0]
                    parent_node = f"directory:{parent_path}"
                    if parent_path not in directories_seen:
                        G.add_node(parent_node, type="directory",
                                   repo=repo_full_name)
                        directories_seen.add(parent_path)
                    G.add_edge(parent_node, file_node, relationship="CONTAINS")

                # Collect scannable source files
                if ext in SOURCE_EXTENSIONS:
                    source_files.append(path)

        _save_graph_locked(G)

    logger.info("scan_repo_tree complete for %s | %d files, %d dirs added",
                repo_full_name, len(source_files), len(directories_seen))
    return source_files


# ------------------------------------------------------------------
# Gap 2: Full-file dependency scan
# ------------------------------------------------------------------

def extract_imports_from_source(filepath, source_code):
    """Parses the full source code of a file for import/require statements.

    Unlike extract_file_dependencies() which only reads diff patches,
    this scans every line to catch pre-existing imports.
    """
    dependencies = []
    patterns = [
        r"^\s*from\s+([a-zA-Z0-9_.-]+)\s+import",
        r"^\s*import\s+([a-zA-Z0-9_.-]+)",
        r"from\s+['\"]([^'\"]+)['\"]",
        r"require\(['\"]([^'\"]+)['\"]\)",
        r"#include\s*[<\"]([^>\"]+)[>\"]",
        r"use\s+([a-zA-Z0-9_:]+)",
    ]

    for line in source_code.split("\n"):
        stripped = line.strip()
        # Skip comments and blank lines for speed
        if not stripped or stripped.startswith("#") and not stripped.startswith("#include"):
            continue

        for pattern in patterns:
            match = re.search(pattern, stripped)
            if match:
                target_module = match.group(1)
                edge = (filepath, "DEPENDS_ON", target_module)
                if edge not in dependencies:
                    dependencies.append(edge)
    return dependencies


# Flush accumulated mutations into the graph singleton every N files so that
# webhook threads can acquire _graph_lock between batches.
_GRAPH_FLUSH_BATCH = 50


def scan_file_contents(repo_full_name, file_paths):
    """Fetches raw content for each source file and scans for dependencies.

    Adds DEPENDS_ON edges to the knowledge graph for every import found.
    Mutations are accumulated locally and flushed into the singleton under
    _graph_lock every _GRAPH_FLUSH_BATCH files, so live webhook threads are
    not starved during a long bootstrap scan.
    Returns the total number of files successfully scanned.
    """
    if not file_paths:
        return 0

    scanned = 0
    batch_nodes: list = []  # (node_id, attr_dict) pairs to add
    batch_edges: list = []  # (src, dst, attr_dict) triples to add

    def _flush() -> None:
        """Merge the current batch into the graph singleton and persist to disk."""
        if not batch_nodes and not batch_edges:
            return
        with _graph_lock:
            G = _get_graph()
            G.add_nodes_from(batch_nodes)
            G.add_edges_from(batch_edges)
            _save_graph_locked(G)
        batch_nodes.clear()
        batch_edges.clear()

    for path in tqdm(file_paths, desc="Scanning file contents", unit="file"):
        try:
            url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/contents/{path}"
            data = _fetch_json(url)

            # GitHub returns base64-encoded content for files < 1MB
            content_b64 = data.get("content")
            if not content_b64:
                continue

            source_code = base64.b64decode(content_b64).decode("utf-8", errors="replace")
            deps = extract_imports_from_source(path, source_code)

            file_node = f"file:{path}"
            batch_nodes.append((file_node, {"type": "file", "repo": repo_full_name}))
            for source, rel, target in deps:
                target_node = f"module:{target}"
                batch_nodes.append((target_node, {"type": "module"}))
                batch_edges.append((file_node, target_node, {"relationship": rel}))

            scanned += 1

            if scanned % _GRAPH_FLUSH_BATCH == 0:
                _flush()

            # Respect rate limits: sleep between requests
            time.sleep(0.1)

        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                # Rate limit hit — back off
                logger.warning("Rate limit hit scanning %s, sleeping 60s", path)
                time.sleep(60)
            else:
                logger.warning("HTTP error scanning %s: %s", path, e)
        except Exception as e:
            logger.warning("Error scanning file %s: %s", path, e)

    _flush()  # flush any files that did not fill the last batch
    logger.info("scan_file_contents complete for %s | %d/%d files scanned",
                repo_full_name, scanned, len(file_paths))
    return scanned


# ------------------------------------------------------------------
# Gap 1: Historical commit backfill
# ------------------------------------------------------------------

def backfill_commits(get_db, repo_full_name, max_commits=None):
    """Paginates through the full commit history and processes each commit.

    For each commit: fetches files, extracts dependencies, updates the
    knowledge graph, and persists a summary record so the chatbot has
    historical data to query.

    Returns the number of commits processed.
    """
    if max_commits is None:
        max_commits = BOOTSTRAP_MAX_COMMITS

    processed = 0
    page = 1
    per_page = 100

    pbar = tqdm(total=max_commits, desc=f"Bootstrapping {repo_full_name}", unit="commit")

    while processed < max_commits:
        try:
            url = f"{GITHUB_API_BASE}/repos/{repo_full_name}/commits"
            params = {"per_page": per_page, "page": page}
            response = requests.get(url, headers=_github_headers(),
                                    params=params, timeout=30)
            response.raise_for_status()
            commits = response.json()

            if not commits:
                break

            for commit_data in commits:
                if processed >= max_commits:
                    break

                sha = commit_data.get("sha")
                if not sha:
                    continue

                try:
                    # Fetch full commit details with file diffs
                    commit_payload, files = fetch_commit_files(repo_full_name, sha)
                    compact_files = build_compact_diff(files)

                    # Extract author + timestamp before they are used below
                    commit_info = commit_data.get("commit", {})
                    actor_login = ((commit_data.get("author") or {}).get("login")
                                   or (commit_info.get("author") or {}).get("name")
                                   or "unknown")
                    commit_msg = commit_info.get("message", "")
                    commit_date = commit_info.get("author", {}).get("date", "")

                    mysql_date = commit_date.replace("T", " ").replace("Z", "") if commit_date else None

                    # Extract dependencies and update graph (with author + timestamp)
                    modified_files_list = [item["filename"] for item in compact_files]
                    dependencies = extract_file_dependencies(compact_files)
                    
                    # MAKE SURE ALL 4 POSITIONAL ARGUMENTS ARE HERE
                    update_knowledge_graph(
                        repo_full_name, 
                        sha, 
                        modified_files_list, 
                        dependencies,  # <--- This is what was missing!
                        actor_login=actor_login,
                        committed_at=commit_date,
                    )

                    # Build and persist summary

                    raw_diff = render_diff_text(compact_files)
                    meta = {
                        "event_type": "push",
                        "commit_message": commit_msg,
                        "backfill": True,
                    }

                    summary_text = heuristic_summary(
                        repo_full_name, "push", compact_files, meta
                    )

                    delivery_id = f"backfill-{sha[:12]}"
                    summary_record = {
                        "summary_text": summary_text,
                        "diff_text": raw_diff,
                        "files_json": json.dumps(
                            compact_files, ensure_ascii=True, default=str
                        ),
                    }

                    # Persist — uses ON DUPLICATE KEY so re-runs are safe
                    conn = get_db()
                    cursor = conn.cursor()
                    try:
                        cursor.execute(
                            """
                            INSERT INTO github_events
                            (delivery_id, event_type, repository_full_name,
                             actor_login, commit_sha, source_url, payload_json, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE delivery_id=delivery_id
                            """,
                            (
                                delivery_id, "push", repo_full_name,
                                actor_login, sha,
                                commit_data.get("html_url", ""),
                                json.dumps({"backfill": True, "sha": sha}, default=str),
                                mysql_date,
                            ),
                        )
                        cursor.execute(
                            """
                            INSERT INTO github_summaries
                            (delivery_id, event_type, repository_full_name,
                             actor_login, commit_sha, source_url,
                             summary_text, diff_text, files_json, created_at)
                            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            ON DUPLICATE KEY UPDATE delivery_id=delivery_id
                            """,
                            (
                                delivery_id, "push", repo_full_name,
                                actor_login, sha,
                                commit_data.get("html_url", ""),
                                summary_record["summary_text"],
                                summary_record.get("diff_text"),
                                summary_record.get("files_json"),
                                mysql_date,
                            ),
                        )
                        conn.commit()
                    finally:
                        cursor.close()
                        conn.close()

                    processed += 1
                    pbar.update(1)

                    if processed % 25 == 0:
                        logger.info(
                            "Backfill progress: %d/%d commits for %s",
                            processed, max_commits, repo_full_name
                        )
                        set_bootstrap_status(
                            get_db, repo_full_name, "in_progress",
                            detail=f"Backfilling commits: {processed}/{max_commits}",
                            commits_processed=processed,
                        )

                    # Respect rate limits
                    time.sleep(0.5)

                except Exception as e:
                    logger.warning(
                        "Error backfilling commit %s: %s", sha[:8], e
                    )
                    print(f"❌ CRASH ON {sha[:8]}: {e}")
                    continue

            # Check rate limit headers
            remaining = response.headers.get("X-RateLimit-Remaining")
            if remaining and int(remaining) < 10:
                reset_time = int(response.headers.get("X-RateLimit-Reset", 0))
                sleep_for = max(reset_time - int(time.time()), 60)
                logger.warning(
                    "Rate limit nearly exhausted (%s remaining), sleeping %ds",
                    remaining, sleep_for
                )
                time.sleep(sleep_for)

            if len(commits) < per_page:
                break  # Last page

            page += 1
            time.sleep(0.5)

        except requests.exceptions.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                logger.warning("Rate limit hit during backfill, sleeping 60s")
                time.sleep(60)
            else:
                logger.error("HTTP error during backfill: %s", e)
                break
        except Exception as e:
            logger.error("Unexpected error during backfill: %s", e)
            break

    pbar.close()
    logger.info(
        "backfill_commits complete for %s | %d commits processed",
        repo_full_name, processed
    )
    return processed


# ------------------------------------------------------------------
# Bootstrap coordinator
# ------------------------------------------------------------------

def bootstrap_repo(get_db, repo_full_name):
    """Runs the full bootstrap pipeline: tree scan → file scan → commit backfill.

    Updates status in the github_bootstrap_status table throughout.
    Skips if already completed (unless force=True via set_bootstrap_status reset).
    """
    existing = get_bootstrap_status(get_db, repo_full_name)
    if existing and existing.get("status") == "completed":
        logger.info("Bootstrap already completed for %s, skipping", repo_full_name)
        return

    logger.info("Starting bootstrap for %s", repo_full_name)
    set_bootstrap_status(get_db, repo_full_name, "in_progress")

    try:
        # Gap 3: Scan repo tree structure
        logger.info("[Bootstrap %s] Phase 1/3: Scanning file tree…", repo_full_name)
        source_files = scan_repo_tree(repo_full_name)

        # Gap 2: Full-file dependency scan
        logger.info(
            "[Bootstrap %s] Phase 2/3: Scanning %d source files for imports…",
            repo_full_name, len(source_files)
        )
        files_scanned = scan_file_contents(repo_full_name, source_files)
        set_bootstrap_status(
            get_db, repo_full_name, "in_progress",
            detail=f"File scan done: {files_scanned} files",
            files_scanned=files_scanned
        )

        # Gap 1: Historical commit backfill
        logger.info(
            "[Bootstrap %s] Phase 3/3: Backfilling commit history…",
            repo_full_name
        )
        commits_processed = backfill_commits(get_db, repo_full_name)

        set_bootstrap_status(
            get_db, repo_full_name, "completed",
            detail=f"Done: {files_scanned} files scanned, "
                   f"{commits_processed} commits backfilled",
            commits_processed=commits_processed,
            files_scanned=files_scanned,
        )
        logger.info(
            "Bootstrap COMPLETE for %s | %d files, %d commits",
            repo_full_name, files_scanned, commits_processed
        )

    except Exception as exc:
        error_msg = f"{type(exc).__name__}: {exc}"
        logger.exception("Bootstrap FAILED for %s: %s", repo_full_name, exc)
        set_bootstrap_status(
            get_db, repo_full_name, "failed", detail=error_msg
        )


def enqueue_bootstrap(get_db, repo_full_name):
    """Launches bootstrap_repo in a daemon thread (non-blocking).

    Same pattern as enqueue_github_event — fire-and-forget so the
    HTTP handler returns immediately.
    """
    worker = threading.Thread(
        target=bootstrap_repo,
        args=(get_db, repo_full_name),
        daemon=True,
        name=f"bootstrap-{repo_full_name}",
    )
    worker.start()
    logger.info("Bootstrap thread started for %s", repo_full_name)
    return worker
