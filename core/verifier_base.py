from abc import ABC, abstractmethod

class BaseVerifier(ABC):
    @abstractmethod
    def verify(self, job, submission, config=None):
        """
        :param job: The Job model instance
        :param submission: The submission data from the worker
        :param config: Specific configuration for this verifier instance (e.g. weight, params)
        :return: (score: float, details: str) - Score between 0.0 and 100.0
        """
        pass
