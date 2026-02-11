import requests
import json
import time
from core.envelope import JobEnvelope

RELAY_URL = "http://localhost:5005"

class ATPClient:
    def __init__(self, agent_id):
        self.agent_id = agent_id

    def post_job(self, description, files, entrypoint, verification_regex, price_usdc):
        env = JobEnvelope(task_type="code_fix")
        env.set_payload(description, entrypoint, verification_regex)
        for path, content in files.items():
            env.add_file(path, content)
        env.set_terms(str(price_usdc), "USDC")
        
        job_data = env.envelope
        job_data['buyer_id'] = self.agent_id
        
        response = requests.post(f"{RELAY_URL}/jobs", json=job_data)
        if response.status_code == 201:
            print(f"[*] Job posted successfully: {response.json()['task_id']}")
            return response.json()['task_id']
        else:
            print(f"[!] Failed to post job: {response.text}")
            return None

    def list_open_jobs(self):
        response = requests.get(f"{RELAY_URL}/jobs")
        if response.status_code == 200:
            jobs = response.json()
            return [j for j in jobs if j['status'] == 'open']
        return []

    def claim_job(self, task_id):
        response = requests.post(f"{RELAY_URL}/jobs/{task_id}/claim", json={"agent_id": self.agent_id})
        return response.status_code == 200

    def submit_result(self, task_id, result_data):
        response = requests.post(f"{RELAY_URL}/jobs/{task_id}/submit", json={
            "agent_id": self.agent_id,
            "result": result_data
        })
        return response.status_code == 200

    def confirm_job(self, task_id):
        response = requests.post(f"{RELAY_URL}/jobs/{task_id}/confirm", json={"buyer_id": self.agent_id})
        return response.status_code == 200

    def get_job_status(self, task_id):
        response = requests.get(f"{RELAY_URL}/jobs/{task_id}")
        if response.status_code == 200:
            return response.json()
        return None

if __name__ == "__main__":
    # Demo Flow:
    client_buyer = ATPClient("human_proxy_v1")
    client_seller = ATPClient("expert_agent_v1")
    
    # 1. Buyer posts a task
    task_id = client_buyer.post_job(
        description="Fix the boot bug in OpenClaw for 8T",
        files={"config.yaml": "broken_config: true"},
        entrypoint="./verify_boot.sh",
        verification_regex="BOOT_SUCCESS",
        price_usdc=50.0
    )
    
    if task_id:
        # 2. Seller finds and claims
        open_jobs = client_seller.list_open_jobs()
        if any(j['task_id'] == task_id for j in open_jobs):
            if client_seller.claim_job(task_id):
                print(f"[*] Seller {client_seller.agent_id} claimed the job!")
                
                # 3. Seller submits result
                time.sleep(1) # Simulate work
                client_seller.submit_result(task_id, {"patch": "fixed_config: true"})
                print("[*] Result submitted. Awaiting Proxy verification...")
                
                # 4. Proxy (representing Human) performs "Verification & Confirmation"
                print("[*] Proxy: Running verification scripts...")
                time.sleep(1) # Simulate running Verifier
                is_valid = True # In reality, call Verifier here
                
                if is_valid:
                    if client_buyer.confirm_job(task_id):
                        print("[*] Proxy: Verification passed! Payment released.")
                
                # 5. Final status
                print(f"[*] Final Job Status: {client_buyer.get_job_status(task_id)['status']}")
