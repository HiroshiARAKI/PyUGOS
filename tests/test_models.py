from pathlib import Path

import pytest

from pyugos.models import ThumbnailSize, UgreenBinary, file_from_record


class StubClient:
    pass


def test_thumbnail_size_values_match_ugos_size_type():
    assert ThumbnailSize.MEDIUM == 1
    assert ThumbnailSize.SMALL == 2
    assert ThumbnailSize.LARGE == 3


def test_binary_save(tmp_path: Path):
    binary = UgreenBinary(b"image", "image/webp")
    destination = tmp_path / "nested" / "thumb.webp"
    assert binary.save(destination) == destination
    assert destination.read_bytes() == b"image"


def test_file_record_accepts_observed_aliases():
    file = file_from_record(
        StubClient(),  # type: ignore[arg-type]
        {
            "file_name": "photo.JPG",
            "file_path": "/home/user/Photos/photo.JPG",
            "file_size": "42",
            "modify_time": "100.9",
            "create_time": 90,
            "is_dir": 0,
        },
        search_root="/home/user/Photos",
    )
    assert file.name == "photo.JPG"
    assert file.extension == "jpg"
    assert file.size == 42
    assert file.mtime == 100
    assert not file.is_directory


def test_directory_has_no_thumbnail():
    file = file_from_record(
        StubClient(),  # type: ignore[arg-type]
        {"name": "album", "path": "/album", "is_dir": True},
        search_root="/",
    )
    with pytest.raises(ValueError, match="Directories"):
        file.get_thumbnail()
