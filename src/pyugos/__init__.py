"""Public API for PyUGOS."""

from .client import UgreenNasClient
from .errors import (
    ApiError,
    AuthenticationError,
    PyUgosError,
    SearchTimeoutError,
    TransportError,
    VideoPreparationTimeoutError,
    VideoQualityUnavailableError,
)
from .models import ThumbnailSize, UgreenBinary, UgreenFile
from .streams import UgreenDownloadStream
from .video import UgreenHlsManifest, UgreenHlsPlayback, VideoQuality, VideoVariant

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
    "UgreenHlsManifest",
    "UgreenHlsPlayback",
    "UgreenNasClient",
    "VideoPreparationTimeoutError",
    "VideoQuality",
    "VideoQualityUnavailableError",
    "VideoVariant",
]

__version__ = "0.1.0"
