from core.verifier_base import BaseVerifier
import os
import json

class LLMJudgeVerifier(BaseVerifier):
    def __init__(self):
        self.api_key = os.getenv("GOOGLE_API_KEY")
        if self.api_key:
            import google.generativeai as genai
            genai.configure(api_key=self.api_key)

    def verify(self, job, submission, config=None):
        if not self.api_key:
            return 0, "Verifier API key missing"

        config = config or job.verification_config
        rubric = config.get('rubric', 'No rubric provided.')
        submission_content = submission.get('content', '') or submission.get('result_data', {}).get('content', '')
        
        prompt = f"""
        Rate this submission on a scale of 0-100 against the rubric.
        Respond JSON: {{ "score": int, "reason": "short explanation" }}
        
        Rubric: {rubric}
        Submission: {submission_content}
        """
        
        try:
            import google.generativeai as genai
            model = genai.GenerativeModel('gemini-1.5-pro')
            response = model.generate_content(prompt)
            data = json.loads(response.text.replace('```json','').replace('```',''))
            return float(data.get('score', 0)), data.get('reason', '')
        except Exception as e:
            return 0, str(e)
