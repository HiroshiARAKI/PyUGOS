"""Small data objects returned by the client."""

import math
import os
import tempfile
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Union

from .video import UgreenHlsPlayback, VideoQuality, VideoVariant

if TYPE_CHECKING:
    from .client import UgreenNasClient
    from .streams import UgreenDownloadStream


PathLike = Union[str, "Path"]


class ThumbnailSize(IntEnum):
    """UGOS thumbnail rendition selected by the ``size_type`` query value.

    Pixel dimensions are approximate and depend on the source image and UGOS
    firmware.  Values observed on DH2300 are roughly 592px for MEDIUM, 128px
    for SMALL, and 1600px or larger for LARGE.
    """

    MEDIUM = 1
    SMALL = 2
    LARGE = 3


@dataclass(frozen=True)
class UgreenBinary:
    """Binary data returned by UGOS, with a convenience method for local saving."""

    content: bytes
    content_type: Optional[str] = None

    def __bytes__(self) -> bytes:
        return self.content

    def __len__(self) -> int:
        return len(self.content)

    def save(self, destination: PathLike) -> Path:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(self.content)
        return path


@dataclass(frozen=True)
class UgreenMediaInfo:
    """Media metadata reported by UGOS for one file.

    Fields that do not apply to the file, or that are absent on the running
    firmware, are ``None``.  ``raw`` retains the complete response data so
    callers can inspect firmware-specific additions without losing the typed
    fields observed in the UGOS Web File Manager.
    """

    file_collation: Optional[str] = None
    width: Optional[int] = None
    height: Optional[int] = None
    duration: Optional[float] = None
    bit_rate: Optional[str] = None
    channel: Optional[str] = None
    device: Optional[str] = None
    software: Optional[str] = None
    color_space: Optional[str] = None
    resolution: Optional[str] = None
    shoot_time: Optional[int] = None
    frame_rate: Optional[float] = None
    video_format: Optional[str] = None
    hdr: Optional[bool] = None
    iso: Optional[str] = None
    aperture: Optional[str] = None
    shutter_speed: Optional[str] = None
    focal_length: Optional[str] = None
    resolution_type: Optional[int] = None
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class UgreenFile:
    """A file or directory reported by the UGOS File Manager search API."""

    name: str
    path: str
    size: int
    mtime: int
    ctime: int
    extension: str
    is_directory: bool
    raw: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)
    _client: "UgreenNasClient" = field(repr=False, compare=False, default=None)  # type: ignore[assignment]

    def get_media_info(self) -> UgreenMediaInfo:
        """Get audio, video, or image metadata reported by UGOS."""

        if self.is_directory:
            raise ValueError("Directories do not have media information")
        if self._client is None:
            raise RuntimeError("This file is not attached to a client")
        return self._client._get_media_info(self)

    def get_thumbnail(self, size: ThumbnailSize = ThumbnailSize.SMALL) -> UgreenBinary:
        """Get one of the three server-defined thumbnail renditions."""

        if self.is_directory:
            raise ValueError("Directories do not have thumbnails")
        if not isinstance(size, ThumbnailSize):
            raise TypeError("size must be a ThumbnailSize value")
        if self._client is None:
            raise RuntimeError("This file is not attached to a client")
        return self._client._get_thumbnail(self, size=size)

    def open_download(
        self,
        range_header: Optional[str] = None,
    ) -> "UgreenDownloadStream":
        """Open a closeable streaming download, optionally for one byte range."""

        if self.is_directory:
            raise ValueError("Directory downloads are not supported")
        if self._client is None:
            raise RuntimeError("This file is not attached to a client")
        return self._client._open_download_stream(self, range_header=range_header)

    def open_video(
        self,
        quality: VideoQuality,
        range_header: Optional[str] = None,
        *,
        preparation_timeout: float = 60.0,
    ) -> Union["UgreenDownloadStream", UgreenHlsPlayback]:
        """Open the original byte stream or a transcoded HLS playback.

        ``ORIGINAL`` delegates to ``open_download()`` and accepts one byte
        range.  ``P1080`` and ``P720`` return an HLS playback and therefore do
        not accept ``range_header``.
        """

        if not isinstance(quality, VideoQuality):
            raise TypeError("quality must be a VideoQuality value")
        if quality is VideoQuality.ORIGINAL:
            return self.open_download(range_header=range_header)
        if range_header is not None:
            raise ValueError("range_header is only supported for ORIGINAL video")
        return self.open_video_playback(
            quality,
            preparation_timeout=preparation_timeout,
        )

    def open_video_playback(
        self,
        quality: VideoQuality,
        *,
        preparation_timeout: float = 60.0,
    ) -> UgreenHlsPlayback:
        """Prepare UGOS' browser HLS rendition at 1080p or 720p."""

        if self.is_directory:
            raise ValueError("Directories do not have video playbacks")
        if not isinstance(quality, VideoQuality):
            raise TypeError("quality must be a VideoQuality value")
        if quality is VideoQuality.ORIGINAL:
            raise ValueError("Use open_video() or open_download() for ORIGINAL video")
        if self._client is None:
            raise RuntimeError("This file is not attached to a client")
        return self._client._open_video_playback(
            self,
            quality=quality,
            preparation_timeout=preparation_timeout,
        )

    def get_video_qualities(
        self,
        *,
        preparation_timeout: float = 60.0,
    ) -> List[VideoVariant]:
        """Get the supported 1080p/720p renditions plus the original."""

        if self.is_directory:
            raise ValueError("Directories do not have video qualities")
        if self._client is None:
            raise RuntimeError("This file is not attached to a client")
        return self._client._get_video_qualities(
            self,
            preparation_timeout=preparation_timeout,
        )

    def download(self, destination: Optional[PathLike] = None) -> Union[bytes, Path]:
        """Download the original, streaming directly when saving to a directory."""

        if self.is_directory:
            raise ValueError("Directory downloads are not supported")
        if destination is None:
            with self.open_download() as stream:
                return b"".join(stream.iter_bytes())
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / self.name
        temporary_path: Optional[Path] = None
        try:
            with self.open_download() as stream:
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=str(directory),
                    prefix=".pyugos-",
                    suffix=".part",
                    delete=False,
                ) as output:
                    temporary_path = Path(output.name)
                    for chunk in stream.iter_bytes():
                        output.write(chunk)
            assert temporary_path is not None
            os.replace(temporary_path, path)
        except BaseException:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except OSError:
                    pass
            raise
        return path


def _first(record: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        value = record.get(name)
        if value is not None:
            return value
    return default


def _integer(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "dir", "directory", "folder"}
    return bool(value)


def _media_string(value: Any) -> Optional[str]:
    return value if isinstance(value, str) else None


def _media_integer(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return None


def _media_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def media_info_from_record(record: Mapping[str, Any]) -> UgreenMediaInfo:
    """Map the type-checked fields used by the UGOS Web File Manager."""

    hdr = record.get("hdr")
    return UgreenMediaInfo(
        file_collation=_media_string(record.get("file_collation")),
        width=_media_integer(record.get("width")),
        height=_media_integer(record.get("height")),
        duration=_media_number(record.get("duration")),
        bit_rate=_media_string(record.get("bit_rate")),
        channel=_media_string(record.get("channel")),
        device=_media_string(record.get("device")),
        software=_media_string(record.get("software")),
        color_space=_media_string(record.get("color_space")),
        resolution=_media_string(record.get("resolution")),
        shoot_time=_media_integer(record.get("shoot_time")),
        frame_rate=_media_number(record.get("frame_rate")),
        video_format=_media_string(record.get("video_format")),
        hdr=hdr if isinstance(hdr, bool) else None,
        iso=_media_string(record.get("iso")),
        aperture=_media_string(record.get("aperture")),
        shutter_speed=_media_string(record.get("shutter_speed")),
        focal_length=_media_string(record.get("focal_length")),
        resolution_type=_media_integer(record.get("resolution_type")),
        raw=dict(record),
    )


def file_from_record(
    client: "UgreenNasClient",
    record: Mapping[str, Any],
    *,
    search_root: str,
) -> UgreenFile:
    path = str(_first(record, "path", "file_path", "full_path", default=""))
    name = str(_first(record, "name", "file_name", "filename", default=""))
    if not name and path:
        name = path.rstrip("/").rsplit("/", 1)[-1]
    if not path and name:
        path = search_root.rstrip("/") + "/" + name

    extension = str(_first(record, "extension", "file_ext", "ext", default=""))
    if not extension and "." in name:
        extension = name.rsplit(".", 1)[-1]
    extension = extension.lstrip(".").lower()

    directory_value = _first(record, "is_directory", "is_dir", "is_folder")
    if directory_value is None:
        directory_value = _first(record, "type", "file_type", default="")

    raw: Dict[str, Any] = dict(record)
    return UgreenFile(
        name=name,
        path=path,
        size=_integer(_first(record, "size", "file_size", default=0)),
        mtime=_integer(
            _first(record, "mtime", "modify_time", "modified_at", "updated_at", default=0)
        ),
        ctime=_integer(
            _first(record, "ctime", "create_time", "created_at", default=0)
        ),
        extension=extension,
        is_directory=_boolean(directory_value),
        raw=raw,
        _client=client,
    )
