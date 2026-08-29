"""Read-only UGOS Pro NAS client."""

import hashlib
import itertools
import json
import math
import re
import time
import uuid
from typing import Any, Callable, Dict, Iterable, List, Mapping, MutableMapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, urlsplit

import requests
from requests import Response, Session
from requests.exceptions import RequestException

from ._crypto import (
    decode_base64,
    decrypt_json,
    encrypt_query,
    encrypt_request_body,
    load_rsa_public_key,
    rsa_encrypt_base64,
)
from .errors import (
    ApiError,
    AuthenticationError,
    SearchTimeoutError,
    TransportError,
    VideoPreparationTimeoutError,
    VideoQualityUnavailableError,
)
from .models import (
    ThumbnailSize,
    UgreenBinary,
    UgreenFile,
    UgreenMediaInfo,
    file_from_record,
    media_info_from_record,
)
from .streams import UgreenDownloadStream
from .video import (
    UgreenHlsPlayback,
    UgreenPlaybackHeartbeat,
    VideoQuality,
    VideoVariant,
    websocket_url,
)


SEARCH_TYPES = {
    "all",
    "dir",
    "file",
    "video",
    "audio",
    "image",
    "documents",
    "archive",
    "custom",
}

SINGLE_RANGE_PATTERN = re.compile(r"bytes=(?:(\d+)-(\d*)|-(\d+))\Z")
VIDEO_RESOLUTION_PATTERN = re.compile(r"(?P<width>\d{3,5})\s*[xX×*]\s*(?P<height>\d{3,5})")
VIDEO_TARGET_DIMENSIONS = {
    VideoQuality.P1080: (1920, 1080),
    VideoQuality.P720: (1280, 720),
}
VIDEO_SOURCE_TYPE_FILE_MANAGER = 1
MAX_HLS_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_VIDEO_ERROR_BYTES = 1024 * 1024


class UgreenNasClient:
    """Minimal client for login, media search, thumbnails, and downloads.

    The class deliberately exposes no filesystem mutation operations. UGOS uses
    a private, firmware-dependent API, so protocol errors include the server's
    numeric code when it is available.
    """

    def __init__(
        self,
        host: str,
        port: int = 9999,
        *,
        scheme: str = "http",
        timeout: float = 30.0,
        verify_tls: bool = True,
        session: Optional[Session] = None,
        client_id: Optional[str] = None,
    ) -> None:
        self.base_url = self._make_base_url(host, port=port, scheme=scheme)
        self.timeout = timeout
        self.verify_tls = verify_tls
        self._session = session or requests.Session()
        self._client_id = client_id or str(uuid.uuid4())
        self._token: Optional[str] = None
        self._static_token: Optional[str] = None
        self._is_ugk = False
        self._auth_type: Optional[str] = None
        self._public_key = None

    @staticmethod
    def _make_base_url(host: str, *, port: int, scheme: str) -> str:
        if "://" not in host:
            host = "{}://{}:{}".format(scheme, host.rstrip("/"), port)
        parsed = urlsplit(host)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("host must be a hostname or an http(s) URL")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("host URL must not contain a path, query, or fragment")
        return "{}://{}".format(parsed.scheme, parsed.netloc)

    @property
    def is_authenticated(self) -> bool:
        return bool(self._token and self._public_key)

    def login(
        self,
        username: str,
        password: str,
        *,
        keepalive: bool = True,
        simple: bool = True,
    ) -> "UgreenNasClient":
        """Authenticate with a username and password.

        OTP accounts are detected but not supported by this minimum client.
        The password and session token are never placed in exception messages.
        """

        if not username or not password:
            raise ValueError("username and password must not be empty")

        check_response = self._send(
            "POST",
            "/ugreen/v1/verify/check",
            json={"username": username},
            headers=self._login_headers(),
        )
        rsa_token = check_response.headers.get("X-Rsa-Token")
        if not rsa_token:
            raise AuthenticationError("UGOS did not return a login RSA key")
        login_key = load_rsa_public_key(decode_base64(rsa_token))

        body = {
            "username": username,
            "password": rsa_encrypt_base64(login_key, password.encode("utf-8")),
            "keepalive": keepalive,
            "otp": True,
            "is_simple": simple,
        }
        login_response = self._send(
            "POST",
            "/ugreen/v1/verify/login",
            json=body,
            headers=self._login_headers(),
        )
        payload = self._response_json(login_response)
        self._check_api_result(payload, authentication=True)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            raise AuthenticationError("UGOS returned an invalid login response")

        token = data.get("token")
        if data.get("enable_otp") and not token:
            raise AuthenticationError("This UGOS account requires OTP, which is not supported")
        encoded_public_key = data.get("public_key")
        if not token or not encoded_public_key:
            raise AuthenticationError("UGOS login response did not contain session credentials")

        auth_type = str(data.get("auth_type") or "url").lower()
        if auth_type not in {"header", "url"}:
            raise AuthenticationError("UGOS returned an unsupported authentication mode")

        self._token = str(token)
        self._static_token = str(data.get("static_token") or token)
        self._is_ugk = bool(data.get("is_ugk"))
        self._auth_type = auth_type
        self._public_key = load_rsa_public_key(decode_base64(str(encoded_public_key)))
        return self

    def search(
        self,
        path: str,
        *,
        recursive: bool = True,
        types: Optional[Sequence[str]] = None,
        limit: int = 2000,
        timeout: float = 30.0,
        poll_interval: float = 0.2,
    ) -> List[UgreenFile]:
        """Search files below an absolute NAS path using a server-side task."""

        self._require_login()
        if not path.startswith("/"):
            raise ValueError("path must be an absolute NAS path")
        if limit <= 0:
            raise ValueError("limit must be positive")
        selected_types = list(types or ["all"])
        unknown = set(selected_types) - SEARCH_TYPES
        if unknown:
            raise ValueError("Unsupported search type(s): {}".format(", ".join(sorted(unknown))))

        create_data = self._post_private(
            "/ugreen/v2/filemgr/createsearchtask",
            {
                "keyword": "",
                "search_path": [path],
                "search_type": selected_types,
                "search_only": not recursive,
                "page": 1,
                "limit": limit,
                "is_stream_task": True,
            },
        )
        task_id = self._extract_task_id(create_data)

        deadline = time.monotonic() + timeout
        first_page: Optional[Mapping[str, Any]] = None
        while time.monotonic() < deadline:
            result = self._search_result(task_id, page=1, limit=limit)
            if self._search_finished(result):
                first_page = result
                break
            time.sleep(max(0.0, poll_interval))
        if first_page is None:
            raise SearchTimeoutError("UGOS search task did not finish within {:.1f}s".format(timeout))

        records = self._records(first_page)
        total = self._optional_int(first_page.get("total"))
        if total is not None:
            page_count = max(1, int(math.ceil(total / float(limit))))
            for page in range(2, page_count + 1):
                records.extend(self._records(self._search_result(task_id, page=page, limit=limit)))
        else:
            page = 2
            while len(records) >= (page - 1) * limit:
                batch = self._records(self._search_result(task_id, page=page, limit=limit))
                records.extend(batch)
                if len(batch) < limit:
                    break
                page += 1

        return [file_from_record(self, record, search_root=path) for record in records]

    def _search_result(self, task_id: str, *, page: int, limit: int) -> Mapping[str, Any]:
        data = self._post_private(
            "/ugreen/v2/filemgr/getsearchtaskresult",
            {
                "task_id": task_id,
                "page": page,
                "limit": limit,
                "reverse": False,
            },
        )
        if not isinstance(data, Mapping):
            raise ApiError("UGOS returned an invalid search result")
        return data

    @staticmethod
    def _extract_task_id(data: Any) -> str:
        if not isinstance(data, Mapping):
            raise ApiError("UGOS did not return a search task")
        result = data.get("result")
        if isinstance(result, Mapping):
            task_id = result.get("task_id") or result.get("id")
        else:
            task_id = result
        task_id = task_id or data.get("task_id")
        if not task_id:
            raise ApiError("UGOS did not return a search task id")
        return str(task_id)

    @staticmethod
    def _search_finished(data: Mapping[str, Any]) -> bool:
        # DH2300 returns retCode=0 both while the task is still running and
        # after it has completed.  When finished_at is part of the schema its
        # value, rather than retCode, is the authoritative completion signal.
        if "finished_at" in data:
            return bool(data.get("finished_at"))
        if "finished" in data:
            return data.get("finished") is True
        if "retCode" in data:
            return str(data.get("retCode")) == "0"
        return "files" in data

    @staticmethod
    def _records(data: Mapping[str, Any]) -> List[Mapping[str, Any]]:
        files = data.get("files") or []
        if not isinstance(files, list) or not all(isinstance(item, Mapping) for item in files):
            raise ApiError("UGOS returned an invalid file list")
        return list(files)

    @staticmethod
    def _optional_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _get_thumbnail(self, file: UgreenFile, *, size: ThumbnailSize) -> UgreenBinary:
        params: Dict[str, Any] = {
            "path": file.path,
            "type": 1,
            "size_type": int(size),
            # The official UI sends its display-box size here, but the server
            # selects the actual rendition using size_type.  Keep the 112px
            # hint observed in File Manager HAR captures.
            "width": 112,
            "height": 112,
            "mtime": file.mtime,
            "ctime": file.ctime,
            "file_size": file.size,
            "intranet_share_id": 0,
        }
        # The Web UI marks thumbnails as static assets.  They do not use the
        # AES-encrypted request interceptor: is_ugk sessions send static_token
        # as ugk, while older sessions send the raw API token as token.
        if self._is_ugk:
            params["ugk"] = self._static_token
        else:
            params["token"] = self._token
        headers = self._client_headers()
        headers["Thumb-ID"] = uuid.uuid4().hex
        return self._get_direct_binary("/ugreen/v1/filemgr/thumbnail", params, headers=headers)

    def _get_media_info(self, file: UgreenFile) -> UgreenMediaInfo:
        """Get the metadata shown by the UGOS Web File Manager detail panel."""

        payload = self._private_request(
            "GET",
            "/ugreen/v1/filemgr/getMediaInfo",
            params={"path": file.path},
        )
        self._check_api_result(payload)
        data = payload.get("data") if isinstance(payload, Mapping) else None
        if not isinstance(data, Mapping):
            raise ApiError("UGOS returned invalid media information")
        return media_info_from_record(data)

    def _open_download_stream(
        self,
        file: UgreenFile,
        *,
        range_header: Optional[str],
    ) -> UgreenDownloadStream:
        """Open UGOS' direct download endpoint without buffering its body."""

        self._require_login()
        headers = self._client_headers()
        if range_header is not None:
            headers["Range"] = self._validate_range_header(range_header)

        response = self._send_stream(
            "GET",
            "/ugreen/v1/filemgr/downloadFile",
            params={"paths": file.path, "token": self._token, "coding": "true"},
            headers=headers,
        )
        content_type = (response.headers.get("Content-Type") or "").lower()

        if response.status_code != 416 and "json" in content_type:
            try:
                payload = response.json()
            except ValueError:
                raise TransportError("UGOS returned an invalid JSON download response") from None
            finally:
                response.close()
            self._check_api_result(payload)
            raise ApiError("UGOS returned JSON instead of a download", payload=payload)

        if response.status_code not in {200, 206, 416}:
            status_code = response.status_code
            response.close()
            raise TransportError(
                "UGOS download request failed (HTTP {})".format(status_code)
            ) from None

        return UgreenDownloadStream(response)

    def _open_video_playback(
        self,
        file: UgreenFile,
        *,
        quality: VideoQuality,
        preparation_timeout: float,
    ) -> UgreenHlsPlayback:
        """Prepare one quality-specific UGOS HLS media playlist."""

        self._require_login()
        if quality not in VIDEO_TARGET_DIMENSIONS:
            raise ValueError("open_video_playback() requires a transcoded quality")
        self._validate_preparation_timeout(preparation_timeout)

        deadline = time.monotonic() + preparation_timeout
        task_id = self._new_video_task_id()
        src_type = VIDEO_SOURCE_TYPE_FILE_MANAGER
        heartbeat: Optional[UgreenPlaybackHeartbeat] = None
        try:
            heartbeat = self._start_video_heartbeat(
                task_id,
                timeout=self._remaining_preparation_time(deadline),
            )
            info = self._get_video_info(
                file,
                task_id=task_id,
                src_type=src_type,
                timeout=self._remaining_preparation_time(deadline),
            )
            record = self._record_for_video_quality(info, quality)
            if record is None:
                raise VideoQualityUnavailableError(
                    "UGOS cannot provide the requested {} rendition".format(quality.value)
                )

            original_dimensions = self._video_dimensions(info.get("resolution"))
            selected_dimensions = self._video_dimensions(record)
            is_transcoded = not (
                original_dimensions is not None and selected_dimensions == original_dimensions
            )
            m3u8_file = self._video_manifest_name(info, record)
            audio_index = self._default_audio_index(info)
            params: Dict[str, Any] = {
                "m3u8_file": m3u8_file,
                "regen": 1,
                "subtitle_index": -1,
                "audio_index": audio_index,
                # Force the broadly compatible rendition observed on DH2300.
                "prefer_h265": "false",
            }
            params.update(self._video_session_params(task_id, src_type=src_type))
            self._get_video_play(
                "/ugreen/v2/stream/transcode/web/play",
                params,
                timeout=self._remaining_preparation_time(deadline),
            )
            manifest = self._get_video_manifest(
                m3u8_file,
                task_id=task_id,
                src_type=src_type,
                timeout=self._remaining_preparation_time(deadline),
            )
            self._remaining_preparation_time(deadline)
            return UgreenHlsPlayback(
                self,
                task_id=task_id,
                src_type=src_type,
                manifest=manifest,
                requested_quality=quality,
                actual_quality=quality,
                is_transcoded=is_transcoded,
                heartbeat=heartbeat,
            )
        except BaseException as exc:
            self._close_video_session(task_id, src_type=src_type)
            if heartbeat is not None:
                heartbeat.close()
            if isinstance(exc, TransportError) and time.monotonic() >= deadline:
                raise VideoPreparationTimeoutError("UGOS video preparation timed out") from None
            raise

    def _get_video_qualities(
        self,
        file: UgreenFile,
        *,
        preparation_timeout: float,
    ) -> List[VideoVariant]:
        """Return supported PyUGOS renditions from UGOS' transcode info API."""

        self._require_login()
        self._validate_preparation_timeout(preparation_timeout)
        deadline = time.monotonic() + preparation_timeout
        task_id = self._new_video_task_id()
        src_type = VIDEO_SOURCE_TYPE_FILE_MANAGER
        heartbeat: Optional[UgreenPlaybackHeartbeat] = None
        try:
            heartbeat = self._start_video_heartbeat(
                task_id,
                timeout=self._remaining_preparation_time(deadline),
            )
            info = self._get_video_info(
                file,
                task_id=task_id,
                src_type=src_type,
                timeout=self._remaining_preparation_time(deadline),
            )
            original_dimensions = self._video_dimensions(info.get("resolution"))
            variants: List[VideoVariant] = []
            for quality in (VideoQuality.P1080, VideoQuality.P720):
                record = self._record_for_video_quality(info, quality)
                if record is None:
                    continue
                dimensions = self._video_dimensions(record)
                width, height = dimensions or (None, None)
                variants.append(
                    VideoVariant(
                        quality=quality,
                        width=width,
                        height=height,
                        transcoded=not (
                            original_dimensions is not None and dimensions == original_dimensions
                        ),
                    )
                )
            original_width, original_height = original_dimensions or (None, None)
            variants.append(
                VideoVariant(
                    quality=VideoQuality.ORIGINAL,
                    width=original_width,
                    height=original_height,
                    transcoded=False,
                )
            )
            return variants
        except TransportError:
            if time.monotonic() >= deadline:
                raise VideoPreparationTimeoutError("UGOS video preparation timed out") from None
            raise
        finally:
            self._close_video_session(task_id, src_type=src_type)
            if heartbeat is not None:
                heartbeat.close()

    @staticmethod
    def _validate_preparation_timeout(value: float) -> None:
        if not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
            raise ValueError("preparation_timeout must be a positive finite number")

    @staticmethod
    def _remaining_preparation_time(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise VideoPreparationTimeoutError("UGOS video preparation timed out")
        return remaining

    @staticmethod
    def _new_video_task_id() -> str:
        value = "PC&{}&{}".format(uuid.uuid4(), int(time.time() * 1000))
        return hashlib.md5(value.encode("ascii")).hexdigest()

    @staticmethod
    def _video_session_params(task_id: str, *, src_type: int) -> Dict[str, Any]:
        return {
            "task_id": task_id,
            "timestamp": str(int(time.time() * 1000)),
            "src_type": src_type,
        }

    def _start_video_heartbeat(
        self,
        task_id: str,
        *,
        timeout: float,
    ) -> UgreenPlaybackHeartbeat:
        assert self._token is not None
        token_name = "ugk" if self._is_ugk else "token"
        # UGOS accepts static_token for thumbnails, but the transcode
        # WebSocket authenticates with the active login session token even
        # when the login response reports is_ugk=true.
        token = self._token
        assert token is not None
        heartbeat = UgreenPlaybackHeartbeat(
            websocket_url(self.base_url, token_name=token_name, token=token),
            task_id=task_id,
            timeout=timeout,
            verify_tls=self.verify_tls,
        )
        heartbeat.start()
        return heartbeat

    def _get_video_info(
        self,
        file: UgreenFile,
        *,
        task_id: str,
        src_type: int,
        timeout: float,
    ) -> Mapping[str, Any]:
        body: Dict[str, Any] = {
            "video_path": file.path,
            "is_net_disk_direct": False,
        }
        body.update(self._video_session_params(task_id, src_type=src_type))
        try:
            data = self._post_private(
                "/ugreen/v2/stream/transcode/web/info",
                body,
                request_timeout=timeout,
            )
        except ApiError as exc:
            raise ApiError(
                "UGOS video information request failed",
                code=self._safe_video_error_code(exc.code),
            ) from None
        if not isinstance(data, Mapping):
            raise ApiError("UGOS returned invalid video information")
        return data

    def _get_video_play(
        self,
        path: str,
        params: Mapping[str, Any],
        *,
        timeout: float,
    ) -> Any:
        """Start playback through UGOS' direct token-authenticated GET API."""

        self._require_login()
        assert self._token is not None
        wire_params = dict(params)
        wire_params["token"] = self._token
        try:
            response = self._send(
                "GET",
                path,
                params=wire_params,
                headers=self._client_headers(),
                timeout=timeout,
            )
            payload = self._response_json(response)
            self._check_api_result(payload)
        except ApiError as exc:
            raise ApiError(
                "UGOS video playback request failed",
                code=self._safe_video_error_code(exc.code),
            ) from None
        return payload.get("data") if isinstance(payload, Mapping) else None

    @staticmethod
    def _safe_video_error_code(value: Any) -> Optional[Any]:
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit() and len(value) <= 12:
            return value
        return None

    @classmethod
    def _record_for_video_quality(
        cls,
        info: Mapping[str, Any],
        quality: VideoQuality,
    ) -> Optional[Mapping[str, Any]]:
        target = VIDEO_TARGET_DIMENSIONS[quality]
        records = info.get("transcodeable")
        if not isinstance(records, list):
            return None
        fallback: Optional[Mapping[str, Any]] = None
        for record in records:
            if not isinstance(record, Mapping):
                continue
            dimensions = cls._video_dimensions(record)
            if dimensions == target:
                return record
            if dimensions is not None and dimensions[1] == target[1]:
                fallback = record
        return fallback

    @classmethod
    def _video_dimensions(cls, value: Any) -> Optional[Tuple[int, int]]:
        if isinstance(value, Mapping):
            width = cls._optional_int(value.get("width"))
            height = cls._optional_int(value.get("height"))
            if width and height:
                return width, height
            for key in ("resolution", "name", "label"):
                dimensions = cls._video_dimensions(value.get(key))
                if dimensions is not None:
                    return dimensions
            return None
        if value is None:
            return None
        text = str(value).strip().lower()
        match = VIDEO_RESOLUTION_PATTERN.search(text)
        if match is not None:
            return int(match.group("width")), int(match.group("height"))
        aliases = {
            "4k": (3840, 2160),
            "2160p": (3840, 2160),
            "1080p": (1920, 1080),
            "720p": (1280, 720),
        }
        return aliases.get(text)

    @classmethod
    def _video_manifest_name(
        cls,
        info: Mapping[str, Any],
        record: Mapping[str, Any],
    ) -> str:
        redir_link = info.get("redir_link")
        parsed_query = parse_qs(urlsplit(str(redir_link or "")).query)
        video_names = parsed_query.get("video_name") or []
        video_name = str(video_names[0]) if video_names else str(info.get("video_name") or "")
        resolution = record.get("resolution")
        if not video_name or not resolution:
            raise ApiError("UGOS video information did not contain a playback name")
        return "{}_{}.m3u8".format(video_name, resolution)

    @staticmethod
    def _default_audio_index(info: Mapping[str, Any]) -> Any:
        audios = info.get("audios")
        if not isinstance(audios, list) or not audios:
            return -1
        records = [item for item in audios if isinstance(item, Mapping)]
        selected = next(
            (item for item in records if item.get("isDefault") or item.get("is_default")),
            records[0] if records else None,
        )
        return selected.get("id", -1) if selected is not None else -1

    def _get_video_manifest(
        self,
        m3u8_file: str,
        *,
        task_id: str,
        src_type: int,
        timeout: float,
    ) -> str:
        assert self._token is not None
        params: Dict[str, Any] = {
            "m3u8_file": m3u8_file,
            "token": self._token,
        }
        params.update(self._video_session_params(task_id, src_type=src_type))
        response = self._send_stream(
            "GET",
            "/ugreen/v2/stream/transcode/web/m3u8",
            params=params,
            headers=self._client_headers(),
            timeout=timeout,
        )
        content_type = (response.headers.get("Content-Type") or "").lower()
        body = self._read_small_stream(
            response,
            limit=MAX_HLS_MANIFEST_BYTES,
            error_message="UGOS returned an oversized HLS manifest",
        )
        if response.status_code != 200:
            self._raise_video_response_error(body, content_type=content_type)
            raise TransportError(
                "UGOS HLS manifest request failed (HTTP {})".format(response.status_code)
            ) from None
        if "json" in content_type or body.lstrip().startswith((b"{", b"[")):
            self._raise_video_response_error(body, content_type=content_type)
        if "mpegurl" not in content_type and "m3u8" not in content_type:
            raise TransportError("UGOS returned an unexpected HLS manifest type")
        try:
            return body.decode("utf-8")
        except UnicodeDecodeError:
            raise TransportError("UGOS returned a non-UTF-8 HLS manifest") from None

    def _open_video_segment(
        self,
        path: str,
        *,
        params: Mapping[str, str],
        on_close: Callable[[UgreenDownloadStream], None],
    ) -> UgreenDownloadStream:
        self._require_login()
        response = self._send_stream(
            "GET",
            path,
            params=dict(params),
            headers=self._client_headers(),
        )
        content_type = (response.headers.get("Content-Type") or "").lower()
        if response.status_code != 200 or "json" in content_type:
            body = self._read_small_stream(
                response,
                limit=MAX_VIDEO_ERROR_BYTES,
                error_message="UGOS returned an oversized HLS segment error",
            )
            self._raise_video_response_error(body, content_type=content_type)
            raise TransportError(
                "UGOS HLS segment request failed (HTTP {})".format(response.status_code)
            ) from None

        iterator = iter(response.iter_content(chunk_size=64 * 1024))
        try:
            first_chunk = next((chunk for chunk in iterator if chunk), b"")
        except RequestException:
            response.close()
            raise TransportError("UGOS HLS segment request was interrupted") from None

        if first_chunk.lstrip().startswith((b"{", b"[")):
            body = self._read_iterator(
                first_chunk,
                iterator,
                response=response,
                limit=MAX_VIDEO_ERROR_BYTES,
            )
            self._raise_video_response_error(body, content_type=content_type)

        content_iterator: Iterable[bytes] = itertools.chain((first_chunk,), iterator)
        return UgreenDownloadStream(
            response,
            content_iterator=content_iterator,
            on_close=on_close,
        )

    @staticmethod
    def _read_small_stream(
        response: Response,
        *,
        limit: int,
        error_message: str,
    ) -> bytes:
        content_length = UgreenNasClient._optional_int(response.headers.get("Content-Length"))
        if content_length is not None and content_length > limit:
            response.close()
            raise TransportError(error_message)
        chunks: List[bytes] = []
        size = 0
        try:
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    raise TransportError(error_message)
                chunks.append(chunk)
        except RequestException:
            raise TransportError("UGOS video response was interrupted") from None
        finally:
            response.close()
        return b"".join(chunks)

    @staticmethod
    def _read_iterator(
        first_chunk: bytes,
        iterator: Iterable[bytes],
        *,
        response: Response,
        limit: int,
    ) -> bytes:
        chunks = [first_chunk]
        size = len(first_chunk)
        try:
            for chunk in iterator:
                size += len(chunk)
                if size > limit:
                    raise TransportError("UGOS returned an oversized HLS segment error")
                chunks.append(chunk)
        except RequestException:
            raise TransportError("UGOS HLS segment error response was interrupted") from None
        finally:
            response.close()
        return b"".join(chunks)

    @classmethod
    def _raise_video_response_error(cls, body: bytes, *, content_type: str) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            if "json" in content_type or body.lstrip().startswith((b"{", b"[")):
                raise TransportError("UGOS returned an invalid JSON video response") from None
            return
        try:
            cls._check_api_result(payload)
        except ApiError as exc:
            raise ApiError(
                "UGOS video playback API failed",
                code=cls._safe_video_error_code(exc.code),
            ) from None
        raise ApiError("UGOS returned JSON instead of video data")

    def _close_video_session(self, task_id: str, *, src_type: int) -> None:
        """Best-effort equivalent of the Web UI's close beacon."""

        if not self.is_authenticated or self._token is None:
            return
        body = self._video_session_params(task_id, src_type=src_type)
        headers = self._client_headers()
        headers["Content-Type"] = "text/plain;charset=UTF-8"
        try:
            response = self._send(
                "POST",
                "/ugreen/v2/stream/transcode/web/close",
                params={"token": self._token},
                data=json.dumps(body, separators=(",", ":")),
                headers=headers,
                timeout=min(self.timeout, 2.0),
            )
            response.close()
        except (ApiError, TransportError):
            pass

    @staticmethod
    def _validate_range_header(value: str) -> str:
        match = SINGLE_RANGE_PATTERN.fullmatch(value)
        if match is None:
            raise ValueError(
                "range_header must be one range such as 'bytes=0-1023', "
                "'bytes=1024-', or 'bytes=-1024'"
            )
        start, end, suffix = match.groups()
        if suffix is not None and int(suffix) == 0:
            raise ValueError("A suffix byte range must be greater than zero")
        if start is not None and end and int(end) < int(start):
            raise ValueError("The range end must not be smaller than its start")
        return value

    def _post_private(
        self,
        path: str,
        body: Mapping[str, Any],
        *,
        request_timeout: Optional[float] = None,
    ) -> Any:
        payload = self._private_request(
            "POST",
            path,
            body=body,
            request_timeout=request_timeout,
        )
        self._check_api_result(payload)
        return payload.get("data") if isinstance(payload, Mapping) else None

    def _get_private(
        self,
        path: str,
        params: Mapping[str, Any],
        *,
        request_timeout: Optional[float] = None,
    ) -> Any:
        payload = self._private_request(
            "GET",
            path,
            public_params=params,
            request_timeout=request_timeout,
        )
        self._check_api_result(payload)
        return payload.get("data") if isinstance(payload, Mapping) else None

    def _get_direct_binary(
        self,
        path: str,
        params: Mapping[str, Any],
        *,
        headers: Mapping[str, str],
    ) -> UgreenBinary:
        self._require_login()
        response = self._send("GET", path, params=params, headers=dict(headers))
        content_type = response.headers.get("Content-Type")
        looks_json = "json" in (content_type or "").lower() or response.content.lstrip().startswith(b"{")
        if looks_json:
            payload = self._response_json(response)
            self._check_api_result(payload)
            raise ApiError("UGOS returned JSON instead of the requested file", payload=payload)
        return UgreenBinary(response.content, content_type)

    def _private_request(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        public_params: Optional[Mapping[str, Any]] = None,
        request_timeout: Optional[float] = None,
    ) -> Any:
        response, aes_key = self._send_private(
            method,
            path,
            body=body,
            params=params,
            public_params=public_params,
            request_timeout=request_timeout,
        )
        return self._decode_private_json(response, aes_key)

    def _send_private(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
        public_params: Optional[Mapping[str, Any]] = None,
        request_timeout: Optional[float] = None,
    ):
        self._require_login()
        assert self._token is not None
        assert self._auth_type is not None
        assert self._public_key is not None

        aes_key = uuid.uuid4().hex.encode("ascii")
        headers = self._client_headers()
        headers.update(
            {
                "X-Ugreen-Security-Key": hashlib.md5(self._token.encode("utf-8")).hexdigest(),
                "X-Ugreen-Security-Code": rsa_encrypt_base64(self._public_key, aes_key),
            }
        )
        query: MutableMapping[str, Any] = dict(params or {})
        if self._auth_type == "header":
            headers["X-Ugreen-Token"] = rsa_encrypt_base64(
                self._public_key, self._token.encode("utf-8")
            )
        else:
            query["token"] = self._token

        request_kwargs: Dict[str, Any] = {"headers": headers}
        if request_timeout is not None:
            request_kwargs["timeout"] = request_timeout
        if method.upper() == "POST":
            request_kwargs["json"] = encrypt_request_body(aes_key, body or {})
            if query:
                request_kwargs["params"] = encrypt_query(aes_key, query)
        elif method.upper() == "GET":
            wire_params: Dict[str, Any] = dict(public_params or {})
            wire_params.update(encrypt_query(aes_key, query))
            request_kwargs["params"] = wire_params
        else:
            raise ValueError("Only read-only GET and search-related POST requests are supported")

        return self._send(method, path, **request_kwargs), aes_key

    def _decode_private_json(self, response: Response, aes_key: bytes) -> Any:
        payload = self._response_json(response)
        if isinstance(payload, Mapping) and payload.get("encrypt_resp_body"):
            return decrypt_json(aes_key, str(payload["encrypt_resp_body"]))
        return payload

    def _send(self, method: str, path: str, **kwargs: Any) -> Response:
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self.verify_tls)
        try:
            response = self._session.request(method, self.base_url + path, **kwargs)
            response.raise_for_status()
            return response
        except RequestException as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            detail = " (HTTP {})".format(status) if status is not None else ""
            # Direct binary URLs contain a session token.  Suppress the
            # requests exception chain so an unhandled traceback cannot print
            # the prepared URL and leak that credential.
            raise TransportError("UGOS request failed{}".format(detail)) from None

    def _send_stream(self, method: str, path: str, **kwargs: Any) -> Response:
        kwargs.setdefault("timeout", self.timeout)
        kwargs.setdefault("verify", self.verify_tls)
        kwargs["stream"] = True
        try:
            return self._session.request(method, self.base_url + path, **kwargs)
        except RequestException:
            # The prepared URL contains the raw API token.
            raise TransportError("UGOS download request failed") from None

    @staticmethod
    def _response_json(response: Response) -> Any:
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise TransportError("UGOS returned a non-JSON API response") from exc

    @staticmethod
    def _check_api_result(payload: Any, *, authentication: bool = False) -> None:
        if not isinstance(payload, Mapping):
            error = AuthenticationError if authentication else ApiError
            raise error("UGOS returned an invalid API response")
        code = payload.get("code")
        if code is None:
            code = payload.get("status")
        if code is None or str(code) in {"0", "200", "2000"}:
            return
        message = payload.get("msg") or payload.get("message") or "UGOS API request failed"
        error = AuthenticationError if authentication else ApiError
        raise error(str(message), code=code, payload=payload)

    def _require_login(self) -> None:
        if not self.is_authenticated:
            raise AuthenticationError("Call login() before using the UGOS private API")

    def _client_headers(self) -> Dict[str, str]:
        return {
            "Accept": "application/json, text/plain, */*",
            "Client-Id": self._client_id,
            "Client-Version": "1.0.0",
            "UG-Agent": "web",
            "UG-Client-Id": self._client_id,
            "X-Specify-Language": "en-US",
        }

    def _login_headers(self) -> Dict[str, str]:
        headers = self._client_headers()
        headers["Content-Type"] = "application/json"
        return headers
