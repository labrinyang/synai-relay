import json
import base64
import os
import uuid

class JobEnvelope:
    def __init__(self, task_type="code_fix", protocol_version="0.1.0"):
        self.envelope = {
            "protocol_version": protocol_version,
            "task_id": str(uuid.uuid4()),
            "task_type": task_type,
            "payload": {
                "problem_description": "",
                "files": {},
                "entrypoint": "",
                "verification_regex": ""
            },
            "terms": {
                "price": "0",
                "currency": "USDC"
            }

        }

    def set_payload(self, description, entrypoint, verification_regex):
        self.envelope["payload"]["problem_description"] = description
        self.envelope["payload"]["entrypoint"] = entrypoint
        self.envelope["payload"]["verification_regex"] = verification_regex

    def add_file(self, file_path, content=None):
        if content is None and os.path.exists(file_path):
            with open(file_path, 'rb') as f:
                content = f.read()
        
        if isinstance(content, str):
            content = content.encode('utf-8')
            
        encoded_content = base64.b64encode(content).decode('utf-8')
        self.envelope["payload"]["files"][file_path] = encoded_content

    def set_terms(self, price, currency="ETH"):
        self.envelope["terms"]["price"] = price
        self.envelope["terms"]["currency"] = currency

    def to_json(self):
        return json.dumps(self.envelope, indent=2)

    @classmethod
    def from_json(cls, json_str):
        data = json.loads(json_str)
        instance = cls(data["task_type"], data["protocol_version"])
        instance.envelope = data
        return instance

    def extract_files(self, target_dir):
        os.makedirs(target_dir, exist_ok=True)
        for path, encoded_content in self.envelope["payload"]["files"].items():
            content = base64.b64decode(encoded_content)
            full_path = os.path.join(target_dir, path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, 'wb') as f:
                f.write(content)
        return list(self.envelope["payload"]["files"].keys())

if __name__ == "__main__":
    # Example usage
    env = JobEnvelope()
    env.set_payload("Fix auth bug", "pytest tests/", "passed")
    env.add_file("README.md", b"# Test Content")
    env.set_terms("0.05", "ETH")
    print(env.to_json())
