"""Adapter for the Kettle & Bloom GraphQL platform.

The second platform. Proves the Standard Commerce Interface holds across a
genuinely different backend - and, because this one supports payment recovery,
proves the engine's behaviour follows the platform's capabilities rather than the
other way round.
"""

from .adapter import KettleAdapter

__all__ = ["KettleAdapter"]