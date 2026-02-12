"""
Live Oracle workflow test — calls the real LLM to verify the 8-step pipeline.
Run: python scripts/test_oracle_live.py
"""
import os
import sys
import json
import time

# Ensure project root is on path
project_root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
sys.path.insert(0, project_root)

# Load .env
env_path = os.path.join(project_root, '.env')
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                os.environ.setdefault(k.strip(), v.strip())

from services.oracle_service import OracleService

svc = OracleService()
print(f"Oracle config: model={svc.model}, threshold={svc.pass_threshold}")
print(f"Base URL: {svc.base_url}")
print()

# ── Test Cases ──────────────────────────────────────────────────────

CASES = [
    {
        "name": "GOOD submission (should PASS)",
        "title": "Write a Python function to check if a number is prime",
        "description": "Write a Python function `is_prime(n)` that returns True if n is a prime number, False otherwise. Handle edge cases (n <= 1, n == 2). Include docstring.",
        "rubric": "1. Function named is_prime\n2. Returns True for primes, False for non-primes\n3. Handles n <= 1 correctly\n4. Handles n == 2 correctly\n5. Has a docstring",
        "submission": '''def is_prime(n):
    """Check if a number is prime.

    Args:
        n: Integer to check.

    Returns:
        True if n is prime, False otherwise.
    """
    if n <= 1:
        return False
    if n == 2:
        return True
    if n % 2 == 0:
        return False
    for i in range(3, int(n**0.5) + 1, 2):
        if n % i == 0:
            return False
    return True''',
    },
    {
        "name": "MEDIOCRE submission (borderline)",
        "title": "Write a Python function to check if a number is prime",
        "description": "Write a Python function `is_prime(n)` that returns True if n is a prime number, False otherwise. Handle edge cases (n <= 1, n == 2). Include docstring.",
        "rubric": "1. Function named is_prime\n2. Returns True for primes, False for non-primes\n3. Handles n <= 1 correctly\n4. Handles n == 2 correctly\n5. Has a docstring",
        "submission": '''def is_prime(n):
    # check prime
    if n < 2:
        return False
    for i in range(2, n):
        if n % i == 0:
            return False
    return True''',
    },
    {
        "name": "GARBAGE submission (should FAIL)",
        "title": "Write a Python function to check if a number is prime",
        "description": "Write a Python function `is_prime(n)` that returns True if n is a prime number, False otherwise. Handle edge cases (n <= 1, n == 2). Include docstring.",
        "rubric": "1. Function named is_prime\n2. Returns True for primes, False for non-primes\n3. Handles n <= 1 correctly\n4. Handles n == 2 correctly\n5. Has a docstring",
        "submission": "I think prime numbers are cool. They are used in cryptography.",
    },
]

# ── Run ─────────────────────────────────────────────────────────────

for i, case in enumerate(CASES):
    print(f"{'='*70}")
    print(f"TEST {i+1}: {case['name']}")
    print(f"{'='*70}")

    t0 = time.time()
    try:
        result = svc.evaluate(
            title=case["title"],
            description=case["description"],
            rubric=case["rubric"],
            submission=case["submission"],
        )
        elapsed = time.time() - t0

        print(f"  Verdict:  {result['verdict']}")
        print(f"  Score:    {result['score']}")
        print(f"  Passed:   {result['passed']}")
        print(f"  Time:     {elapsed:.1f}s")
        print(f"  Steps:    {len(result['steps'])}")
        print(f"  Reason:   {result['reason'][:200]}...")
        print()

        # Show per-step summary
        for step in result['steps']:
            out = step['output']
            step_score = (
                out.get('relevance_confidence')
                or out.get('structural_score')
                or out.get('completeness_score')
                or out.get('quality_score')
                or out.get('consistency_score')
                or out.get('adjusted_score')
                or out.get('score')
                or '—'
            )
            print(f"    Step {step['step']:1d} ({step['name']:17s}): score={step_score}")

    except Exception as e:
        elapsed = time.time() - t0
        print(f"  ERROR after {elapsed:.1f}s: {e}")

    print()

print("Done.")
