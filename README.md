# PyUGOS
**NOTE: This is the unofficial tool for UGREEN NAS based on the behaviors as of August 2026**

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

## 対応範囲

- username / password ログイン（OTP なし）
- header および url token mode
- サーバー側 search task によるファイル検索
- サムネイル取得
- `/ugreen/v1/filemgr/downloadFile` からの単一ファイル取得

NAS 上のファイル作成、更新、移動、削除を行うメソッドは実装していません。検索 task の作成には private API の仕様上 POST を使いますが、NAS のファイルシステムは変更しません。

実機確認は DH2300 / UGOS Pro 1.18 系の header token mode で行っています。url token mode は解析済みの通信仕様に基づく実装で、実機では未確認です。

## 開発

```console
python -m pip install -e '.[test]'
pytest
```

認証情報や session token をソース、テスト、ログへ保存しないでください。
