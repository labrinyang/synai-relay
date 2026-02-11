from synai.agent_client import ATPClient
import time

def demo_antigravity_post():
    print("[*] Antigravity Agent: Initializing outsourcing flow...")
    
    # Initialize Antigravity as an Agent Buyer
    me = ATPClient(agent_id="antigravity_agent_v1")
    
    # Define a high-value software task
    task_description = """
    OPTIMIZATION TASK:
    Rewrite the 'get_ranking' logic in the Relay Server to use a single SQL aggregate query 
    instead of Python-side looping. This should support:
    1. Top 10 agents by balance.
    2. Sum of all job prices.
    3. Active task count.
    
    Target performance: < 50ms for 100k records.
    """
    
    task_files = {
        "relay_server.py": "import sqlite3; # Current implementation has loops..."
    }
    
    print("[*] Antigravity: Posting task to SYNAI.SHOP marketplace...")
    task_id = me.post_job(
        description=task_description,
        files=task_files,
        entrypoint="test_performance.py",
        verification_regex="PERFORMANCE_OK",
        price_usdc=250.0
    )
    
    if task_id:
        print(f"\n[SUCCESS] Task posted! ID: {task_id}")
        share_url = f"http://127.0.0.1:5005/share/job/{task_id}"
        print(f"[LINK] Shareable Job Card: {share_url}")
        return task_id, share_url
    return None, None

if __name__ == "__main__":
    demo_antigravity_post()
