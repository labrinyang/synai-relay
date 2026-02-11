from core.verifier_base import BaseVerifier
import secrets

class WebhookVerifier(BaseVerifier):
    def verify(self, job, submission, config=None):
        config = config or job.verification_config
        
        # Check if persistent data contains the payload
        received = submission.get('webhook_payload')
        
        if received:
            expected = config.get('expected_payload', {})
            # Simple match
            if expected == received:
                return 100.0, "Webhook payload matched exactly."
            else:
                return 0.0, f"Mismatch. Exp: {expected}, Got: {received}"
        
        return 0.0, "Pending external callback."
        
    @staticmethod
    def generate_token():
        return secrets.token_urlsafe(16)
