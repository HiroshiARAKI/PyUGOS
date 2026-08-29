"""Small data objects returned by the client."""

from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional, Union

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
        with self.open_download() as stream, path.open("wb") as output:
            for chunk in stream.iter_bytes():
                output.write(chunk)
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
