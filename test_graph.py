import sys
import os

# Added GRAPH_FILE to the imports
from github_monitor import load_graph, update_knowledge_graph, save_graph, GRAPH_FILE

def check_graph_file_path():
    """Verifies exactly where the script is reading/writing the graph."""
    print("\n📂 --- Checking Graph File Path ---")
    print(f"Graph file path resolved to: {GRAPH_FILE}")
    print(f"File exists at this path: {os.path.exists(GRAPH_FILE)}")
    
    if os.path.exists(GRAPH_FILE):
        print(f"File size: {os.path.getsize(GRAPH_FILE)} bytes")
    else:
        print("File size: N/A")
    print("-" * 45)

def check_commit_in_graph(sha):
    """Checks the current state of the graph for a specific commit SHA."""
    print(f"\n🔍 --- Checking Graph for SHA: {sha} ---")
    
    try:
        G = load_graph()
    except Exception as e:
        print(f"❌ Failed to load graph: {e}")
        return

    commit_node = f"commit:{sha}"
    short_matches = [n for n in G.nodes if str(n).startswith("commit:") and sha[:7] in str(n)]
    
    print(f"Total nodes in graph: {G.number_of_nodes()}")
    print(f"Total edges in graph: {G.number_of_edges()}")
    print(f"Exact node exists ('{commit_node}'): {G.has_node(commit_node)}")
    print(f"Partial matches (short SHA): {short_matches}")
    print("-" * 45)

def test_empty_dependencies_fix():
    """Tests if update_knowledge_graph creates a node when dependencies are empty."""
    print("\n🧪 --- Testing 'Empty Dependencies' Fix ---")
    test_sha = "test_fake_sha_9999999999"
    test_author = "test_bot"
    
    print("1. Calling update_knowledge_graph with dependencies=[]...")
    try:
        update_knowledge_graph(
            repo_full_name="test/dummy-repo",
            commit_sha=test_sha,
            modified_files=["dummy_test_file.py"],  # <--- Add this missing argument
            dependencies=[],  # The critical test: empty deps!
            actor_login=test_author,
            committed_at="2026-06-08T12:00:00Z"
        )
    except Exception as e:
        print(f"❌ Function crashed: {e}")
        return

    print("2. Reloading graph to verify save...")
    G = load_graph()
    commit_node = f"commit:{test_sha}"
    author_node = f"author:{test_author}"
    
    if G.has_node(commit_node):
        print("✅ SUCCESS: Commit node was added even with empty dependencies!")
        
        # Cleanup so we don't permanently pollute the user's graph
        print("3. Cleaning up test nodes...")
        G.remove_node(commit_node)
        if G.has_node(author_node) and G.degree(author_node) == 0:
            G.remove_node(author_node)
        
        save_graph(G)
        print("   Cleanup complete.")
    else:
        print("❌ FAILED: Commit node is missing. The save didn't persist to the file being loaded.")
    print("-" * 45)

if __name__ == "__main__":
    target_sha = "a5e1800507313421357735aaac222d6dd39902d9"
    
    if len(sys.argv) > 1:
        target_sha = sys.argv[1]
        
    check_graph_file_path()
    check_commit_in_graph(target_sha)
    test_empty_dependencies_fix()