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
from .streams import UgreenDownloadStream

__all__ = [
    "ApiError",
    "AuthenticationError",
    "PyUgosError",
    "SearchTimeoutError",
    "ThumbnailSize",
    "TransportError",
    "UgreenBinary",
    "UgreenDownloadStream",
    "UgreenFile",
    "UgreenNasClient",
]

__version__ = "0.1.0"
