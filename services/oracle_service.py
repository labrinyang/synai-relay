"""
8-step Oracle workflow orchestrator (Steps 2-9).
Step 1 (Guard) is handled by OracleGuard before this service is called.
"""
import os
import json
import requests
from services.oracle_prompts import (
    STEP2_COMPREHENSION, STEP3_STRUCTURAL, STEP4_COMPLETENESS,
    STEP5_QUALITY, STEP6_CONSISTENCY, STEP7_DEVILS_ADVOCATE,
    STEP8_PENALTY, STEP9_VERDICT,
    COMPLETENESS_WITH_RUBRIC, COMPLETENESS_WITHOUT_RUBRIC,
    build_rubric_section, build_rubric_items,
)


class OracleService:
    RETRIABLE_STATUS_CODES = {429, 500, 502, 503, 504}

    def __init__(self):
        self.base_url = os.environ.get('ORACLE_LLM_BASE_URL', 'https://openrouter.ai/api/v1')
        self.api_key = os.environ.get('ORACLE_LLM_API_KEY', '')
        self.model = os.environ.get('ORACLE_LLM_MODEL', 'openai/gpt-4o')
        self.pass_threshold = int(os.environ.get('ORACLE_PASS_THRESHOLD', '80'))

    def _call_llm(self, prompt: str, temperature: float = 0.1, max_tokens: int = 1000) -> dict:
        """Call LLM and parse JSON response. Retries on transient errors."""
        import time

        max_retries = 3
        last_error = None

        for attempt in range(max_retries):
            try:
                resp = requests.post(
                    f"{self.base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self.model,
                        "messages": [{"role": "user", "content": prompt}],
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
                    timeout=60,
                )

                if resp.status_code in self.RETRIABLE_STATUS_CODES:
                    last_error = RuntimeError(
                        f"LLM API transient error: {resp.status_code}"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(2 ** attempt)
                        continue
                    raise last_error

                if not resp.ok:
                    raise RuntimeError(
                        f"LLM API error: {resp.status_code} {resp.text[:200]}"
                    )

                data = resp.json()
                content = data['choices'][0]['message']['content'].strip()

                if content.startswith('```'):
                    content = content.split('\n', 1)[1].rsplit('```', 1)[0].strip()

                try:
                    return json.loads(content)
                except json.JSONDecodeError as e:
                    last_error = RuntimeError(
                        f"LLM returned invalid JSON (attempt {attempt + 1}): {e}"
                    )
                    if attempt < max_retries - 1:
                        time.sleep(1)
                        continue
                    raise last_error

            except requests.exceptions.Timeout:
                last_error = RuntimeError("LLM API timeout")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue

            except requests.exceptions.ConnectionError as e:
                last_error = RuntimeError(f"LLM API connection error: {e}")
                if attempt < max_retries - 1:
                    time.sleep(2 ** attempt)
                    continue

        raise last_error

    def evaluate(self, title: str, description: str, rubric: str, submission: str) -> dict:
        """
        Run Steps 2-9 of the oracle workflow.
        Returns {verdict, score, reason, steps[]}.
        """
        rubric_section = build_rubric_section(rubric)
        submission_str = json.dumps(submission, ensure_ascii=False) if isinstance(submission, dict) else str(submission)
        # H4: Escape SUBMISSION delimiters in content to prevent delimiter injection
        submission_str = submission_str.replace('</SUBMISSION>', '&lt;/SUBMISSION&gt;').replace('<SUBMISSION>', '&lt;SUBMISSION&gt;')
        steps = []

        # ── Step 2: Comprehension & Relevance ───────────────────────
        prompt2 = STEP2_COMPREHENSION.format(
            title=title, description=description,
            rubric_section=rubric_section, submission=submission_str,
        )
        step2 = self._call_llm(prompt2, temperature=0.1, max_tokens=1500)
        steps.append({"step": 2, "name": "comprehension", "output": step2})

        if step2.get('verdict') == 'CLEAR_FAIL':
            # Early exit — skip to Step 9
            prompt9 = STEP9_VERDICT.format(
                title=title, description=description, rubric_section=rubric_section,
                step2_output=json.dumps(step2),
                step3_output="SKIPPED (early exit from Step 2)",
                step4_output="SKIPPED",
                step5_output="SKIPPED",
                step6_output="SKIPPED",
                step7_output="SKIPPED",
                step8_output="SKIPPED",
                adjusted_score=0,
                pass_threshold=self.pass_threshold,
            )
            step9 = self._call_llm(prompt9, temperature=0, max_tokens=1500)
            steps.append({"step": 9, "name": "verdict", "output": step9})
            return self._build_result(step9, steps)

        # ── Step 3: Structural Integrity (NEW) ──────────────────────
        prompt3 = STEP3_STRUCTURAL.format(
            title=title, description=description,
            step2_output=json.dumps(step2),
            submission=submission_str,
        )
        step3 = self._call_llm(prompt3, temperature=0.1, max_tokens=1200)
        steps.append({"step": 3, "name": "structural", "output": step3})

        # ── Step 4: Completeness & Coverage ─────────────────────────
        if rubric:
            completeness_instructions = COMPLETENESS_WITH_RUBRIC.format(
                rubric_items=build_rubric_items(rubric)
            )
        else:
            completeness_instructions = COMPLETENESS_WITHOUT_RUBRIC

        prompt4 = STEP4_COMPLETENESS.format(
            title=title, description=description,
            rubric_section=rubric_section,
            step2_output=json.dumps(step2),
            step3_output=json.dumps(step3),
            submission=submission_str,
            completeness_instructions=completeness_instructions,
        )
        step4 = self._call_llm(prompt4, temperature=0.1, max_tokens=2000)
        steps.append({"step": 4, "name": "completeness", "output": step4})

        # ── Step 5: Depth & Quality ─────────────────────────────────
        prompt5 = STEP5_QUALITY.format(
            title=title, description=description,
            step2_output=json.dumps(step2),
            step3_output=json.dumps(step3),
            step4_output=json.dumps(step4),
            submission=submission_str,
        )
        step5 = self._call_llm(prompt5, temperature=0.15, max_tokens=2000)
        steps.append({"step": 5, "name": "quality", "output": step5})

        # ── Step 6: Consistency Audit (NEW) ─────────────────────────
        prompt6 = STEP6_CONSISTENCY.format(
            title=title, description=description,
            step4_output=json.dumps(step4),
            step5_output=json.dumps(step5),
            submission=submission_str,
        )
        step6 = self._call_llm(prompt6, temperature=0.1, max_tokens=1500)
        steps.append({"step": 6, "name": "consistency", "output": step6})

        # ── Step 7: Devil's Advocate ────────────────────────────────
        prompt7 = STEP7_DEVILS_ADVOCATE.format(
            title=title, description=description,
            step2_output=json.dumps(step2),
            step3_output=json.dumps(step3),
            step4_output=json.dumps(step4),
            step5_output=json.dumps(step5),
            step6_output=json.dumps(step6),
            submission=submission_str,
        )
        step7 = self._call_llm(prompt7, temperature=0.2, max_tokens=2000)
        steps.append({"step": 7, "name": "devils_advocate", "output": step7})

        # ── Step 8: Penalty Calculator (NEW) ────────────────────────
        structural_score = step3.get('structural_score', 50)
        completeness_score = step4.get('completeness_score', 50)
        quality_score = step5.get('quality_score', 50)
        consistency_score = step6.get('consistency_score', 50)

        prompt8 = STEP8_PENALTY.format(
            structural_score=structural_score,
            completeness_score=completeness_score,
            quality_score=quality_score,
            consistency_score=consistency_score,
            step7_output=json.dumps(step7),
        )
        step8 = self._call_llm(prompt8, temperature=0, max_tokens=1000)
        steps.append({"step": 8, "name": "penalty", "output": step8})

        # ── Step 9: Final Verdict ───────────────────────────────────
        adjusted_score = step8.get('adjusted_score', 0)

        prompt9 = STEP9_VERDICT.format(
            title=title, description=description, rubric_section=rubric_section,
            step2_output=json.dumps(step2),
            step3_output=json.dumps(step3),
            step4_output=json.dumps(step4),
            step5_output=json.dumps(step5),
            step6_output=json.dumps(step6),
            step7_output=json.dumps(step7),
            step8_output=json.dumps(step8),
            adjusted_score=adjusted_score,
            pass_threshold=self.pass_threshold,
        )
        step9 = self._call_llm(prompt9, temperature=0, max_tokens=1500)
        steps.append({"step": 9, "name": "verdict", "output": step9})

        return self._build_result(step9, steps)

    def _build_result(self, verdict_step: dict, steps: list) -> dict:
        score = verdict_step.get('score', 0)
        passed = score >= self.pass_threshold
        # H8: Override verdict based on score to prevent LLM inconsistency
        verdict = 'RESOLVED' if passed else 'REJECTED'
        return {
            "verdict": verdict,
            "score": score,
            "passed": passed,
            "reason": verdict_step.get('reason', ''),
            "steps": steps,
        }
