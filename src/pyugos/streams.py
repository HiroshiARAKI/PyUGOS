"""Streaming response objects for large UGOS downloads."""

from typing import Any, Iterator, Optional

from requests import Response
from requests.exceptions import RequestException

from .errors import TransportError


class UgreenDownloadStream:
    """A one-shot, closeable download stream without access to its tokenized URL.

    Use this object as a context manager. Iteration closes the underlying HTTP
    response when it is exhausted or interrupted by a transport error.
    """

    def __init__(self, response: Response) -> None:
        self.status_code = int(response.status_code)
        self.content_type = response.headers.get("Content-Type")
        self.content_length = self._optional_int(response.headers.get("Content-Length"))
        self.content_range = response.headers.get("Content-Range")
        self.accept_ranges = response.headers.get("Accept-Ranges")
        self._response = response
        self._closed = False
        self._iteration_started = False

    @staticmethod
    def _optional_int(value: Optional[str]) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @property
    def closed(self) -> bool:
        return self._closed

    def iter_bytes(self, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        """Yield response bytes once and close the response when iteration ends."""

        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if self._closed:
            raise ValueError("The download stream is closed")
        if self._iteration_started:
            raise RuntimeError("The download stream can only be iterated once")
        self._iteration_started = True

        try:
            for chunk in self._response.iter_content(chunk_size=chunk_size):
                if chunk:
                    yield chunk
        except RequestException:
            # requests exceptions may contain the prepared URL, including the
            # session token. Do not expose the original exception as a cause.
            raise TransportError("UGOS download stream was interrupted") from None
        finally:
            self.close()

    def close(self) -> None:
        """Close the underlying HTTP response. Safe to call more than once."""

        if not self._closed:
            self._closed = True
            self._response.close()

    def __enter__(self) -> "UgreenDownloadStream":
        if self._closed:
            raise ValueError("The download stream is closed")
        return self

    def __exit__(self, *args: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            "UgreenDownloadStream(status_code={!r}, content_type={!r}, "
            "content_length={!r}, content_range={!r}, accept_ranges={!r}, closed={!r})"
        ).format(
            self.status_code,
            self.content_type,
            self.content_length,
            self.content_range,
            self.accept_ranges,
            self.closed,
        )

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            # Destructors must never mask interpreter shutdown or GC errors.
            pass
