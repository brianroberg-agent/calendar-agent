"""Custom exceptions for the Calendar Agent."""


class ProxyError(Exception):
    """Base exception for proxy-related errors."""

    pass


class ProxyAuthError(ProxyError):
    """Raised when authentication with the proxy fails (401)."""

    pass


class ProxyForbiddenError(ProxyError):
    """Raised when the proxy returns 403: an operation blocked by policy or
    rejected by the operator (the proxy blocks in-line while a human approves
    mutations; 403 means rejection or operator timeout, never "pending")."""

    pass


class ProxyNotFoundError(ProxyError):
    """Raised when the proxy returns 404: the calendar or event does not exist.

    Distinct from a generic upstream failure because "the event is gone" is
    the expected, successful answer when verifying that a delete took effect."""

    pass


class ProxyTimeoutError(ProxyError):
    """Raised when the proxy doesn't respond before the client-side timeout.

    The operation's outcome is unknown: a confirmation-gated mutation may
    still execute if the operator approves after this client gave up. Callers
    must verify by re-reading the resource, not assume failure."""

    pass


class ProxyConfigError(ProxyError):
    """Raised when the proxy client's configuration is internally unsafe."""

    pass


class LLMError(Exception):
    """Raised when LLM operations fail."""

    pass
