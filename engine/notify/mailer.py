"""Sending mail out of the engine.

Checkout requires a signed-in account with an email precisely so an order
confirmation can reach somebody - collecting the address and never using it
would make that gate empty ceremony. No SMTP credentials are configured for
this project (local, SQLite, no hosting - see CLAUDE.md's "Running it"), so by
default this fails soft to a recorded-not-delivered outcome, the same
"recorded rather than pretended" shape `NOTIFY_BACK_IN_STOCK` already uses when
a capability is not built (`engine/execution/service.py`).

Real SMTP is one set of environment variables away (`Mailer.from_env` reads
MAILER_SMTP_HOST and friends) whenever a merchant's actual credentials exist;
nothing above this module needs to change to switch it on.
"""

from __future__ import annotations

import asyncio
import logging
import os
import smtplib
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import EmailMessage

logger = logging.getLogger(__name__)


@dataclass
class SentMail:
    """One send attempt, whether or not it actually left the building."""

    to: str
    subject: str
    body: str
    #: True only for a real SMTP send that succeeded.
    delivered: bool
    sent_at: datetime


class Mailer:
    """Sends mail, or honestly records that it could not.

    A send failure here is never a checkout failure: by the time this runs the
    order is already placed and paid for, so an SMTP outage should read as
    "the confirmation may be late", not as anything the shopper's payment was
    at risk from. `send` therefore never raises - it always returns a
    `SentMail`, with `delivered` telling the truth about what happened.
    """

    def __init__(
        self,
        *,
        host: str | None,
        port: int,
        username: str | None,
        password: str | None,
        sender: str,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender = sender

    @classmethod
    def from_env(cls) -> Mailer:
        """Build from environment, unconfigured if no host is set.

        A missing SMTP host is not an error, the same way a missing model key
        is not one in `ReasoningService.from_env` - the engine runs without
        one, recording rather than delivering, and starts actually sending the
        moment a real host is configured.
        """
        return cls(
            host=os.getenv("MAILER_SMTP_HOST", "").strip() or None,
            port=int(os.getenv("MAILER_SMTP_PORT", "587").strip() or "587"),
            username=os.getenv("MAILER_SMTP_USER", "").strip() or None,
            password=os.getenv("MAILER_SMTP_PASSWORD", "").strip() or None,
            sender=os.getenv("MAILER_FROM", "orders@example.com").strip(),
        )

    @property
    def configured(self) -> bool:
        return bool(self._host)

    async def send(self, to: str, subject: str, body: str) -> SentMail:
        if not self.configured:
            logger.info("mailer not configured; recorded, not sent: %r to %s", subject, to)
            return SentMail(
                to=to, subject=subject, body=body, delivered=False,
                sent_at=datetime.now(UTC),
            )

        try:
            # smtplib is synchronous; off the event loop so one slow or hung
            # SMTP connection cannot stall every other request the engine is
            # serving at the same time.
            await asyncio.to_thread(self._send_sync, to, subject, body)
            return SentMail(
                to=to, subject=subject, body=body, delivered=True,
                sent_at=datetime.now(UTC),
            )
        except Exception:
            logger.exception("mail send failed for %s", to)
            return SentMail(
                to=to, subject=subject, body=body, delivered=False,
                sent_at=datetime.now(UTC),
            )

    def _send_sync(self, to: str, subject: str, body: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self._sender
        msg["To"] = to
        msg.set_content(body)

        with smtplib.SMTP(self._host, self._port, timeout=10) as smtp:
            smtp.starttls()
            if self._username:
                smtp.login(self._username, self._password or "")
            smtp.send_message(msg)
