"""Typed adapter errors: the orchestrator decides by TYPE what to do next
(retry, ask a human, stop with an instruction) instead of searching words in
error messages. All are RuntimeError/TimeoutError subclasses, so older code
that catches those keeps working."""

from __future__ import annotations


class AdapterError(RuntimeError):
    """Base class. `kind`: transient | human | config | stopped."""

    kind = "transient"


class TransientAdapterError(AdapterError):
    """Page not ready, send failed, session restored mid-reply...: retry (new chat)."""

    kind = "transient"


class NeedsHumanError(AdapterError):
    """Captcha / pop-up / login that a human did not resolve in time."""

    kind = "human"


class ConfigAdapterError(AdapterError):
    """Wrong setup (model not available, nothing to type into): retrying cannot help."""

    kind = "config"


class StoppedError(AdapterError):
    """The project was stopped while waiting."""

    kind = "stopped"


class ReplyTimeoutError(TimeoutError):
    """No (complete) reply in time. The message may already be with the service."""

    kind = "timeout"


def error_kind(exc: BaseException) -> str:
    """transient | human | config | stopped | timeout | unknown."""
    kind = getattr(exc, "kind", None)
    if isinstance(kind, str):
        return kind
    if isinstance(exc, TimeoutError):
        return "timeout"
    return "unknown"
