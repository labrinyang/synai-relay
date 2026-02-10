import google.generativeai as genai
import os
import json

class LLMJudgeVerifier:
    def __init__(self):
        self.api_key = os.getenv("GOOGLE_API_KEY")
        if self.api_key:
            genai.configure(api_key=self.api_key)

    def verify(self, job, result_data):
        """
        Uses an LLM to judge the submission against the rubric.
        """
        if not self.api_key:
            return {"success": False, "reason": "Verifier API key missing"}

        rubric = job.verification_config.get('rubric', 'No rubric provided.')
        submission_content = result_data.get('content', '')
        
        prompt = f"""
        You are an impartial judge for a crowdsourced task platform.
        
        Task Title: {job.title}
        Task Description: {job.description}
        
        Acceptance Rubric:
        {rubric}
        
        Worker Submission:
        {submission_content}
        
        Evaluate the submission strictly against the rubric.
        Respond ONLY with a JSON object in this format:
        {{
            "success": boolean,
            "reason": "concise explanation of pass/fail decision",
            "score": integer (0-100)
        }}
        """
        
        try:
            model = genai.GenerativeModel('gemini-1.5-pro')
            response = model.generate_content(prompt)
            # Cleanup Markdown code blocks if present
            text = response.text.replace('```json', '').replace('```', '').strip()
            evaluation = json.loads(text)
            
            return {
                "success": evaluation.get('success', False),
                "reason": evaluation.get('reason', "No reason provided"),
                "score": evaluation.get('score', 0)
            }
        except Exception as e:
            return {"success": False, "reason": f"LLM Judge Error: {str(e)}"}
