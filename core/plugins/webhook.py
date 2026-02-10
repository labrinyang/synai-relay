import secrets

class WebhookVerifier:
    def verify(self, job, result_data):
        """
        For Webhooks, the initial 'verification' is just a status check.
        Real verification happens when the callback endpoint is hit.
        """
        # If this is called from the submission flow, we just return Pending
        # unless the result_data contains the special 'callback_payload' from the API
        if result_data.get('source') == 'webhook_callback':
             expected = job.verification_config.get('expected_payload', {})
             received = result_data.get('payload', {})
             
             # Simple equality check for prototype
             # In production, use JSONSchema or flexible matching
             if expected == received:
                 return {"success": True, "reason": "Webhook payload matched expected data."}
             else:
                 return {"success": False, "reason": f"Payload mismatch. Expected {expected}, got {received}"}

        return {"success": False, "reason": "Waiting for external webhook event."}

    @staticmethod
    def generate_token():
        return secrets.token_urlsafe(16)
