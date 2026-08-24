"""Session memory, shared across the friction widget and the open-ended chat.

Both surfaces write against one session_id, so a shopper whose payment declined
does not have to mention it when they open the chat.
"""

from .store import (
    HISTORY_TURNS,
    add_turn,
    context_for,
    history,
    new_session_id,
    recent_friction,
    turns,
)

__all__ = [
    "HISTORY_TURNS",
    "add_turn",
    "context_for",
    "history",
    "new_session_id",
    "recent_friction",
    "turns",
]