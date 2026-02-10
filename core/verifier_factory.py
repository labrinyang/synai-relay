import importlib
import logging

class VerifierFactory:
    _plugins = {}

    @staticmethod
    def register_plugin(verifier_type, plugin_class):
        VerifierFactory._plugins[verifier_type] = plugin_class

    @staticmethod
    def get_verifier(verifier_type):
        """
        Returns an instance of the requested verifier plugin.
        """
        # Lazy load plugins to avoid circular imports or heavy startup
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
    def verify(job, payload):
        """
        Dispatches verification to the appropriate plugin.
        """
        verifier_type = job.verification_config.get('type')
        if not verifier_type:
            raise ValueError("Verification type not specified in job config.")
            
        verifier = VerifierFactory.get_verifier(verifier_type)
        return verifier.verify(job, payload)
