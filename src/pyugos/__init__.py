"""Public API for PyUGOS."""

from .client import UgreenNasClient
from .errors import (
    ApiError,
    AuthenticationError,
    PyUgosError,
    SearchTimeoutError,
    TransportError,
)
from .models import ThumbnailSize, UgreenBinary, UgreenFile

__all__ = [
    "ApiError",
    "AuthenticationError",
    "PyUgosError",
    "SearchTimeoutError",
    "ThumbnailSize",
    "TransportError",
    "UgreenBinary",
    "UgreenFile",
    "UgreenNasClient",
]

__version__ = "0.1.0"
