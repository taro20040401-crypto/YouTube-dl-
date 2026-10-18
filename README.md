# ytdl-web

[yt-dlp](https://github.com/yt-dlp/yt-dlp) と ffmpeg を使った、シンプルな動画・音声ダウンロード用 Web フロントエンドです。
URL を入力 → 形式を選ぶ → 変換 → ダウンロード、までを行います。

- 音声: M4A / MP3 / Opus / WAV（M4A・MP3 はサムネイルをジャケット画像として埋め込み）
- 動画: MP4（最高画質 / 1080p / 720p / 480p / 360p）、WebM（4K 対応）
- バックエンド: FastAPI（ジョブをスレッドで実行し、進捗をポーリングで取得）
- 変換済みファイルは一定時間（既定 30 分）で自動削除
- 同時変換数・1 IP あたりのジョブ数・動画の長さ・ファイルサイズに上限あり

## 構成

```
main.py            API サーバー（ジョブ管理・yt-dlp 呼び出し・自動削除）
index.html / style.css / app.js   フロントエンド（ビルド不要）
Dockerfile         ffmpeg と deno を含むイメージ
entrypoint.sh      起動時に yt-dlp を更新してサーバーを起動
```

## 起動方法

### Docker（推奨）

```bash
docker build -t ytdl-web . && docker run -p 8000:8000 ytdl-web
# http://localhost:8000
```

### ローカル（Python 3.10+）

ffmpeg と [deno](https://deno.com/) をインストールしておいてください（YouTube の取得に必要です）。

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
uvicorn main:app --reload
```

## 設定（環境変数）

| 変数 | 既定値 | 説明 |
|---|---|---|
| `FILE_TTL_SECONDS` | 1800 | 変換後にファイルを保持する秒数 |
| `MAX_CONCURRENT_JOBS` | 2 | 同時に変換する数 |
| `MAX_JOBS_PER_IP` | 2 | 1 IP あたりの同時ジョブ数 |
| `MAX_DURATION_SECONDS` | 3600 | 変換できる動画の最大長 |
| `MAX_FILESIZE_MB` | 2048 | ファイルサイズの上限 |
| `TRUST_PROXY` | 0 | リバースプロキシ配下なら 1（`X-Forwarded-For` を信用） |
| `ALLOW_PRIVATE_HOSTS` | 0 | 1 にするとプライベート IP への接続を許可（非推奨） |
| `YTDLP_AUTO_UPDATE` | 1 | Docker 起動時に yt-dlp を最新化 |

## 公開して運用する場合の注意

- **YouTube のアクセス規制**: データセンターの IP からの大量アクセスはブロックされやすく、
  失敗することがあります。プロキシ・クッキー等での回避は本リポジトリには含めていません。
- **yt-dlp は頻繁に更新が必要**です。定期的にコンテナを再起動する（`YTDLP_AUTO_UPDATE=1`）か、
  イメージを再ビルドしてください。
- 認証がないため、公開する場合は Basic 認証・リバースプロキシでのレート制限などを推奨します。
- ストレージ（`/srv/downloads`）と CPU を消費します。上限値は環境に合わせて調整してください。

## 法的な注意

利用者・運営者は、各サービスの利用規約と、居住国の著作権法を遵守する責任を負います。
権利者の許諾がないコンテンツの保存は、規約違反や違法になる場合があります
（日本では、違法にアップロードされたと知りながらの録音・録画が私的使用でも違法になり得ます）。
自分が権利を持つ、または保存が許可されているコンテンツにのみ使用してください。
本ソフトウェアは現状有姿で提供され、利用により生じた問題について作者は責任を負いません。

## API

| メソッド | パス | 内容 |
|---|---|---|
| POST | `/api/jobs` | `{"url": "...", "format": "m4a", "filename": null}` → `{"id": "..."}` |
| GET | `/api/jobs/{id}` | `status`: queued / running / done / error / expired、`progress`、`message` |
| GET | `/api/jobs/{id}/file` | 完成したファイル |
| GET | `/api/config` | フォーマット一覧・上限値・yt-dlp バージョン |

## ライセンス

お好みのライセンスを `LICENSE` に追加してください（例: MIT）。
