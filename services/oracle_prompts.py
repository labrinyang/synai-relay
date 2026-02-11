"""
Prompt templates for the 6-step Oracle workflow.
All submissions are wrapped in <SUBMISSION> delimiters.
"""

STEP2_COMPREHENSION = """You are evaluating a task submission. Analyze whether the submission addresses the task.

## Task
Title: {title}
Description:
{description}

{rubric_section}

## Submission
<SUBMISSION>
{submission}
</SUBMISSION>

## Instructions
1. Does the submission address what the task is asking for?
2. Is it a genuine attempt (not empty, off-topic, or placeholder)?
3. If a rubric is provided, does it relate to the rubric items?

Respond with exactly one JSON object:
{{"addresses_task": true/false, "analysis": "brief explanation", "verdict": "CLEAR_FAIL" or "CONTINUE"}}

Use CLEAR_FAIL only if the submission clearly does not address the task at all."""

STEP3_COMPLETENESS = """You are checking the completeness of a task submission.

## Task
Title: {title}
Description:
{description}

{rubric_section}

## Previous Analysis (Step 2 — Comprehension)
{step2_output}

## Submission
<SUBMISSION>
{submission}
</SUBMISSION>

## Instructions
{completeness_instructions}

Respond with exactly one JSON object:
{{"items_checked": [...], "gaps": [...], "completeness_score": 0-100}}"""

COMPLETENESS_WITH_RUBRIC = """Check each rubric item explicitly:
{rubric_items}

For each item, state whether it is MET or NOT MET with brief reasoning."""

COMPLETENESS_WITHOUT_RUBRIC = """Infer the key requirements from the task description and check if each is addressed.
List what you consider the requirements and whether each is met."""

STEP4_QUALITY = """You are assessing the quality of a task submission.

## Task
Title: {title}
Description:
{description}

## Previous Analysis
Step 2 (Comprehension): {step2_output}
Step 3 (Completeness): {step3_output}

## Submission
<SUBMISSION>
{submission}
</SUBMISSION>

## Instructions
1. Rate the overall quality from 0 to 100.
2. List strengths and weaknesses.
3. If score >= 95 and no significant weaknesses, set verdict to CLEAR_PASS.
4. Otherwise set verdict to CONTINUE.

Respond with exactly one JSON object:
{{"score": 0-100, "strengths": [...], "weaknesses": [...], "verdict": "CLEAR_PASS" or "CONTINUE"}}"""

STEP5_DEVILS_ADVOCATE = """You are playing Devil's Advocate. Your job is to argue AGAINST accepting this submission.

## Task
Title: {title}
Description:
{description}

## Previous Analysis
Step 2: {step2_output}
Step 3: {step3_output}
Step 4: {step4_output}

## Submission
<SUBMISSION>
{submission}
</SUBMISSION>

## Instructions
Find every possible reason this submission should NOT be accepted:
- Subtle errors or inaccuracies
- Missing edge cases
- Quality issues
- Anything the previous steps might have missed

Be thorough but fair. Do not fabricate issues that don't exist.

Respond with exactly one JSON object:
{{"arguments_against": [...], "severity": "none" or "minor" or "major", "summary": "brief summary"}}"""

STEP6_VERDICT = """You are the final judge. Synthesize all previous analysis to make a verdict.

## Task
Title: {title}
Description:
{description}

{rubric_section}

## Analysis Chain
Step 2 (Comprehension): {step2_output}
Step 3 (Completeness): {step3_output}
Step 4 (Quality): {step4_output}
Step 5 (Devil's Advocate): {step5_output}

## Instructions
Weigh all evidence. The Devil's Advocate step intentionally looks for problems — consider whether those problems are genuine or nitpicks.

Pass threshold: score >= {pass_threshold}

Respond with exactly one JSON object:
{{"verdict": "RESOLVED" or "REJECTED", "score": 0-100, "reason": "detailed explanation"}}"""


def build_rubric_section(rubric: str) -> str:
    if rubric:
        return f"## Rubric (Evaluation Criteria)\n{rubric}"
    return "## Rubric\nNo rubric provided. Infer requirements from the task description."


def build_rubric_items(rubric: str) -> str:
    if not rubric:
        return ""
    lines = [line.strip() for line in rubric.strip().split('\n') if line.strip()]
    items = []
    for i, line in enumerate(lines, 1):
        items.append(f"  {i}. {line}")
    return "\n".join(items)
