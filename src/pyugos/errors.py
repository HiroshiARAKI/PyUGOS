"""Exceptions raised by PyUGOS."""

from typing import Any, Optional


class PyUgosError(Exception):
    """Base class for all package-specific errors."""


class TransportError(PyUgosError):
    """The NAS could not be reached or returned an invalid response."""


class ApiError(PyUgosError):
    """UGOS returned an application-level error."""

    def __init__(
        self,
        message: str,
        *,
        code: Optional[Any] = None,
        payload: Optional[Any] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.payload = payload


class AuthenticationError(ApiError):
    """Authentication failed or the session is not usable."""


class SearchTimeoutError(PyUgosError):
    """A server-side search task did not finish before its deadline."""


class VideoPreparationTimeoutError(PyUgosError):
    """UGOS did not prepare a browser playback before its deadline."""


class VideoQualityUnavailableError(PyUgosError):
    """UGOS cannot provide the requested video rendition."""
