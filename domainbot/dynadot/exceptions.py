from __future__ import annotations


class DynadotError(Exception):
    """Base class for Dynadot integration failures."""


class DynadotAPIError(DynadotError):
    def __init__(self, message: str, *, http_status: int | None = None, api_code: str | None = None):
        super().__init__(message)
        self.http_status = http_status
        self.api_code = api_code


class DynadotNetworkError(DynadotError):
    """Network failure where the remote outcome may be unknown."""


class DynadotTimeoutError(DynadotNetworkError):
    """Timeout where a register command may have reached Dynadot."""

