"""Errors that change what a run does next."""

from __future__ import annotations


class PuenteError(Exception):
    """Base class."""


class AuthError(PuenteError):
    """The destination refused our credentials. Systemic, so the run stops."""


class RetryExhausted(PuenteError):
    """A call kept failing with retryable errors until the retry budget ran out."""

    def __init__(self, message: str, *, attempts: int, last_outcome: str) -> None:
        super().__init__(message)
        self.attempts = attempts
        self.last_outcome = last_outcome


class UnexpectedResponse(PuenteError):
    """A non-retryable answer where only a success makes sense, for example on a read."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"HTTP {status_code}: {body[:300]}")
        self.status_code = status_code
