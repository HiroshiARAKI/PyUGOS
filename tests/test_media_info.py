import base64

import pytest
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from pyugos import ApiError, UgreenMediaInfo, UgreenNasClient
from pyugos._crypto import decrypt_bytes, encrypt_bytes, json_stringify, stringify_query
from pyugos.models import file_from_record, media_info_from_record


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.headers = {"Content-Type": "application/json"}
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class EncryptedMediaInfoSession:
    def __init__(self, private_key, payload):
        self.private_key = private_key
        self.payload = payload
        self.calls = []
        self.aes_key = None

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        self.aes_key = self.private_key.decrypt(
            base64.b64decode(kwargs["headers"]["X-Ugreen-Security-Code"]),
            padding.PKCS1v15(),
        )
        return FakeResponse(
            {
                "encrypt_resp_body": encrypt_bytes(
                    self.aes_key,
                    json_stringify(self.payload),
                )
            }
        )


def media_info_client(payload):
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    session = EncryptedMediaInfoSession(private_key, payload)
    client = UgreenNasClient("nas.local", session=session)  # type: ignore[arg-type]
    client._token = "unit-test-token"
    client._static_token = "unit-test-token"
    client._auth_type = "header"
    client._public_key = private_key.public_key()
    return client, session


def test_file_get_media_info_uses_encrypted_path_query_and_maps_fields():
    data = {
        "file_collation": "2026-08",
        "width": 3840,
        "height": 2160,
        "duration": 12.5,
        "bit_rate": "120 Mb/s",
        "channel": "stereo",
        "device": "Camera",
        "software": "Firmware 1",
        "color_space": "BT.2020",
        "resolution": "3840x2160",
        "shoot_time": 1787958000,
        "frame_rate": 59.94,
        "video_format": "H.265",
        "hdr": True,
        "iso": "100",
        "aperture": "f/2.8",
        "shutter_speed": "1/120",
        "focal_length": "24 mm",
        "resolution_type": 4,
        "firmware_extra": "preserved",
    }
    client, session = media_info_client({"code": 200, "data": data})
    file = file_from_record(
        client,
        {"name": "clip.mov", "path": "/home/user/Videos/clip.mov"},
        search_root="/home/user/Videos",
    )

    info = file.get_media_info()

    assert isinstance(info, UgreenMediaInfo)
    assert (info.width, info.height, info.duration) == (3840, 2160, 12.5)
    assert info.frame_rate == 59.94
    assert info.video_format == "H.265"
    assert info.hdr is True
    assert info.raw["firmware_extra"] == "preserved"
    assert "firmware_extra" not in repr(info)

    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url.endswith("/ugreen/v1/filemgr/getMediaInfo")
    assert set(kwargs["params"]) == {"encrypt_query"}
    assert file.path not in str(kwargs["params"])
    assert session.aes_key is not None
    assert decrypt_bytes(session.aes_key, kwargs["params"]["encrypt_query"]).decode() == (
        stringify_query({"path": file.path})
    )
    assert kwargs["headers"]["X-Ugreen-Token"] != "unit-test-token"


def test_media_info_ignores_known_fields_with_unexpected_types():
    info = media_info_from_record(
        {
            "width": "3840",
            "duration": True,
            "hdr": 1,
            "video_format": ["H.265"],
        }
    )

    assert info.width is None
    assert info.duration is None
    assert info.hdr is None
    assert info.video_format is None


def test_media_info_rejects_invalid_api_data():
    client, _ = media_info_client({"code": 200, "data": []})
    file = file_from_record(
        client,
        {"name": "clip.mov", "path": "/clip.mov"},
        search_root="/",
    )

    with pytest.raises(ApiError, match="invalid media information"):
        file.get_media_info()


def test_media_info_propagates_ugos_api_errors():
    client, _ = media_info_client(
        {"code": 1404, "msg": "Media information is unavailable"}
    )
    file = file_from_record(
        client,
        {"name": "notes.txt", "path": "/notes.txt"},
        search_root="/",
    )

    with pytest.raises(ApiError) as caught:
        file.get_media_info()

    assert caught.value.code == 1404


def test_directory_media_info_is_rejected_before_request():
    client, session = media_info_client({"code": 200, "data": {}})
    directory = file_from_record(
        client,
        {"name": "Videos", "path": "/Videos", "is_dir": True},
        search_root="/",
    )

    with pytest.raises(ValueError, match="Directories"):
        directory.get_media_info()

    assert session.calls == []
