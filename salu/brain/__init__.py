from .base import Brain, BrainEvent, BrainSession, HealthResult

__all__ = ["Brain", "BrainEvent", "BrainSession", "HealthResult", "create_brain"]


def create_brain(settings) -> Brain:
    """Factory — the single place a provider is chosen."""
    from .claude_sdk import ClaudeSdkBrain
    return ClaudeSdkBrain(settings)
