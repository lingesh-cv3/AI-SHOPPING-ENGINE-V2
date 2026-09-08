"""Sending things to a shopper outside the chat. Currently just order mail."""

from .confirmation import confirm_order
from .mailer import Mailer, SentMail

__all__ = ["Mailer", "SentMail", "confirm_order"]
