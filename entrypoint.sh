#!/bin/sh
set -e
# サイト側の仕様変更に追従するため、起動時に yt-dlp を最新化する
if [ "${YTDLP_AUTO_UPDATE:-1}" = "1" ]; then
  pip install -q -U "yt-dlp[default]" || echo "yt-dlp の更新に失敗しました。既存版で起動します。"
fi
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8000}"
