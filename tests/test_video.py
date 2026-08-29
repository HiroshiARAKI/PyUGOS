import json
import sys
from types import SimpleNamespace
from typing import Any, Dict, Iterable, List, Mapping, Optional

import pytest

from pyugos import (
    ApiError,
    UgreenDownloadStream,
    UgreenHlsPlayback,
    UgreenNasClient,
    VideoPreparationTimeoutError,
    VideoQuality,
    VideoQualityUnavailableError,
)
from pyugos.models import file_from_record
from pyugos.video import UgreenPlaybackHeartbeat


TOKEN = "unit-test-video-token"
UPSTREAM_TASK = "upstream-task-must-not-leak"


INFO = {
    "resolution": {"name": "4K", "resolution": "3840x2160"},
    "transcodeable": [
        {"name": "4K", "resolution": "3840x2160"},
        {"name": "1080P", "resolution": "1920x1080"},
        {"name": "720P", "resolution": "1280x720"},
    ],
    "audios": [{"id": 4352, "isDefault": True}],
    "subtitles": [],
    "redir_link": "http://nas/play?video_name=video-hash.mov",
}


def manifest_for(resolution: str) -> str:
    return """#EXTM3U
#EXT-X-VERSION:3
#EXT-X-TARGETDURATION:4
#EXTINF:4.00000
get/session/artifact/h264_{resolution}_00000.ts?task_id={task}&src_type=1&timestamp=1
#EXTINF:3.50000
get/session/artifact/h264_{resolution}_00001.ts?task_id={task}&src_type=1&timestamp=1
#EXT-X-ENDLIST
""".format(resolution=resolution, task=UPSTREAM_TASK)


class FakeHeartbeat:
    def __init__(self) -> None:
        self.closed = False
        self.failed = False

    def raise_if_failed(self) -> None:
        if self.failed:
            raise RuntimeError("heartbeat failed")

    def close(self) -> None:
        self.closed = True


class FakeStreamingResponse:
    def __init__(
        self,
        status_code: int = 200,
        *,
        headers: Optional[Dict[str, str]] = None,
        chunks: Optional[Iterable[bytes]] = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks or [])
        self.closed = False

    def iter_content(self, chunk_size: int):
        yield from self._chunks

    def close(self) -> None:
        self.closed = True


def make_file():
    client = UgreenNasClient("nas.local")
    client._token = TOKEN
    client._static_token = TOKEN
    client._auth_type = "header"
    client._public_key = object()
    file = file_from_record(
        client,
        {
            "name": "movie.mov",
            "path": "/home/user/movie.mov",
            "size": 100,
            "is_dir": False,
        },
        search_root="/home/user",
    )
    return file, client


def prepare_fake_video_client(client: UgreenNasClient, resolution: str):
    heartbeat = FakeHeartbeat()
    play_calls: List[Mapping[str, Any]] = []
    close_calls: List[Any] = []

    client._start_video_heartbeat = lambda task_id, timeout: heartbeat  # type: ignore[method-assign]
    client._get_video_info = lambda *args, **kwargs: INFO  # type: ignore[method-assign]

    def fake_play(path, params, timeout):
        play_calls.append(dict(params))
        return {"bitrate_bps": 8_000_000}

    client._get_video_private = fake_play  # type: ignore[method-assign]
    client._get_video_manifest = (  # type: ignore[method-assign]
        lambda *args, **kwargs: manifest_for(resolution)
    )
    client._close_video_session = (  # type: ignore[method-assign]
        lambda task_id, src_type: close_calls.append((task_id, src_type))
    )
    return heartbeat, play_calls, close_calls


@pytest.mark.parametrize(
    ("quality", "resolution"),
    [
        (VideoQuality.P1080, "1920x1080"),
        (VideoQuality.P720, "1280x720"),
    ],
)
def test_hls_playback_prepares_quality_rewrites_manifest_and_closes(
    quality,
    resolution,
):
    file, client = make_file()
    heartbeat, play_calls, close_calls = prepare_fake_video_client(client, resolution)
    segment_response = FakeStreamingResponse(
        headers={"Content-Type": "application/octet-stream"},
        chunks=[b"Gsegment", b"tail"],
    )
    opened_resources = []

    def open_segment(path, *, params, on_close):
        opened_resources.append((path, dict(params)))
        return UgreenDownloadStream(segment_response, on_close=on_close)

    client._open_video_segment = open_segment  # type: ignore[method-assign]

    playback = file.open_video_playback(quality)

    assert isinstance(playback, UgreenHlsPlayback)
    assert playback.protocol == "hls"
    assert playback.requested_quality is quality
    assert playback.actual_quality is quality
    assert playback.is_transcoded is True
    assert play_calls[0]["m3u8_file"] == "video-hash.mov_{}.m3u8".format(resolution)
    assert play_calls[0]["prefer_h265"] == "false"
    assert play_calls[0]["audio_index"] == 4352

    manifest = playback.open_manifest()
    text = bytes(manifest).decode()
    assert manifest.content_type == "application/vnd.apple.mpegurl"
    assert UPSTREAM_TASK not in text
    assert "/ugreen/" not in text
    assert text.count("segments/") == 2
    assert TOKEN not in text
    assert TOKEN not in repr(playback)

    segment_id = playback.segment_ids[0]
    segment = playback.open_segment(segment_id)
    assert b"".join(segment.iter_bytes()) == b"Gsegmenttail"
    assert opened_resources[0][0].startswith("/ugreen/v2/stream/transcode/web/get/")
    assert opened_resources[0][1]["task_id"] == UPSTREAM_TASK

    playback.close()
    playback.close()
    assert heartbeat.closed
    assert len(close_calls) == 1
    assert segment_response.closed


def test_get_video_qualities_returns_1080_720_and_original():
    file, client = make_file()
    heartbeat, _, close_calls = prepare_fake_video_client(client, "1920x1080")

    qualities = file.get_video_qualities()

    assert [(item.quality, item.width, item.height, item.transcoded) for item in qualities] == [
        (VideoQuality.P1080, 1920, 1080, True),
        (VideoQuality.P720, 1280, 720, True),
        (VideoQuality.ORIGINAL, 3840, 2160, False),
    ]
    assert heartbeat.closed
    assert len(close_calls) == 1


def test_original_open_video_delegates_to_open_download_and_keeps_range():
    file, client = make_file()
    marker = object()
    calls = []

    def open_download(attached_file, *, range_header):
        calls.append((attached_file, range_header))
        return marker

    client._open_download_stream = open_download  # type: ignore[method-assign]

    result = file.open_video(VideoQuality.ORIGINAL, range_header="bytes=0-")

    assert result is marker
    assert calls == [(file, "bytes=0-")]


def test_transcoded_video_rejects_range_before_preparation():
    file, client = make_file()
    called = False

    def start(*args, **kwargs):
        nonlocal called
        called = True

    client._start_video_heartbeat = start  # type: ignore[method-assign]

    with pytest.raises(ValueError, match="only supported for ORIGINAL"):
        file.open_video(VideoQuality.P1080, range_header="bytes=0-1")

    assert not called


def test_unavailable_quality_closes_playback_session():
    file, client = make_file()
    heartbeat, _, close_calls = prepare_fake_video_client(client, "1920x1080")
    unavailable_info = dict(INFO)
    unavailable_info["transcodeable"] = [INFO["transcodeable"][2]]
    client._get_video_info = lambda *args, **kwargs: unavailable_info  # type: ignore[method-assign]

    with pytest.raises(VideoQualityUnavailableError, match="1080p"):
        file.open_video_playback(VideoQuality.P1080)

    assert heartbeat.closed
    assert len(close_calls) == 1


def test_video_api_error_drops_sensitive_message_and_payload():
    file, client = make_file()
    heartbeat = FakeHeartbeat()
    close_calls = []
    client._start_video_heartbeat = lambda task_id, timeout: heartbeat  # type: ignore[method-assign]
    client._close_video_session = (  # type: ignore[method-assign]
        lambda task_id, src_type: close_calls.append((task_id, src_type))
    )

    def fail_private(*args, **kwargs):
        raise ApiError(
            "failed URL contains {}".format(TOKEN),
            code=5005,
            payload={"token": TOKEN},
        )

    client._post_private = fail_private  # type: ignore[method-assign]

    with pytest.raises(ApiError) as captured:
        file.open_video_playback(VideoQuality.P1080)

    assert TOKEN not in str(captured.value)
    assert captured.value.code == 5005
    assert captured.value.payload is None
    assert heartbeat.closed
    assert len(close_calls) == 1


def test_preparation_timeout_is_dedicated_error(monkeypatch):
    file, client = make_file()
    client._close_video_session = lambda *args, **kwargs: None  # type: ignore[method-assign]
    values = iter([10.0, 12.0])
    monkeypatch.setattr("pyugos.client.time.monotonic", lambda: next(values))

    with pytest.raises(VideoPreparationTimeoutError):
        file.open_video_playback(VideoQuality.P1080, preparation_timeout=1.0)


def test_segment_json_error_with_octet_stream_is_not_returned_as_video():
    _, client = make_file()
    response = FakeStreamingResponse(
        headers={"Content-Type": "application/octet-stream"},
        chunks=[json.dumps({"code": 150724, "msg": "preempted"}).encode()],
    )
    client._send_stream = lambda *args, **kwargs: response  # type: ignore[method-assign]

    with pytest.raises(ApiError) as captured:
        client._open_video_segment(
            "/ugreen/v2/stream/transcode/web/get/session/segment.ts",
            params={"task_id": "task"},
            on_close=lambda stream: None,
        )

    assert captured.value.code == 150724
    assert captured.value.payload is None
    assert response.closed


def test_manifest_rejects_master_playlist_and_upstream_absolute_urls():
    file, client = make_file()
    heartbeat, _, _ = prepare_fake_video_client(client, "1920x1080")
    base = dict(
        client=client,
        task_id="task",
        src_type=1,
        requested_quality=VideoQuality.P1080,
        actual_quality=VideoQuality.P1080,
        is_transcoded=True,
        heartbeat=heartbeat,
    )

    with pytest.raises(ApiError, match="master"):
        UgreenHlsPlayback(
            manifest="#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nchild.m3u8\n",
            **base,
        )
    with pytest.raises(ApiError, match="unsafe"):
        UgreenHlsPlayback(
            manifest="#EXTM3U\n#EXTINF:4\nhttps://example.test/segment.ts\n",
            **base,
        )


def test_heartbeat_messages_and_repr_do_not_expose_url_token(monkeypatch):
    class FakeSocket:
        def __init__(self) -> None:
            self.messages = []
            self.closed = False

        def send(self, value):
            self.messages.append(json.loads(value))

        def close(self):
            self.closed = True

    socket = FakeSocket()
    module = SimpleNamespace(create_connection=lambda *args, **kwargs: socket)
    monkeypatch.setitem(sys.modules, "websocket", module)
    heartbeat = UgreenPlaybackHeartbeat(
        "ws://nas/stream?token={}".format(TOKEN),
        task_id="task-id",
        timeout=1.0,
        verify_tls=True,
        interval=60.0,
    )

    heartbeat.start()
    assert socket.messages[0]["cmd"] == "ping"
    assert socket.messages[0]["task_id"] == "task-id"
    assert TOKEN not in repr(heartbeat)

    heartbeat.close()
    assert socket.messages[-1]["cmd"] == "close"
    assert socket.closed
