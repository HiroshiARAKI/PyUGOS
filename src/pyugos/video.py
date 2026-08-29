"""HLS playback objects for UGOS' browser transcode service."""

import json
import ssl
import threading
from dataclasses import dataclass
from enum import Enum
from secrets import token_urlsafe
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Mapping, Optional, Set, Tuple
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit

from .errors import ApiError, TransportError

if TYPE_CHECKING:
    from .client import UgreenNasClient
    from .streams import UgreenDownloadStream


class VideoQuality(str, Enum):
    """Video renditions exposed by PyUGOS."""

    P1080 = "1080p"
    P720 = "720p"
    ORIGINAL = "original"


@dataclass(frozen=True)
class VideoVariant:
    """A video rendition reported by UGOS."""

    quality: VideoQuality
    width: Optional[int]
    height: Optional[int]
    transcoded: bool


@dataclass(frozen=True)
class UgreenHlsManifest:
    """A small, token-free HLS media playlist."""

    content: bytes
    content_type: str = "application/vnd.apple.mpegurl"

    def __bytes__(self) -> bytes:
        return self.content

    def __len__(self) -> int:
        return len(self.content)


class UgreenPlaybackHeartbeat:
    """Keep a UGOS transcode session alive over its private WebSocket."""

    def __init__(
        self,
        url: str,
        *,
        task_id: str,
        timeout: float,
        verify_tls: bool,
        interval: float = 10.0,
    ) -> None:
        self._url: Optional[str] = url
        self._task_id = task_id
        self._timeout = timeout
        self._verify_tls = verify_tls
        self._interval = interval
        self._socket: Any = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._error: Optional[TransportError] = None
        self._closed = False

    def start(self) -> None:
        """Connect and send the initial application-level ping."""

        try:
            from websocket import create_connection

            sslopt = {
                "cert_reqs": ssl.CERT_REQUIRED if self._verify_tls else ssl.CERT_NONE,
            }
            assert self._url is not None
            self._socket = create_connection(
                self._url,
                timeout=self._timeout,
                enable_multithread=True,
                sslopt=sslopt,
            )
            # Do not retain a tokenized URL after the connection is open.
            self._url = None
            self._send("ping")
        except Exception:
            self._url = None
            self.close()
            raise TransportError("Could not start the UGOS video playback session") from None

        self._thread = threading.Thread(
            target=self._run,
            name="pyugos-video-heartbeat",
            daemon=True,
        )
        self._thread.start()

    def _message(self, command: str) -> str:
        import time

        return json.dumps(
            {
                "cmd": command,
                "timestamp": int(time.time() * 1000),
                "task_id": self._task_id,
                "data": {},
            },
            separators=(",", ":"),
        )

    def _send(self, command: str) -> None:
        self._socket.send(self._message(command))

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                self._send("ping")
            except Exception:
                self._error = TransportError("UGOS video playback heartbeat was interrupted")
                return

    def raise_if_failed(self) -> None:
        if self._error is not None:
            raise self._error from None

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        socket = self._socket
        self._socket = None
        if socket is not None:
            try:
                socket.send(self._message("close"))
            except Exception:
                pass
            try:
                socket.close()
            except Exception:
                pass
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        self._url = None

    def __repr__(self) -> str:
        return "UgreenPlaybackHeartbeat(closed={!r})".format(self._closed)


class UgreenHlsPlayback:
    """A closeable HLS playback session with opaque segment identifiers.

    The upstream manifest and its tokenized URL are never exposed.  By default,
    ``open_manifest()`` rewrites segment URIs to ``segments/<opaque-id>`` so a
    reverse proxy can route those requests to ``open_segment()``.
    """

    protocol = "hls"
    content_type = "application/vnd.apple.mpegurl"

    def __init__(
        self,
        client: "UgreenNasClient",
        *,
        task_id: str,
        src_type: int,
        manifest: str,
        requested_quality: VideoQuality,
        actual_quality: VideoQuality,
        is_transcoded: bool,
        heartbeat: UgreenPlaybackHeartbeat,
    ) -> None:
        self.requested_quality = requested_quality
        self.actual_quality = actual_quality
        self.is_transcoded = is_transcoded
        self._client = client
        self._task_id = task_id
        self._src_type = src_type
        self._heartbeat = heartbeat
        self._manifest_lines, self._segments = self._parse_manifest(manifest)
        self._open_streams: Set["UgreenDownloadStream"] = set()
        self._lock = threading.RLock()
        self._closed = False

    @staticmethod
    def _parse_manifest(
        manifest: str,
    ) -> Tuple[List[Tuple[str, Optional[str]]], Dict[str, Tuple[str, Mapping[str, str]]]]:
        if not manifest.lstrip().startswith("#EXTM3U"):
            raise ApiError("UGOS returned an invalid HLS manifest")

        lines: List[Tuple[str, Optional[str]]] = []
        segments: Dict[str, Tuple[str, Mapping[str, str]]] = {}
        for raw_line in manifest.splitlines():
            line = raw_line.strip()
            upper = line.upper()
            if upper.startswith("#EXT-X-STREAM-INF"):
                raise ApiError("UGOS returned an unsupported HLS master playlist")
            if upper.startswith("#EXT-X-KEY") or upper.startswith("#EXT-X-MAP"):
                raise ApiError("UGOS returned an unsupported HLS manifest feature")
            if not line or line.startswith("#"):
                lines.append((raw_line, None))
                continue

            parsed = urlsplit(line)
            if parsed.scheme or parsed.netloc:
                raise ApiError("UGOS returned an unsafe HLS segment URL")
            path = urljoin("/ugreen/v2/stream/transcode/web/m3u8", parsed.path)
            if not path.startswith("/ugreen/v2/stream/transcode/web/get/"):
                raise ApiError("UGOS returned an unexpected HLS resource URL")
            segment_id = token_urlsafe(18)
            params = dict(parse_qsl(parsed.query, keep_blank_values=True))
            segments[segment_id] = (path, params)
            lines.append((raw_line, segment_id))

        if not segments:
            raise ApiError("UGOS returned an empty HLS manifest")
        return lines, segments

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def segment_ids(self) -> Tuple[str, ...]:
        """Opaque identifiers accepted by ``open_segment()``."""

        return tuple(self._segments)

    def open_manifest(
        self,
        segment_url: Optional[Callable[[str], str]] = None,
    ) -> UgreenHlsManifest:
        """Return a token-free playlist with caller-controlled segment URIs."""

        with self._lock:
            if self._closed:
                raise ValueError("The video playback is closed")
            self._heartbeat.raise_if_failed()

        make_url = segment_url or (lambda value: "segments/{}".format(quote(value, safe="")))
        output: List[str] = []
        for raw_line, segment_id in self._manifest_lines:
            if segment_id is None:
                output.append(raw_line)
                continue
            rewritten = str(make_url(segment_id))
            if "\r" in rewritten or "\n" in rewritten:
                raise ValueError("segment_url must not return line breaks")
            output.append(rewritten)
        return UgreenHlsManifest(("\n".join(output) + "\n").encode("utf-8"))

    def open_segment(self, segment_id: str) -> "UgreenDownloadStream":
        """Open one upstream MPEG-TS segment without exposing its UGOS URL."""

        with self._lock:
            if self._closed:
                raise ValueError("The video playback is closed")
            self._heartbeat.raise_if_failed()
            resource = self._segments.get(segment_id)
            if resource is None:
                raise ValueError("Unknown HLS segment id")

        path, params = resource

        def remove_stream(stream: "UgreenDownloadStream") -> None:
            with self._lock:
                self._open_streams.discard(stream)

        stream = self._client._open_video_segment(
            path,
            params=params,
            on_close=remove_stream,
        )
        with self._lock:
            if self._closed:
                stream.close()
                raise ValueError("The video playback is closed")
            self._open_streams.add(stream)
        return stream

    def close(self) -> None:
        """Close segment responses and stop the UGOS playback session."""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            streams = list(self._open_streams)
            self._open_streams.clear()
        for stream in streams:
            try:
                stream.close()
            except Exception:
                pass
        try:
            self._client._close_video_session(self._task_id, src_type=self._src_type)
        finally:
            self._heartbeat.close()

    def __enter__(self) -> "UgreenHlsPlayback":
        if self._closed:
            raise ValueError("The video playback is closed")
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            "UgreenHlsPlayback(protocol='hls', requested_quality={!r}, "
            "actual_quality={!r}, is_transcoded={!r}, segments={!r}, closed={!r})"
        ).format(
            self.requested_quality,
            self.actual_quality,
            self.is_transcoded,
            len(self._segments),
            self.closed,
        )

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


def websocket_url(
    base_url: str,
    *,
    token_name: str,
    token: str,
) -> str:
    """Build the private UGOS heartbeat URL without making it public."""

    parsed = urlsplit(base_url)
    scheme = "wss" if parsed.scheme == "https" else "ws"
    return "{}://{}{}?{}".format(
        scheme,
        parsed.netloc,
        "/ugreen/v2/stream/transcode/ws",
        urlencode({token_name: token}),
    )
