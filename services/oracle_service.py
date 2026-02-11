"""
6-step Oracle workflow orchestrator.
Step 1 (Guard) is handled by OracleGuard before this service is called.
This service handles Steps 2-6.
"""
import os
import json
import requests
from services.oracle_prompts import (
    STEP2_COMPREHENSION, STEP3_COMPLETENESS, STEP4_QUALITY,
    STEP5_DEVILS_ADVOCATE, STEP6_VERDICT,
    COMPLETENESS_WITH_RUBRIC, COMPLETENESS_WITHOUT_RUBRIC,
    build_rubric_section, build_rubric_items,
)


class OracleService:
    def __init__(self):
        self.base_url = os.environ.get('ORACLE_LLM_BASE_URL', 'https://openrouter.ai/api/v1')
        self.api_key = os.environ.get('ORACLE_LLM_API_KEY', '')
        self.model = os.environ.get('ORACLE_LLM_MODEL', 'openai/gpt-4o')
        self.pass_threshold = int(os.environ.get('ORACLE_PASS_THRESHOLD', '80'))

    def _call_llm(self, prompt: str, temperature: float = 0.1) -> dict:
        """Call LLM and parse JSON response."""
        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": temperature,
                "max_tokens": 1000,
            },
            timeout=60,
        )
        data = resp.json()
        content = data['choices'][0]['message']['content'].strip()
        # Extract JSON from response (handle markdown code blocks)
        if content.startswith('```'):
            content = content.split('\n', 1)[1].rsplit('```', 1)[0].strip()
        return json.loads(content)

    def evaluate(self, title: str, description: str, rubric: str, submission: str) -> dict:
        """
        Run Steps 2-6 of the oracle workflow.
        Returns {verdict, score, reason, steps[]}.
        """
        rubric_section = build_rubric_section(rubric)
        submission_str = json.dumps(submission, ensure_ascii=False) if isinstance(submission, dict) else str(submission)
        steps = []

        # Step 2: Comprehension
        prompt2 = STEP2_COMPREHENSION.format(
            title=title, description=description,
            rubric_section=rubric_section, submission=submission_str,
        )
        step2 = self._call_llm(prompt2, temperature=0.1)
        steps.append({"step": 2, "name": "comprehension", "output": step2})

        if step2.get('verdict') == 'CLEAR_FAIL':
            # Early exit — skip to Step 6
            prompt6 = STEP6_VERDICT.format(
                title=title, description=description, rubric_section=rubric_section,
                step2_output=json.dumps(step2),
                step3_output="SKIPPED (early exit from Step 2)",
                step4_output="SKIPPED",
                step5_output="SKIPPED",
                pass_threshold=self.pass_threshold,
            )
            step6 = self._call_llm(prompt6, temperature=0)
            steps.append({"step": 6, "name": "verdict", "output": step6})
            return self._build_result(step6, steps)

        # Step 3: Completeness
        if rubric:
            completeness_instructions = COMPLETENESS_WITH_RUBRIC.format(
                rubric_items=build_rubric_items(rubric)
            )
        else:
            completeness_instructions = COMPLETENESS_WITHOUT_RUBRIC

        prompt3 = STEP3_COMPLETENESS.format(
            title=title, description=description,
            rubric_section=rubric_section,
            step2_output=json.dumps(step2),
            submission=submission_str,
            completeness_instructions=completeness_instructions,
        )
        step3 = self._call_llm(prompt3, temperature=0.1)
        steps.append({"step": 3, "name": "completeness", "output": step3})

        # Step 4: Quality
        prompt4 = STEP4_QUALITY.format(
            title=title,
            step2_output=json.dumps(step2),
            step3_output=json.dumps(step3),
            submission=submission_str,
        )
        step4 = self._call_llm(prompt4, temperature=0.2)
        steps.append({"step": 4, "name": "quality", "output": step4})

        if step4.get('verdict') == 'CLEAR_PASS' and step4.get('score', 0) >= 95:
            # Early exit — skip to Step 6
            prompt6 = STEP6_VERDICT.format(
                title=title, description=description, rubric_section=rubric_section,
                step2_output=json.dumps(step2),
                step3_output=json.dumps(step3),
                step4_output=json.dumps(step4),
                step5_output="SKIPPED (early exit from Step 4 — CLEAR_PASS)",
                pass_threshold=self.pass_threshold,
            )
            step6 = self._call_llm(prompt6, temperature=0)
            steps.append({"step": 6, "name": "verdict", "output": step6})
            return self._build_result(step6, steps)

        # Step 5: Devil's Advocate
        prompt5 = STEP5_DEVILS_ADVOCATE.format(
            title=title, description=description,
            step2_output=json.dumps(step2),
            step3_output=json.dumps(step3),
            step4_output=json.dumps(step4),
            submission=submission_str,
        )
        step5 = self._call_llm(prompt5, temperature=0.2)
        steps.append({"step": 5, "name": "devils_advocate", "output": step5})

        # Step 6: Final Verdict
        prompt6 = STEP6_VERDICT.format(
            title=title, description=description, rubric_section=rubric_section,
            step2_output=json.dumps(step2),
            step3_output=json.dumps(step3),
            step4_output=json.dumps(step4),
            step5_output=json.dumps(step5),
            pass_threshold=self.pass_threshold,
        )
        step6 = self._call_llm(prompt6, temperature=0)
        steps.append({"step": 6, "name": "verdict", "output": step6})

        return self._build_result(step6, steps)

    def _build_result(self, verdict_step: dict, steps: list) -> dict:
        score = verdict_step.get('score', 0)
        return {
            "verdict": verdict_step.get('verdict', 'REJECTED'),
            "score": score,
            "passed": score >= self.pass_threshold,
            "reason": verdict_step.get('reason', ''),
            "steps": steps,
        }
