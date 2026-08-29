# PyUGOS

[English](README.en.md) | 日本語

**NOTE: This is an unofficial tool for UGREEN NAS, based on behavior observed as of August 2026.**

PyUGOS は、UGOS Pro NAS の private API に読み取り専用でアクセスするための、小さな Python クライアントです。Python 3.9 以降をサポートします。

UGREEN 公式の公開 API を利用したライブラリではありません。UGOS の更新により通信仕様が変わる可能性があります。

## インストール

```console
python -m pip install -e .
```

## 使い方

```python
from pathlib import Path

from pyugos import ThumbnailSize, UgreenNasClient

nas = UgreenNasClient(host="192.168.1.100", port=9999)
nas.login(username="hoge", password="fuga")

files = nas.search(
    path="/home/hoge/Photos",
    recursive=True,
    types=["image", "video"],
)

for file in files:
    print(file.name, file.path, file.size, file.mtime)

    thumbnail = file.get_thumbnail(size=ThumbnailSize.SMALL)
    thumbnail.save(Path("thumbnails") / (file.name + ".webp"))

    # destination はローカル側の保存先ディレクトリです。
    file.download(destination="originals")
```

`get_thumbnail()` の `size` には `ThumbnailSize.SMALL`（`size_type=2`）、`MEDIUM`（`1`）、`LARGE`（`3`）を指定できます。実際のピクセル寸法は画像とUGOSのバージョンに依存します。戻り値は `UgreenBinary` で、`bytes(thumbnail)` で内容を取得でき、`thumbnail.content_type` でレスポンスの Content-Type を確認できます。`download()` は保存先を省略すると `bytes`、指定すると保存した `Path` を返します。

### Range付きストリーミングダウンロード

大きな動画などは `open_download()` で原本全体をメモリへ載せずに取得できます。HTTPレスポンスを確実に閉じるため、context managerとして使用してください。

```python
with file.open_download(range_header="bytes=0-1048575") as stream:
    print(stream.status_code)     # 200 / 206 / 416
    print(stream.content_type)
    print(stream.content_length)
    print(stream.content_range)
    print(stream.accept_ranges)

    for chunk in stream.iter_bytes(chunk_size=1024 * 1024):
        process(chunk)
```

Rangeは `bytes=0-1023`、`bytes=1024-`、`bytes=-1024` のような単一範囲だけを受け付けます。複数Rangeはリクエスト前に拒否されます。`download(destination=...)` もローカルファイルへストリーミング保存します。保存先を省略して `bytes` を受け取る場合のみ、従来どおり原本全体をメモリへ保持します。

## 対応範囲

- username / password ログイン（OTP なし）
- header および url token mode
- サーバー側 search task によるファイル検索
- サムネイル取得
- Range対応のストリーミングダウンロード
- `/ugreen/v1/filemgr/downloadFile` からの単一ファイル取得

NAS 上のファイル作成、更新、移動、削除を行うメソッドは実装していません。検索 task の作成には private API の仕様上 POST を使いますが、NAS のファイルシステムは変更しません。

実機確認は DH2300 / UGOS Pro 1.18 系の header token mode で行っています。`downloadFile` の通常取得（200）、単一Range（206）、範囲外（416）も実機で確認済みです。url token mode は解析済みの通信仕様に基づく実装で、実機では未確認です。

## 開発

```console
python -m pip install -e '.[test]'
pytest
```

認証情報や session token をソース、テスト、ログへ保存しないでください。
