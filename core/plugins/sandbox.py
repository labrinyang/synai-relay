from core.verifier_base import BaseVerifier
import subprocess
import tempfile
import os

class SandboxVerifier(BaseVerifier):
    def verify(self, job, submission, config=None):
        config = config or job.verification_config
        
        code = submission.get('content', '')
        # Security: In production, check for malicious imports here
        
        test_command = config.get('test_command', 'python3 main.py')
        
        with tempfile.TemporaryDirectory() as temp_dir:
            with open(os.path.join(temp_dir, 'main.py'), 'w') as f:
                f.write(code)
            
            try:
                cmd = [
                    "docker", "run", "--rm",
                    "--network", "none", # Isolating network
                    "-v", f"{temp_dir}:/app",
                    "-w", "/app",
                    "python:3.9-slim",
                    "sh", "-c", test_command
                ]
                
                result = subprocess.run(
                    cmd, 
                    capture_output=True, 
                    text=True, 
                    timeout=30 # Timeout -> Slash
                )
                
                if result.returncode == 0:
                    return 100.0, "Sandbox execution passed."
                else:
                    return 0.0, f"Execution failed (Exit {result.returncode}): {result.stderr or result.stdout}"

            except subprocess.TimeoutExpired:
                 return 0.0, "Execution TIMEOUT (Start Slash Procedure)"
            except Exception as e:
                return 0.0, f"Sandbox Error: {str(e)}"
