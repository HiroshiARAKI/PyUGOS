"""Read-only UGOS Pro NAS client."""

import hashlib
import json
import math
import re
import time
import uuid
from typing import Any, Dict, List, Mapping, MutableMapping, Optional, Sequence
from urllib.parse import urlsplit

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
from .errors import ApiError, AuthenticationError, SearchTimeoutError, TransportError
from .models import ThumbnailSize, UgreenBinary, UgreenFile, file_from_record
from .streams import UgreenDownloadStream


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

    def _post_private(self, path: str, body: Mapping[str, Any]) -> Any:
        payload = self._private_request("POST", path, body=body)
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
    ) -> Any:
        response, aes_key = self._send_private(method, path, body=body, params=params)
        return self._decode_private_json(response, aes_key)

    def _send_private(
        self,
        method: str,
        path: str,
        *,
        body: Optional[Mapping[str, Any]] = None,
        params: Optional[Mapping[str, Any]] = None,
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
        if method.upper() == "POST":
            request_kwargs["json"] = encrypt_request_body(aes_key, body or {})
            if query:
                request_kwargs["params"] = encrypt_query(aes_key, query)
        elif method.upper() == "GET":
            request_kwargs["params"] = encrypt_query(aes_key, query)
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
