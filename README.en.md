# PyUGOS

English | [日本語](README.md)

**NOTE: This is an unofficial tool for UGREEN NAS, based on behavior observed as of August 2026.**

PyUGOS is a small Python client for accessing the private API of UGOS Pro NAS devices. It currently supports read-only operations and requires Python 3.9 or later.

This library does not use an official public UGREEN API. Its communication protocol may change with future UGOS updates.

## Installation

```console
python -m pip install -e .
```

## Usage

```python
from pathlib import Path

from pyugos import ThumbnailSize, UgreenNasClient

nas = UgreenNasClient(host="192.168.1.100", port=9999)
nas.login(username="user", password="password")

files = nas.search(
    path="/home/user/Photos",
    recursive=True,
    types=["image", "video"],
)

for file in files:
    print(file.name, file.path, file.size, file.mtime)

    thumbnail = file.get_thumbnail(size=ThumbnailSize.SMALL)
    thumbnail.save(Path("thumbnails") / (file.name + ".webp"))

    # destination is a local directory.
    file.download(destination="originals")
```

The `size` argument to `get_thumbnail()` accepts `ThumbnailSize.SMALL` (`size_type=2`), `MEDIUM` (`1`), or `LARGE` (`3`). Actual pixel dimensions depend on the source image and UGOS version. The method returns a `UgreenBinary`; use `bytes(thumbnail)` to access its content and `thumbnail.content_type` to inspect the response Content-Type. `download()` returns `bytes` when no destination is given, or the saved `Path` when a destination is specified.

### Streaming downloads with Range

Use `open_download()` to download large files such as videos without buffering the complete original in memory. Use it as a context manager so the HTTP response is always closed.

```python
with file.open_download(range_header="bytes=0-1048575") as stream:
    print(stream.status_code)     # 200 / 206 / 416
    print(stream.content_type)
    print(stream.content_length)
    print(stream.content_range)
    print(stream.accept_ranges)

    for chunk in stream.iter_bytes(chunk_size=1024 * 1024):
        process(chunk)
```

Only a single range such as `bytes=0-1023`, `bytes=1024-`, or `bytes=-1024` is accepted. Multiple ranges are rejected before a request is sent. `download(destination=...)` also streams directly to the local file. Calling `download()` without a destination still buffers the complete original in memory because it returns `bytes`.

## Supported features

- Username/password login without OTP
- Header and URL token modes
- File search through server-side search tasks
- Thumbnail retrieval
- Range-aware streaming downloads
- Single-file downloads through `/ugreen/v1/filemgr/downloadFile`

The client does not expose methods for creating, updating, moving, or deleting files on the NAS. Search-task creation uses POST as required by the private API, but it does not modify the NAS filesystem.

The client has been tested against a DH2300 running UGOS Pro 1.18 in header token mode. Normal downloads (200), single-range downloads (206), and unsatisfiable ranges (416) have also been verified against the device. URL token mode is implemented from the observed protocol but has not been tested against a device.

## Development

```console
python -m pip install -e '.[test]'
pytest
```

Do not store credentials or session tokens in source code, tests, or logs.
