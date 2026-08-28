"""Small data objects returned by the client."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Mapping, Optional, Union

if TYPE_CHECKING:
    from .client import UgreenNasClient


PathLike = Union[str, "Path"]


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

    def get_thumbnail(self, width: int = 256, height: int = 256) -> UgreenBinary:
        if self.is_directory:
            raise ValueError("Directories do not have thumbnails")
        if width <= 0 or height <= 0:
            raise ValueError("Thumbnail width and height must be positive")
        if self._client is None:
            raise RuntimeError("This file is not attached to a client")
        return self._client._get_thumbnail(self, width=width, height=height)

    def download(self, destination: Optional[PathLike] = None) -> Union[bytes, Path]:
        """Download the original, returning bytes or saving it below a directory."""

        if self.is_directory:
            raise ValueError("Directory downloads are not supported")
        if self._client is None:
            raise RuntimeError("This file is not attached to a client")
        data = self._client._download_file(self)
        if destination is None:
            return data.content
        directory = Path(destination)
        directory.mkdir(parents=True, exist_ok=True)
        return data.save(directory / self.name)


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
