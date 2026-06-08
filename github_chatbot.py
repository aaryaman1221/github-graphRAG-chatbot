"""
github_chatbot.py
-----------------
Standalone Streamlit chatbot for querying GitHub change summaries
across one or more repositories.
"""

import os
import streamlit as st
import logging
from tqdm import tqdm
import re
from app_config import get_db_config, load_environment

# Add this to force Python to print your INFO logs to the terminal
logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')

# st.set_page_config MUST be the first Streamlit call
st.set_page_config(
    page_title="GitHub Chatbot",
    layout="wide",
)

load_environment()

try:
    import mysql.connector
    from github_monitor import (
        answer_from_summaries,
        build_summary_query_result,
        ensure_github_tables,
        fetch_recent_summaries,
        extract_search_term,
        classify_query_intent,
        get_commits_from_graph,
        fetch_summaries_by_commits,
        get_bootstrap_status,
        enqueue_bootstrap,
        get_blast_radius_from_commit_sha,
        get_commits_by_author,
    )
    IMPORT_OK = True
except ImportError as _ie:
    IMPORT_OK = False
    IMPORT_ERROR = str(_ie)

# ---------------------------------------------------------------------------
# Custom CSS (Removed the broken chat bubbles, kept the repo pills)
# ---------------------------------------------------------------------------
st.markdown(
    """
<style>
.repo-pill {
    display: inline-block;
    background: #0d1117;
    color: #58a6ff;
    border: 1px solid #30363d;
    border-radius: 2em;
    padding: 2px 12px;
    font-size: 0.82rem;
    margin: 2px 2px;
}
</style>
""",
    unsafe_allow_html=True,
)

if not IMPORT_OK:
    st.error("Missing dependency")
    st.code(f"ImportError: {IMPORT_ERROR}")
    st.info("Make sure `github_monitor.py` and its dependencies are in the same directory.")
    st.stop()

# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
def _db_config():
    return get_db_config()

def get_db():
    return mysql.connector.connect(**_db_config())

def try_init_tables():
    try:
        ensure_github_tables(get_db)
        return True, None
    except Exception as exc:
        return False, str(exc)

def get_known_repositories():
    conn = None
    cursor = None
    try:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT DISTINCT repository_full_name "
            "FROM github_summaries ORDER BY repository_full_name"
        )
        rows = cursor.fetchall()
        return [r[0] for r in rows]
    except Exception:
        logging.warning("get_known_repositories: failed to fetch repositories", exc_info=True)
        return []
    finally:
        if cursor is not None:
            cursor.close()
        if conn is not None:
            conn.close()

# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
def _init_state():
    defaults = {
        "messages": [],
        "selected_repos": [],
        "result_limit": 20,
        "db_ok": None,
        "db_error": "",
        "known_repos": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()

# ---------------------------------------------------------------------------
# Header & Sidebar
# ---------------------------------------------------------------------------
st.title("GitHub Repository Chatbot")
st.caption("Ask questions about commits and pull requests across your repositories.")
st.divider()

with st.sidebar:
    st.header("⚙️ Settings")

    if st.session_state.db_ok is None:
        ok, err = try_init_tables()
        st.session_state.db_ok = ok
        st.session_state.db_error = err or ""

    if st.session_state.db_ok:
        st.success("Database connected")
    else:
        st.error("Database error")
        st.caption(st.session_state.db_error)
        st.info("Set DB_HOST / DB_PORT / DB_USER / DB_PASSWORD / DB_NAME and restart the app.")

    st.divider()
    st.subheader("Repositories")

    if st.session_state.known_repos is None:
        st.session_state.known_repos = get_known_repositories()

    known_repos = st.session_state.known_repos

    if st.button("Refresh list", use_container_width=True):
        st.session_state.known_repos = get_known_repositories()
        st.rerun()

    if not known_repos:
        st.info("No repositories found yet.\nWebhook events will appear here once received.")
        selected_repos = []
    else:
        all_selected = st.checkbox("All repositories", value=True, key="all_repos_cb")
        if all_selected:
            selected_repos = []
            for repo in known_repos:
                st.markdown(f'<span class="repo-pill">📦 {repo}</span>', unsafe_allow_html=True)
        else:
            selected_repos = st.multiselect(
                "Select repositories",
                options=known_repos,
                default=known_repos[:1],
                label_visibility="collapsed",
            )

    st.session_state.selected_repos = selected_repos

    # -------------------------------------------------------------------
    # Bootstrap Status Section
    # -------------------------------------------------------------------
    st.divider()
    st.subheader("🔧 Bootstrap Status")

    if known_repos:
        for repo in known_repos:
            try:
                bs = get_bootstrap_status(get_db, repo)
                if bs and bs.get("status") == "completed":
                    files_n = bs.get("files_scanned", 0)
                    commits_n = bs.get("commits_processed", 0)
                    st.markdown(
                        f"🟢 **{repo}** — Indexed "
                        f"({files_n} files, {commits_n} commits)"
                    )
                elif bs and bs.get("status") == "in_progress":
                    files_n = bs.get("files_scanned", 0)
                    commits_n = bs.get("commits_processed", 0)
                    st.markdown(
                        f"🟡 **{repo}** — Bootstrapping… "
                        f"({files_n} files, {commits_n} commits so far)"
                    )
                    st.progress(min(commits_n / 500, 1.0))
                elif bs and bs.get("status") == "failed":
                    st.markdown(f"🔴 **{repo}** — Bootstrap failed")
                    st.caption(bs.get("error_detail", "Unknown error"))
                    if st.button(f"Retry {repo}", key=f"retry_{repo}"):
                        enqueue_bootstrap(get_db, repo)
                        st.toast(f"Retrying bootstrap for {repo}…")
                        st.rerun()
                else:
                    st.markdown(f"⚪ **{repo}** — Not bootstrapped")
                    if st.button(f"Bootstrap {repo}", key=f"boot_{repo}"):
                        enqueue_bootstrap(get_db, repo)
                        st.toast(f"Bootstrap started for {repo}!")
                        st.rerun()
            except Exception:
                st.markdown(f"⚠️ **{repo}** — Status unavailable")
    else:
        st.caption("No repositories to bootstrap yet.")

    st.divider()
    st.subheader("Context Depth")
    st.session_state.result_limit = st.slider(
        "Max summaries fetched per answer",
        min_value=5,
        max_value=50,
        value=st.session_state.result_limit,
        step=5,
    )

    st.divider()
    if st.button("Clear chat history", use_container_width=True):
        st.session_state.messages = []
        st.rerun()

# ---------------------------------------------------------------------------
# Native Streamlit Chat Display
# ---------------------------------------------------------------------------
if not st.session_state.messages:
    st.info(
        "**Ask me anything about your repository changes**, e.g.\n\n"
        "- *What changed in the last push to main?*\n"
        "- *Were there any bug fixes this week?*\n"
        "- *Who modified the authentication code?*\n"
        "- *What commit added the react module?*"
    )
else:
    for msg in st.session_state.messages:
        # Convert our custom roles to standard streamlit roles
        role = "assistant" if msg["role"] == "bot" else "user"
        with st.chat_message(role):
            st.markdown(msg["content"])

# ---------------------------------------------------------------------------
# Native Streamlit Chat Input (Handles Graph RAG Logic safely)
# ---------------------------------------------------------------------------
if user_q := st.chat_input("e.g. What files changed in the last commit?"):
    if not st.session_state.db_ok:
        st.error("Cannot query — database is not connected. Check sidebar for details.")
        st.stop()

    # Show the user's message immediately
    st.session_state.messages.append({"role": "user", "content": user_q})
    with st.chat_message("user"):
        st.markdown(user_q)

    # Stream the assistant's response process
    with st.chat_message("assistant"):
        with st.spinner("Searching graph and generating answer…"):
            try:
                repos_to_query = st.session_state.selected_repos
                limit = st.session_state.result_limit
                
                intent = classify_query_intent(user_q)
                search_keyword = extract_search_term(user_q)
                all_summaries = []
                graph_used = False

                # ── Path 1: SHA query ──────────────────────────────────────────
                sha_match = re.search(r'\b[0-9a-f]{7,40}\b', user_q, re.IGNORECASE)
                if sha_match:
                    sha = sha_match.group(0)
                    targeted_commits = get_commits_from_graph(sha, max_hops=2, intent="blast_radius")
                    if targeted_commits:
                        all_summaries = fetch_summaries_by_commits(get_db, targeted_commits)
                        graph_used = True
                    # If SHA not in graph yet, fall back to direct DB lookup
                    if not all_summaries:
                        all_summaries = fetch_recent_summaries(get_db, limit=limit, keyword=sha)

                # ── Path 2: Keyword + intent (file, module, author) ────────────
                elif search_keyword and intent != "recent":
                    targeted_commits = get_commits_from_graph(
                        search_keyword, max_hops=2, intent=intent
                    )
                    if targeted_commits:
                        all_summaries = fetch_summaries_by_commits(get_db, targeted_commits)
                        graph_used = True

                # ── Path 3: Author query (no dot/slash keyword needed) ─────────
                # elif intent == "author_query":
                #     # extract_search_term fails for plain names — extract differently
                #     words = [w for w in user_q.lower().split() 
                #             if len(w) > 2 and w not in {"who", "what", "when", "did", "the"}]
                #     for word in words:
                #         targeted_commits = get_commits_by_author(word)
                #         if targeted_commits:
                #             all_summaries = fetch_summaries_by_commits(get_db, targeted_commits)
                #             graph_used = True
                #             break

                # ── Path 4: Recent/broad — go straight to SQL, skip fake graph ─
                if not all_summaries:
                    if repos_to_query:
                        for repo in repos_to_query:
                            all_summaries.extend(fetch_recent_summaries(get_db, repo, limit))
                    else:
                        all_summaries = fetch_recent_summaries(get_db, limit=limit)

                all_summaries = all_summaries[:limit]
                ranked = build_summary_query_result(user_q, all_summaries)
                answer = answer_from_summaries(user_q, ranked)

                if graph_used:
                    st.success(f"Graph traversal: `{intent}` → `{search_keyword}` → {len(all_summaries)} commits")
                else:
                    st.info("SQL fallback — no graph match for this query")   

                # 1. Knowledge Graph Check (only for structural queries)
                # if intent != "recent" and search_keyword:
                #     st.toast(f"🔍 Graph traversal: {intent} → '{search_keyword}'")
                #     targeted_commits = get_commits_from_graph(
                #         search_keyword,
                #         max_hops=2,
                #         intent=intent,
                #     )
                #     if targeted_commits:
                #         st.toast(f"🎯 Graph found {len(targeted_commits)} relevant commits!")
                #         all_summaries = fetch_summaries_by_commits(get_db, targeted_commits)
                
                # # 2. UPGRADE 1: REVERSE TRAVERSAL IMPACT ANALYSIS
                # if not all_summaries:
                #     st.toast("⚠️ Broad query. Triggering Graph Impact Analysis on latest push...")
                    
                #     # Grab the single most recent commit to act as our "Graph Seed"
                #     latest_record = fetch_recent_summaries(get_db, limit=1)
                    
                #     if latest_record and latest_record[0].get('commit_sha'):
                #         latest_sha = latest_record[0]['commit_sha']
                #         st.toast(f"🕸️ Tracing architectural impact from commit {latest_sha[:7]}...")
                        
                #         # Feed the commit SHA backward into the graph (Commit -> File -> Module -> Commits)
                #         targeted_commits = get_commits_from_graph(latest_sha, max_hops=2)
                        
                #         if targeted_commits:
                #             all_summaries = fetch_summaries_by_commits(get_db, targeted_commits)
                    
                #     # 3. Absolute last resort (e.g., brand new database with no graph data yet)
                #     if not all_summaries:
                #         if repos_to_query:
                #             for repo in repos_to_query:
                #                 all_summaries.extend(fetch_recent_summaries(get_db, repo, limit))
                #         else:
                #             all_summaries = fetch_recent_summaries(get_db, limit=limit)
                
            except Exception as exc:
                answer = f"⚠️ Error while fetching answer: {exc}"

        st.markdown(answer)
        st.session_state.messages.append({"role": "bot", "content": answer})
