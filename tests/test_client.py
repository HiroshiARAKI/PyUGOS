import base64
import json
from typing import Any, Dict, List

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pyugos import ThumbnailSize, UgreenNasClient
from pyugos._crypto import decrypt_bytes, encrypt_bytes, stringify_query


class FakeResponse:
    def __init__(self, payload: Any, *, headers: Dict[str, str] = None):
        self._payload = payload
        self.headers = headers or {"Content-Type": "application/json"}
        self.content = json.dumps(payload).encode()
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeBinaryResponse:
    def __init__(self, content: bytes, content_type: str):
        self.content = content
        self.headers = {"Content-Type": content_type}
        self.status_code = 200
        self.closed = False

    def json(self):
        raise ValueError("binary response")

    def raise_for_status(self):
        return None

    def iter_content(self, chunk_size):
        yield self.content

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses: List[FakeResponse]):
        self.responses = responses
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)


def public_pem(private_key) -> bytes:
    return private_key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def test_login_encrypts_password_and_search_maps_files():
    login_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    api_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    check_key = base64.b64encode(public_pem(login_private)).decode()
    api_key = base64.b64encode(public_pem(api_private)).decode()
    session = FakeSession(
        [
            FakeResponse({}, headers={"X-Rsa-Token": check_key}),
            FakeResponse(
                {
                    "code": 200,
                    "data": {
                        "token": "raw-token",
                        "static_token": "static-token",
                        "is_ugk": True,
                        "auth_type": "header",
                        "public_key": api_key,
                    },
                }
            ),
            FakeResponse({"code": 200, "data": {"result": "task-1"}}),
            FakeResponse(
                {
                    "code": 200,
                    "data": {
                        "files": [],
                        "total": 0,
                        "retCode": 0,
                        "finished_at": None,
                    },
                }
            ),
            FakeResponse(
                {
                    "code": 200,
                    "data": {
                        "files": [
                            {
                                "name": "photo.jpg",
                                "path": "/home/user/Photos/photo.jpg",
                                "size": 123,
                                "mtime": 12,
                                "ctime": 10,
                                "is_dir": False,
                            }
                        ],
                        "total": 1,
                        "retCode": 0,
                        "finished_at": "2026-08-28T10:00:00+09:00",
                    },
                }
            ),
            FakeBinaryResponse(b"thumbnail", "image/webp"),
            FakeBinaryResponse(b"original", "image/jpeg"),
        ]
    )
    client = UgreenNasClient("nas.local", session=session, client_id="test-client")  # type: ignore[arg-type]
    client.login("user", "pāssword")

    encrypted_password = session.calls[1][2]["json"]["password"]
    assert login_private.decrypt(base64.b64decode(encrypted_password), padding.PKCS1v15()) == "pāssword".encode()

    files = client.search("/home/user/Photos", types=["image"])
    assert [file.name for file in files] == ["photo.jpg"]
    assert files[0].size == 123

    create_call = session.calls[2][2]
    aes_key = api_private.decrypt(
        base64.b64decode(create_call["headers"]["X-Ugreen-Security-Code"]),
        padding.PKCS1v15(),
    )
    wire_body = create_call["json"]
    plaintext = json.loads(decrypt_bytes(aes_key, wire_body["encrypt_req_body"]))
    assert plaintext["search_path"] == ["/home/user/Photos"]
    assert plaintext["search_type"] == ["image"]
    assert plaintext["search_only"] is False

    thumbnail = files[0].get_thumbnail(ThumbnailSize.SMALL)
    assert bytes(thumbnail) == b"thumbnail"
    thumbnail_params = session.calls[5][2]["params"]
    assert thumbnail_params["ugk"] == "static-token"
    assert thumbnail_params["size_type"] == 2
    assert thumbnail_params["width"] == 112
    assert thumbnail_params["height"] == 112
    assert "encrypt_query" not in thumbnail_params

    assert files[0].download() == b"original"
    download_params = session.calls[6][2]["params"]
    assert download_params == {
        "paths": "/home/user/Photos/photo.jpg",
        "token": "raw-token",
        "coding": "true",
    }


def test_search_completion_prefers_finished_at_over_ret_code():
    assert not UgreenNasClient._search_finished(
        {"files": [], "total": 0, "retCode": 0, "finished_at": None}
    )
    assert UgreenNasClient._search_finished(
        {"files": [], "total": 0, "retCode": 0, "finished_at": "2026-08-28T10:00:00+09:00"}
    )


def test_url_auth_mode_encrypts_token_in_post_query():
    api_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    session = FakeSession([FakeResponse({"code": 200, "data": {"ok": True}})])
    client = UgreenNasClient("nas.local", session=session)  # type: ignore[arg-type]
    client._token = "raw-token"
    client._static_token = "raw-token"
    client._auth_type = "url"
    client._public_key = api_private.public_key()

    assert client._post_private("/ugreen/v2/example", {"read": True}) == {"ok": True}

    call = session.calls[0][2]
    aes_key = api_private.decrypt(
        base64.b64decode(call["headers"]["X-Ugreen-Security-Code"]),
        padding.PKCS1v15(),
    )
    encrypted_query = call["params"]["encrypt_query"]
    assert decrypt_bytes(aes_key, encrypted_query).decode() == stringify_query(
        {"token": "raw-token"}
    )
    assert "X-Ugreen-Token" not in call["headers"]


def test_private_get_keeps_video_params_visible_and_encrypts_only_auth_query():
    api_private = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    class DynamicSession:
        def __init__(self):
            self.calls = []
            self.aes_key = None

        def request(self, method, url, **kwargs):
            self.calls.append((method, url, kwargs))
            self.aes_key = api_private.decrypt(
                base64.b64decode(kwargs["headers"]["X-Ugreen-Security-Code"]),
                padding.PKCS1v15(),
            )
            payload = {
                "encrypt_resp_body": encrypt_bytes(
                    self.aes_key,
                    b'{"code":200,"data":{"ok":true}}',
                )
            }
            return FakeResponse(payload)

    session = DynamicSession()
    client = UgreenNasClient("nas.local", session=session)  # type: ignore[arg-type]
    client._token = "raw-token"
    client._static_token = "raw-token"
    client._auth_type = "header"
    client._public_key = api_private.public_key()

    result = client._get_private(
        "/ugreen/v2/stream/transcode/web/play",
        {
            "m3u8_file": "video.mov_1920x1080.m3u8",
            "prefer_h265": "false",
            "task_id": "task-id",
        },
    )

    assert result == {"ok": True}
    params = session.calls[0][2]["params"]
    assert params["m3u8_file"] == "video.mov_1920x1080.m3u8"
    assert params["prefer_h265"] == "false"
    assert params["task_id"] == "task-id"
    assert session.aes_key is not None
    assert decrypt_bytes(session.aes_key, params["encrypt_query"]) == b""
    assert "token" not in params
