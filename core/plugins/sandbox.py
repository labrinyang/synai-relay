import subprocess
import tempfile
import os

class SandboxVerifier:
    def verify(self, job, result_data):
        """
        Runs the submitted code in a Docker container.
        """
        code = result_data.get('code', '')
        test_command = job.verification_config.get('test_command', 'python3 main.py')
        
        # Create a temporary directory for the code
        with tempfile.TemporaryDirectory() as temp_dir:
            # Write the code to a file
            with open(os.path.join(temp_dir, 'main.py'), 'w') as f:
                f.write(code)
            
            # Simple Docker execution:
            # docker run --rm -v {temp_dir}:/app -w /app python:3.9-slim {test_command}
            try:
                cmd = [
                    "docker", "run", "--rm",
                    "-v", f"{temp_dir}:/app",
                    "-w", "/app",
                    "python:3.9-slim",
                    "sh", "-c", test_command
                ]
                
                result = subprocess.run(
                    cmd, 
                    capture_output=True, 
                    text=True, 
                    timeout=30
                )
                
                if result.returncode == 0:
                    return {"success": True, "reason": "Sandbox execution passed."}
                else:
                    return {
                        "success": False, 
                        "reason": f"Execution failed: {result.stderr or result.stdout}"
                    }
                    
            except subprocess.TimeoutExpired:
                 return {"success": False, "reason": "Execution timed out."}
            except Exception as e:
                return {"success": False, "reason": f"Sandbox Error: {str(e)}"}
