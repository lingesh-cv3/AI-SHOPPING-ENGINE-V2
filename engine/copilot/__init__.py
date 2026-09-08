"""Natural-language Q&A over a merchant's own store data, and over a CV3
operator's cross-merchant workload. Read-only."""

from .service import CopilotReply, ask, ask_ops

__all__ = ["CopilotReply", "ask", "ask_ops"]
