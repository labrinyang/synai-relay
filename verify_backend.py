import requests
import time
import json

RELAY_URL = "http://127.0.0.1:5005"

def test_flow():
    print("[*] Starting ATP Backend Verification...")
    
    # 1. Create a Job
    job_data = {
        "title": "Kernel Patch for OnePlus 8T",
        "buyer_id": "proxy_human_01",
        "terms": {"price": 1000},
        "envelope_json": {"entry": "patch.sh"}
    }
    resp = requests.post(f"{RELAY_URL}/jobs", json=job_data)
    task_id = resp.json()['task_id']
    print(f"[+] Job posted: {task_id}")



    # 2. Fund the Job (Simulate Escrow Deposit)
    requests.post(f"{RELAY_URL}/jobs/{task_id}/fund", json={
        "escrow_tx_hash": "0xabc123_dummy_tx_hash"
    })
    print("[+] Job funded with mock transaction")

    # 3. Claim Job
    requests.post(f"{RELAY_URL}/jobs/{task_id}/claim", json={"agent_id": "cyber_ninja_01"})
    print("[+] Job claimed by cyber_ninja_01")


    # 3. Submit Result
    requests.post(f"{RELAY_URL}/jobs/{task_id}/submit", json={"agent_id": "cyber_ninja_01", "result": {"diff": "..."}})
    print("[+] Result submitted")

    # 5. Confirm Job (Settlement with Signature)
    confirm_data = {
        "buyer_id": "proxy_human_01",
        "signature": "sig_confirmed_by_buyer_0x789"
    }
    requests.post(f"{RELAY_URL}/jobs/{task_id}/confirm", json=confirm_data)
    print("[+] Job confirmed with signature and settled")


    # 6. Check Ranking
    ranking = requests.get(f"{RELAY_URL}/ledger/ranking").json()
    print(f"[+] Current Stats: {ranking['stats']}")

    print(f"[+] Top Agent: {ranking['agent_ranking'][0]['agent_id']} - Balance: {ranking['agent_ranking'][0]['balance']}")

    # 6. Test Adoption
    adopt_data = {
        "agent_id": "cyber_ninja_01",
        "twitter_handle": "alice_builds",
        "tweet_url": "https://twitter.com/alice_builds/status/123456"
    }
    requests.post(f"{RELAY_URL}/agents/adopt", json=adopt_data)
    print("[+] Agent cyber_ninja_01 adopted by @alice_builds")

    # 7. Re-check Ranking (Owner should now appear)
    ranking_v2 = requests.get(f"{RELAY_URL}/ledger/ranking").json()
    print(f"[+] Top Agent Owner: {ranking_v2['agent_ranking'][0]['owner_id']}")

if __name__ == "__main__":
    try:
        test_flow()
    except Exception as e:
        print(f"[!] Test failed: {e}")
