from typing import Any, Dict, Iterable, List, Optional

import pytest
from requests.exceptions import ChunkedEncodingError, ConnectionError

from pyugos import ApiError, TransportError, UgreenDownloadStream, UgreenNasClient
from pyugos.models import file_from_record


TOKEN = "unit-test-download-token"


class FakeStreamingResponse:
    def __init__(
        self,
        status_code: int,
        *,
        headers: Optional[Dict[str, str]] = None,
        chunks: Optional[Iterable[Any]] = None,
        json_payload: Any = None,
    ) -> None:
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = list(chunks or [])
        self._json_payload = json_payload
        self.closed = False

    def iter_content(self, chunk_size: int):
        for item in self._chunks:
            if isinstance(item, Exception):
                raise item
            yield item

    def json(self):
        if isinstance(self._json_payload, Exception):
            raise self._json_payload
        return self._json_payload

    def close(self):
        self.closed = True


class FakeStreamingSession:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: List[Any] = []

    def request(self, method: str, url: str, **kwargs: Any):
        self.calls.append((method, url, kwargs))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def make_file(response: Any):
    session = FakeStreamingSession(response)
    client = UgreenNasClient("nas.local", session=session)  # type: ignore[arg-type]
    client._token = TOKEN
    client._static_token = TOKEN
    client._auth_type = "header"
    client._public_key = object()
    file = file_from_record(
        client,
        {
            "name": "movie.mp4",
            "path": "/home/user/movie.mp4",
            "size": 10,
            "is_dir": False,
        },
        search_root="/home/user",
    )
    return file, session


def test_normal_200_stream_exposes_metadata_and_closes_after_iteration():
    response = FakeStreamingResponse(
        200,
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": "6",
            "Accept-Ranges": "bytes",
        },
        chunks=[b"abc", b"", b"def"],
    )
    file, session = make_file(response)

    with file.open_download() as stream:
        assert isinstance(stream, UgreenDownloadStream)
        assert stream.status_code == 200
        assert stream.content_type == "video/mp4"
        assert stream.content_length == 6
        assert stream.content_range is None
        assert stream.accept_ranges == "bytes"
        assert b"".join(stream.iter_bytes(chunk_size=3)) == b"abcdef"

    assert stream.closed
    assert response.closed
    assert session.calls[0][2]["stream"] is True
    assert "Range" not in session.calls[0][2]["headers"]
    assert TOKEN not in repr(stream)


def test_single_range_206_is_forwarded_and_exposes_content_range():
    response = FakeStreamingResponse(
        206,
        headers={
            "Content-Type": "video/mp4",
            "Content-Length": "4",
            "Content-Range": "bytes 0-3/10",
            "Accept-Ranges": "bytes",
        },
        chunks=[b"0123"],
    )
    file, session = make_file(response)

    with file.open_download(range_header="bytes=0-3") as stream:
        assert stream.status_code == 206
        assert stream.content_length == 4
        assert stream.content_range == "bytes 0-3/10"
        assert list(stream.iter_bytes()) == [b"0123"]

    assert session.calls[0][2]["headers"]["Range"] == "bytes=0-3"
    assert response.closed


def test_416_is_returned_to_the_caller_and_can_be_closed():
    response = FakeStreamingResponse(
        416,
        headers={"Content-Length": "0", "Content-Range": "bytes */10"},
    )
    file, _ = make_file(response)

    stream = file.open_download(range_header="bytes=100-200")
    assert stream.status_code == 416
    assert stream.content_range == "bytes */10"
    stream.close()
    stream.close()

    assert stream.closed
    assert response.closed
    with pytest.raises(ValueError, match="closed"):
        list(stream.iter_bytes())


def test_multiple_or_invalid_ranges_are_rejected_before_request():
    file, session = make_file(FakeStreamingResponse(206))

    for value in ("bytes=0-1,4-5", "bytes=5-4", "bytes=-0", "items=0-1"):
        with pytest.raises(ValueError):
            file.open_download(range_header=value)

    assert session.calls == []


def test_interrupted_stream_closes_and_does_not_expose_token():
    response = FakeStreamingResponse(
        200,
        chunks=[
            b"partial",
            ChunkedEncodingError("connection lost: ?token={}".format(TOKEN)),
        ],
    )
    file, _ = make_file(response)
    stream = file.open_download()

    with pytest.raises(TransportError) as captured:
        list(stream.iter_bytes())

    assert stream.closed
    assert response.closed
    assert TOKEN not in str(captured.value)
    assert captured.value.__cause__ is None


def test_json_error_response_is_closed_and_raised_as_api_error():
    response = FakeStreamingResponse(
        200,
        headers={"Content-Type": "application/json"},
        json_payload={"code": 1302, "msg": "Path does not exist"},
    )
    file, _ = make_file(response)

    with pytest.raises(ApiError, match="Path does not exist"):
        file.open_download()

    assert response.closed


def test_request_error_does_not_expose_token_or_exception_cause():
    error = ConnectionError(
        "request failed: http://nas/ugreen/v1/filemgr/downloadFile?token={}".format(TOKEN)
    )
    file, _ = make_file(error)

    with pytest.raises(TransportError) as captured:
        file.open_download()

    assert TOKEN not in str(captured.value)
    assert captured.value.__cause__ is None


def test_download_to_directory_streams_to_disk(tmp_path):
    response = FakeStreamingResponse(
        200,
        headers={"Content-Type": "video/mp4", "Content-Length": "6"},
        chunks=[b"abc", b"def"],
    )
    file, _ = make_file(response)

    saved = file.download(destination=tmp_path)

    assert saved == tmp_path / "movie.mp4"
    assert saved.read_bytes() == b"abcdef"
    assert response.closed
    assert list(tmp_path.glob(".pyugos-*.part")) == []


def test_interrupted_download_preserves_existing_file_and_removes_temporary_file(
    tmp_path,
):
    destination = tmp_path / "movie.mp4"
    destination.write_bytes(b"existing-content")
    response = FakeStreamingResponse(
        200,
        headers={"Content-Type": "video/mp4", "Content-Length": "12"},
        chunks=[
            b"partial",
            ChunkedEncodingError("connection lost: ?token={}".format(TOKEN)),
        ],
    )
    file, _ = make_file(response)

    with pytest.raises(TransportError, match="interrupted"):
        file.download(destination=tmp_path)

    assert destination.read_bytes() == b"existing-content"
    assert list(tmp_path.glob(".pyugos-*.part")) == []
    assert response.closed
