import subprocess
import re
import os

class Verifier:
    def __init__(self, workspace_dir):
        self.workspace_dir = workspace_dir

    def verify(self, entrypoint, verification_regex):
        """
        Runs the entrypoint command and checks if output matches the regex.
        Returns (success: bool, output: str)
        """
        try:
            # Run the command in the specified workspace
            result = subprocess.run(
                entrypoint,
                shell=True,
                cwd=self.workspace_dir,
                capture_output=True,
                text=True,
                timeout=60 # Safety timeout
            )
            
            combined_output = result.stdout + "\n" + result.stderr
            
            # Check for regex match
            match = re.search(verification_regex, combined_output)
            
            if match:
                return True, combined_output
            else:
                return False, combined_output
                
        except Exception as e:
            return False, f"Verification failed with error: {str(e)}"

if __name__ == "__main__":
    # Test verifier
    os.makedirs("test_ws", exist_ok=True)
    with open("test_ws/test.py", "w") as f:
        f.write("print('Success!')")
    
    v = Verifier("test_ws")
    success, out = v.verify("python3 test.py", "Success")
    print(f"Success: {success}, Output: {out}")
