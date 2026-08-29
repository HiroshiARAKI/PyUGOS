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

from pyugos import ThumbnailSize, UgreenNasClient, VideoQuality

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

### Media information

Retrieve the same media metadata that UGOS displays for image, audio, and video files in its detail panel.

```python
info = file.get_media_info()
print(info.width, info.height)
print(info.duration, info.frame_rate)
print(info.video_format, info.hdr)
```

The result is a `UgreenMediaInfo`. Fields unavailable for a file are `None`, while `info.raw` retains the original data, including firmware-specific additions. Typed fields cover resolution, duration, bit rate, channels, frame rate, video format, HDR, capture device and time, software, color space, ISO, aperture, shutter speed, and focal length. Values retain the units and formatting supplied by UGOS. Directories are not supported.

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

Only a single range such as `bytes=0-1023`, `bytes=1024-`, or `bytes=-1024` is accepted. Multiple ranges are rejected before a request is sent. `download(destination=...)` streams to a temporary file and replaces the destination only after completion, preserving an existing file if the transfer fails. Calling `download()` without a destination still buffers the complete original in memory because it returns `bytes`.

### 1080p and 720p HLS playback

On a DH2300 running UGOS Pro 1.18.2.0100, browser transcodes are quality-specific HLS (MPEG-TS), not MP4 Range streams. `open_video_playback()` starts a UGOS playback session, maintains its heartbeat, and exposes a token-free manifest using opaque segment identifiers.

```python
from pyugos import VideoQuality

qualities = file.get_video_qualities()

with file.open_video_playback(
    VideoQuality.P1080,  # VideoQuality.P720 is also supported
    preparation_timeout=60,
) as playback:
    print(playback.protocol)          # hls
    print(playback.requested_quality)
    print(playback.actual_quality)
    print(playback.is_transcoded)

    # Rewrite entries to routes served by your Hagukumi-style proxy. Without
    # a callback, the manifest uses relative segments/<opaque-id> URLs.
    manifest = playback.open_manifest(
        lambda segment_id: "/video/segments/{}".format(segment_id)
    )
    serve(bytes(manifest), content_type=manifest.content_type)

    segment_id = playback.segment_ids[0]
    with playback.open_segment(segment_id) as segment:
        for chunk in segment.iter_bytes():
            serve_chunk(chunk)
```

UGOS URLs, API tokens, and transcode task IDs are not included in public `repr()` output or the rewritten manifest. `playback.close()` closes open segment responses, the WebSocket heartbeat, and the UGOS playback session. Always use it as a context manager.

The original continues to use the existing Range stream:

```python
with file.open_video(
    VideoQuality.ORIGINAL,
    range_header="bytes=0-",
) as stream:
    for chunk in stream.iter_bytes():
        serve_chunk(chunk)
```

Because `P1080` and `P720` use HLS, they reject `range_header`. If UGOS does not list a requested rendition as `transcodeable`, PyUGOS raises `VideoQualityUnavailableError` and never falls back to the original.

## Supported features

- Username/password login without OTP
- Header and URL token modes
- File search through server-side search tasks
- Thumbnail retrieval
- Image, audio, and video media information
- Range-aware streaming downloads
- 1080p and 720p HLS browser-playback streams
- Available video-quality discovery
- Single-file downloads through `/ugreen/v1/filemgr/downloadFile`

The client does not expose methods for creating, updating, moving, or deleting files on the NAS. Search-task creation uses POST as required by the private API, but it does not modify the NAS filesystem.

The client has been tested against a DH2300 running UGOS Pro 1.18 in header token mode. Normal downloads (200), single-range downloads (206), and unsatisfiable ranges (416) have also been verified against the device. The 1080p/720p HLS protocol is based on a DH2300 running UGOS Pro 1.18.2.0100, its captured network traffic, and its Web player implementation. Media-information retrieval is based on a HAR capture and the Web UI implementation from the same firmware version. URL token mode is implemented from the observed protocol but has not been tested against a device.

## Development

```console
python -m pip install -e '.[test]'
pytest
```

Do not store credentials or session tokens in source code, tests, or logs.
