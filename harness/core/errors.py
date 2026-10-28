"""Domain errors with safe, provider-neutral messages."""


class HarnessError(Exception):
    """Base class for all expected harness failures."""


class ProtocolError(HarnessError):
    """Raised when a streamed delegate protocol is malformed."""


class MediaError(HarnessError):
    """Raised when buffered media cannot form a valid provider input."""


class BackendError(HarnessError):
    """Raised when a backend cannot produce a usable response."""

    def __init__(self, provider: str, message: str, status_code: int = 0):
        super().__init__(message)
        self.provider = provider
        self.status_code = status_code


class ConfigurationError(HarnessError):
    """Raised for invalid local configuration, before an API call is made."""


class ApiNotConfiguredError(ConfigurationError):
    """Raised when a selected backend has no credentials or local endpoint."""
