"""Download every file and available thumbnail below a UGOS directory.

Example 1:
    export PYUGOS_HOST="192.168.1.100"
    export PYUGOS_USERNAME="your-name"
    export PYUGOS_PASSWORD="your-password"
    python example/download_tree.py /home/your-name/Photos ./download

Example 2:
    python example/download_tree.py --host="xxx.local" --port=9999 --username=your-name /home/your-name/Photos/yyy/zzz ./download 

If PYUGOS_PASSWORD is not set, the script asks for it without echoing input.
Thumbnails are saved below ``<destination>/thumbnails`` while preserving the
same relative directory tree as the originals.
"""

import argparse
import getpass
import os
from pathlib import Path, PurePosixPath
from typing import Optional, Sequence

from pyugos import ApiError, ThumbnailSize, UgreenNasClient


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download all files below a UGOS directory recursively."
    )
    parser.add_argument("remote_path", help="Absolute directory path on the NAS")
    parser.add_argument("destination", type=Path, help="Local destination directory")
    parser.add_argument(
        "--host",
        default=os.environ.get("PYUGOS_HOST"),
        help="NAS hostname (or set PYUGOS_HOST)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PYUGOS_PORT", "9999")),
        help="NAS HTTP port (default: 9999)",
    )
    parser.add_argument(
        "--username",
        default=os.environ.get("PYUGOS_USERNAME"),
        help="UGOS username (or set PYUGOS_USERNAME)",
    )
    args = parser.parse_args(argv)
    if not args.host:
        parser.error("--host or PYUGOS_HOST is required")
    if not args.username:
        parser.error("--username or PYUGOS_USERNAME is required")
    if not args.remote_path.startswith("/"):
        parser.error("remote_path must be an absolute NAS path")
    return args


def destination_for(
    remote_root: PurePosixPath,
    remote_file: PurePosixPath,
    local_root: Path,
) -> Path:
    """Map a NAS path below remote_root to a safe local path."""

    try:
        relative = remote_file.relative_to(remote_root)
    except ValueError as exc:
        raise ValueError("Search returned a file outside the requested directory") from exc
    if not relative.parts:
        raise ValueError("Search returned the directory itself as a file")
    return local_root.joinpath(*relative.parts)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    password = os.environ.get("PYUGOS_PASSWORD")
    if password is None:
        password = getpass.getpass("UGOS password: ")

    nas = UgreenNasClient(host=args.host, port=args.port)
    nas.login(username=args.username, password=password)

    remote_root = PurePosixPath(args.remote_path)
    files = nas.search(
        path=args.remote_path,
        recursive=True,
        types=["file"],
    )
    regular_files = [item for item in files if not item.is_directory]
    thumbnail_root = args.destination / "thumbnails"
    thumbnail_count = 0

    for index, item in enumerate(regular_files, start=1):
        remote_file = PurePosixPath(item.path)
        local_path = destination_for(
            remote_root,
            remote_file,
            args.destination,
        )
        local_path.parent.mkdir(parents=True, exist_ok=True)
        with item.open_download() as stream, local_path.open("wb") as output:
            if stream.status_code != 200:
                raise RuntimeError(
                    "Unexpected download status: {}".format(stream.status_code)
                )
            for chunk in stream.iter_bytes():
                output.write(chunk)

        thumbnail_path = destination_for(
            remote_root,
            remote_file,
            thumbnail_root,
        ).with_name(item.name + ".webp")
        try:
            thumbnail = item.get_thumbnail(size=ThumbnailSize.MEDIUM)
            thumbnail.save(thumbnail_path)
            thumbnail_count += 1
            thumbnail_result = str(thumbnail_path)
        except ApiError as exc:
            # UGOS does not generate thumbnails for every file format.
            thumbnail_result = "skipped ({})".format(exc)

        print(
            "[{}/{}] original={} thumbnail={}".format(
                index,
                len(regular_files),
                local_path,
                thumbnail_result,
            )
        )

    print(
        "Downloaded {} file(s) and {} thumbnail(s) to {}".format(
            len(regular_files),
            thumbnail_count,
            args.destination,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
