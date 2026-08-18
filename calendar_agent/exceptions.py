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


class ProxyTimeoutError(ProxyError):
    """Raised when the proxy doesn't respond before the client-side timeout.

    The operation's outcome is unknown: a confirmation-gated mutation may
    still execute if the operator approves after this client gave up. Callers
    must verify by re-reading the resource, not assume failure."""

    pass


class LLMError(Exception):
    """Raised when LLM operations fail."""

    pass
