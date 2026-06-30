"""Engine-specific exceptions."""


class JobTimeoutError(Exception):
    """Raised when a conversion job exceeds its timeout."""
    pass


class RuleCacheError(Exception):
    """Raised when rule cache cannot load rules from DB."""
    pass


class AgentPoolNotStartedError(Exception):
    """Raised when a job is submitted before the agent pool is started."""
    pass


class MaxQueueDepthError(Exception):
    """Raised when the queue is at capacity and the job cannot be accepted."""
    pass
