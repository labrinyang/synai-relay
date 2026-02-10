from core.verifier_base import BaseVerifier

class VerifierFactory:
    _plugins = {}

    @staticmethod
    def register_plugin(verifier_type, plugin_class):
        VerifierFactory._plugins[verifier_type] = plugin_class

    @staticmethod
    def get_verifier(verifier_type) -> BaseVerifier:
        if not VerifierFactory._plugins:
            VerifierFactory._load_default_plugins()
        plugin_class = VerifierFactory._plugins.get(verifier_type)
        if not plugin_class:
            raise ValueError(f"Verifier type '{verifier_type}' not supported.")
        return plugin_class()

    @staticmethod
    def _load_default_plugins():
        from core.plugins.llm_judge import LLMJudgeVerifier
        from core.plugins.webhook import WebhookVerifier
        from core.plugins.sandbox import SandboxVerifier
        
        VerifierFactory.register_plugin('llm_judge', LLMJudgeVerifier)
        VerifierFactory.register_plugin('webhook', WebhookVerifier)
        VerifierFactory.register_plugin('sandbox', SandboxVerifier)

    @staticmethod
    def verify_composite(job, submission):
        """
        Executes all configured verifiers and calculates weighted score.
        """
        # Default to legacy config if new one is empty
        verifiers_config = job.verifiers_config or []
        if not verifiers_config:
            # Fallback to single verifier logic for backward compatibility
            v_type = job.verification_config.get('type')
            if v_type:
                verifiers_config = [{"type": v_type, "weight": 1.0, "config": job.verification_config}]
            else:
                return {"success": False, "score": 0, "reason": "No verification config found"}

        total_score = 0.0
        total_weight = 0.0
        details = []

        for v_conf in verifiers_config:
            v_type = v_conf.get('type')
            weight = v_conf.get('weight', 1.0)
            instance_config = v_conf.get('config', {})
            
            try:
                verifier = VerifierFactory.get_verifier(v_type)
                # Pass instance config merged with job verification config if needed, 
                # but better to assume instances are configured in 'verifiers_config'
                score, detail = verifier.verify(job, submission, instance_config)
                
                weighted_score = score * weight
                total_score += weighted_score
                total_weight += weight
                
                details.append(f"[{v_type.upper()}] Score: {score} * {weight} = {weighted_score:.2f} | {detail}")
            except Exception as e:
                # If a verifier crashes, it counts as 0 score
                total_weight += weight
                details.append(f"[{v_type.upper()}] CRASH: {str(e)}")

        final_score = total_score / total_weight if total_weight > 0 else 0
        is_passing = final_score >= 80.0

        return {
            "success": is_passing,
            "score": final_score,
            "reason": "\n".join(details)
        }
