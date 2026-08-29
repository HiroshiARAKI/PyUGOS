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

from pyugos import ThumbnailSize, UgreenNasClient, VideoQuality

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

### メディア情報

画像、音声、動画についてUGOSの詳細パネルと同じメディア情報を取得できます。

```python
info = file.get_media_info()
print(info.width, info.height)
print(info.duration, info.frame_rate)
print(info.video_format, info.hdr)
```

戻り値は `UgreenMediaInfo` です。取得できない項目は `None` になり、UGOSのファームウェア固有の追加項目を含む元データは `info.raw` で参照できます。既知項目として、解像度、duration、bit rate、channel、frame rate、映像形式、HDR、撮影機器・日時、software、color space、ISO、aperture、shutter speed、focal lengthを公開します。値の単位と表記はUGOSの応答を維持します。ディレクトリには使用できません。

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

Rangeは `bytes=0-1023`、`bytes=1024-`、`bytes=-1024` のような単一範囲だけを受け付けます。複数Rangeはリクエスト前に拒否されます。`download(destination=...)` も一時ファイルへストリーミングし、完了後に置き換えるため、転送失敗時に既存ファイルを破損しません。保存先を省略して `bytes` を受け取る場合のみ、従来どおり原本全体をメモリへ保持します。

### 1080p／720p HLS再生

DH2300 / UGOS Pro 1.18.2.0100のブラウザ再生用変換はMP4 Rangeではなく、画質別のHLS（MPEG-TS）です。`open_video_playback()`はUGOSの再生セッションを開始し、heartbeatを維持しながら、トークンを含まないマニフェストとopaqueなセグメントIDを公開します。

```python
from pyugos import VideoQuality

qualities = file.get_video_qualities()

with file.open_video_playback(
    VideoQuality.P1080,  # VideoQuality.P720 も指定可能
    preparation_timeout=60,
) as playback:
    print(playback.protocol)          # hls
    print(playback.requested_quality)
    print(playback.actual_quality)
    print(playback.is_transcoded)

    # Hagukumi等のプロキシ上のURLへ書き換えます。省略時は
    # segments/<opaque-id> という相対URLになります。
    manifest = playback.open_manifest(
        lambda segment_id: "/video/segments/{}".format(segment_id)
    )
    serve(bytes(manifest), content_type=manifest.content_type)

    segment_id = playback.segment_ids[0]
    with playback.open_segment(segment_id) as segment:
        for chunk in segment.iter_bytes():
            serve_chunk(chunk)
```

UGOSのURL、API token、transcode task IDは公開オブジェクトの`repr()`や書き換え後のマニフェストへ含まれません。`playback.close()`は開いているセグメント、WebSocket heartbeat、UGOS再生セッションを閉じます。必ずcontext managerで使用してください。

原本は同じ画質enumを使って既存Rangeストリームを開けます。

```python
with file.open_video(
    VideoQuality.ORIGINAL,
    range_header="bytes=0-",
) as stream:
    for chunk in stream.iter_bytes():
        serve_chunk(chunk)
```

`P1080`と`P720`はHLSなので`range_header`を受け付けません。要求画質がUGOSの`transcodeable`一覧にない場合は、原本へフォールバックせず`VideoQualityUnavailableError`を送出します。

## 対応範囲

- username / password ログイン（OTP なし）
- header および url token mode
- サーバー側 search task によるファイル検索
- サムネイル取得
- 画像・音声・動画のメディア情報取得
- Range対応のストリーミングダウンロード
- 1080p／720pのHLSブラウザ再生用ストリーム
- 利用可能動画画質の取得
- `/ugreen/v1/filemgr/downloadFile` からの単一ファイル取得

NAS 上のファイル作成、更新、移動、削除を行うメソッドは実装していません。検索 task の作成には private API の仕様上 POST を使いますが、NAS のファイルシステムは変更しません。

実機確認は DH2300 / UGOS Pro 1.18 系の header token mode で行っています。`downloadFile` の通常取得（200）、単一Range（206）、範囲外（416）も実機で確認済みです。1080p／720p HLSの通信仕様はDH2300 / UGOS Pro 1.18.2.0100のHARとWebプレイヤー実装に基づきます。メディア情報取得の通信仕様は同バージョンのHARとWeb UI実装に基づきます。url token mode は解析済みの通信仕様に基づく実装で、実機では未確認です。

## 開発

```console
python -m pip install -e '.[test]'
pytest
```

認証情報や session token をソース、テスト、ログへ保存しないでください。
