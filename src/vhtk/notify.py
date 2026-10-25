"""Notification layer: protocol + safe null implementation.

Core modules (safety, recovery, fsm) depend only on the ``Notifier``
protocol so they are testable without network access. The real Telegram
implementation (phase 5a) will live here too and satisfy the same protocol.
"""

from __future__ import annotations

from typing import Protocol, TypeAlias

import numpy as np
import numpy.typing as npt
import structlog

log = structlog.get_logger(__name__)

Image: TypeAlias = npt.NDArray[np.uint8]


class Notifier(Protocol):
    """Anything that can alert a human and ask for confirmation."""

    def send_text(self, text: str) -> None:
        """Send a plain text notification. Must never raise."""
        ...

    def send_photo(self, image: Image, caption: str) -> None:
        """Send a screenshot with a caption. Must never raise."""
        ...

    def wait_confirm(self, prompt: str, image: Image | None, timeout: float) -> bool:
        """Ask a human to approve an action; ``False`` on deny OR timeout.

        Timeout must count as denial — an unanswered question never
        authorizes an irreversible action.
        """
        ...


class NullNotifier:
    """Notifier used when Telegram is disabled: logs locally, denies everything.

    Denying every confirmation is the safe default — with ``confirm_mode``
    on and no reachable human, irreversible actions simply do not happen.
    """

    def send_text(self, text: str) -> None:
        """Log the message instead of sending it."""
        log.info("notify (null)", text=text)

    def send_photo(self, image: Image, caption: str) -> None:
        """Log the caption; the image is dropped."""
        log.info("notify photo (null)", caption=caption, shape=tuple(image.shape))

    def wait_confirm(self, prompt: str, image: Image | None, timeout: float) -> bool:
        """Always deny: no human is reachable through this notifier."""
        log.warning("confirm requested but notifier is null -> denied", prompt=prompt)
        return False
