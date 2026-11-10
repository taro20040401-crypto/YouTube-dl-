"""yt-dlp の Web フロントエンド (最小構成)。

POST /api/jobs            変換ジョブを作成
GET  /api/jobs/{id}       進捗を取得
GET  /api/jobs/{id}/file  完成したファイルをダウンロード
"""
from __future__ import annotations

import importlib.metadata as importlib_metadata
import ipaddress
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urljoin, urlparse

import yt_dlp
import yt_dlp.version
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

log = logging.getLogger("ytdl-web")

# ---------------------------------------------------------------- 設定 (環境変数)
BASE_DIR = Path(__file__).resolve().parent
DOWNLOAD_DIR = Path(os.getenv("DOWNLOAD_DIR", BASE_DIR / "downloads"))
FILE_TTL = int(os.getenv("FILE_TTL_SECONDS", "1800"))          # 完成後ファイルを保持する秒数
MAX_CONCURRENT = int(os.getenv("MAX_CONCURRENT_JOBS", "2"))    # 同時に変換する数
MAX_JOBS_PER_IP = int(os.getenv("MAX_JOBS_PER_IP", "2"))       # 1 IP あたりの同時ジョブ数
MAX_DURATION = int(os.getenv("MAX_DURATION_SECONDS", "3600"))  # 動画の最大長
MAX_FILESIZE_MB = int(os.getenv("MAX_FILESIZE_MB", "2048"))    # 最大ファイルサイズ
ALLOW_PRIVATE_HOSTS = os.getenv("ALLOW_PRIVATE_HOSTS", "0") == "1"
TRUST_PROXY = os.getenv("TRUST_PROXY", "0") == "1"             # リバースプロキシ配下なら 1
AUTO_UPDATE = os.getenv("YTDLP_AUTO_UPDATE", "1") == "1"
UPDATE_INTERVAL = int(os.getenv("YTDLP_UPDATE_INTERVAL_HOURS", "6")) * 3600
PROXY_COOLDOWN = int(os.getenv("PROXY_COOLDOWN_SECONDS", "600"))  # 失敗した回線を休ませる秒数
MAX_ATTEMPTS = int(os.getenv("MAX_ATTEMPTS") or os.getenv("MAX_PROXY_ATTEMPTS") or "8")  # 1ジョブで試す組み合わせの最大数
# YouTube で順に試すクライアント（YouTube への名乗り方）。default は yt-dlp の標準設定
YT_CLIENTS = [c.strip() for c in os.getenv("YTDLP_CLIENTS", "default,android_vr,tv,mweb,ios").split(",") if c.strip()] or ["default"]
EMBED_THUMBNAIL = os.getenv("EMBED_THUMBNAIL", "0") == "1"       # 音声にジャケット画像を埋め込む（CPU を使う）


def _load_proxies() -> list[str | None]:
    """YTDLP_PROXIES（カンマ/空白/改行区切り）と YTDLP_PROXY から回線一覧を作る。
    例: http://1.2.3.4:80,socks5://5.6.7.8:1080,direct   ("direct" はプロキシなし)"""
    raw = os.getenv("YTDLP_PROXIES", "") + "," + os.getenv("YTDLP_PROXY", "")
    out: list[str | None] = []
    for item in re.split(r"[,\s]+", raw):
        item = item.strip()
        if not item:
            continue
        item = None if item.lower() == "direct" else item
        if item not in out:
            out.append(item)
    return out or [None]


PROXIES = _load_proxies()

# 自分自身への定期アクセス（Render 無料プランのスリープ対策。既定はオフ）
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "0") == "1"
KEEP_ALIVE_INTERVAL = int(os.getenv("KEEP_ALIVE_INTERVAL_SECONDS", "600"))
KEEP_ALIVE_URL = (os.getenv("KEEP_ALIVE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")

# ---------------------------------------------------------------- フォーマット定義
AUDIO_FORMATS = {
    "m4a":  {"label": "音声 (M4A)",  "codec": "m4a",  "ext": "m4a",  "thumb": True,
             "format": "bestaudio[ext=m4a]/bestaudio/best"},
    "mp3":  {"label": "音声 (MP3)",  "codec": "mp3",  "ext": "mp3",  "thumb": True,
             "format": "bestaudio/best"},
    "opus": {"label": "音声 (Opus)", "codec": "opus", "ext": "opus", "thumb": False,
             "format": "bestaudio[acodec=opus]/bestaudio/best"},
    "wav":  {"label": "音声 (WAV)",  "codec": "wav",  "ext": "wav",  "thumb": False,
             "format": "bestaudio/best"},
}


def _mp4(height: int | None) -> str:
    h = f"[height<={height}]" if height else ""
    return (f"bestvideo{h}[ext=mp4]+bestaudio[ext=m4a]/"
            f"best{h}[ext=mp4]/best{h}")


VIDEO_FORMATS = {
    "mp4":      {"label": "動画 (MP4・最高画質)", "format": _mp4(None), "container": "mp4"},
    "mp4-1080": {"label": "動画 (MP4・1080p)",    "format": _mp4(1080), "container": "mp4"},
    "mp4-720":  {"label": "動画 (MP4・720p)",     "format": _mp4(720),  "container": "mp4"},
    "mp4-480":  {"label": "動画 (MP4・480p)",     "format": _mp4(480),  "container": "mp4"},
    "mp4-360":  {"label": "動画 (MP4・360p)",     "format": _mp4(360),  "container": "mp4"},
    "webm":     {"label": "動画 (WebM・4K対応)",
                 "format": "bestvideo[ext=webm]+bestaudio[ext=webm]/bestvideo+bestaudio/best",
                 "container": "webm"},
}

ALL_FORMATS = {**AUDIO_FORMATS, **VIDEO_FORMATS}
MEDIA_EXTS = {".mp3", ".m4a", ".opus", ".wav", ".mp4", ".webm", ".mkv"}

# ---------------------------------------------------------------- ジョブ管理
JOBS: dict[str, dict] = {}
LOCK = threading.Lock()
EXECUTOR = ThreadPoolExecutor(max_workers=MAX_CONCURRENT)


class UserError(Exception):
    """利用者にそのまま見せてよいエラー。detail には元の英語エラーを入れる。"""

    def __init__(self, message: str, detail: str | None = None):
        super().__init__(message)
        self.detail = detail


def _update(job_id: str, **kw) -> None:
    with LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(kw)


def _clean_ytdlp_error(e: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    msg = re.sub(r"^ERROR:\s*", "", msg).strip()
    return msg[:300] or "ダウンロードに失敗しました。"


# yt-dlp の英語エラーを、短い日本語に言い換える（上から順に最初に一致したものを使う）
FRIENDLY_ERRORS: list[tuple[str, str]] = [
    (r"not a bot|confirm you.re not a bot",
     "YouTube に自動アクセスとして拒否されました。回線（プロキシ）を替えるか、時間をおいてください。"),
    (r"confirm your age|age[- ]restricted|inappropriate for some users",
     "年齢制限のある動画です。ログインなしでは保存できません。"),
    (r"Private video|This video is private",
     "非公開の動画です。"),
    (r"members-only|Join this channel|members only",
     "メンバー限定の動画です。"),
    (r"in your country|not made this video available|geo.?restrict",
     "この地域では視聴できない動画です。"),
    (r"copyright",
     "著作権の理由でブロックされている動画です。"),
    (r"Video unavailable|has been removed|account associated with this video has been terminated|no longer available",
     "動画が削除されたか、公開されていません。"),
    (r"live event will begin|Premieres in|This live event",
     "まだ始まっていない配信・プレミア公開です。"),
    (r"Unsupported URL",
     "このURLには対応していません。動画ページのURLを入力してください。"),
    (r"CERTIFICATE_VERIFY_FAILED|self[- ]signed|certificate",
     "回線（プロキシ）の証明書が不正です。この回線は使えません。"),
    (r"timed out|timeout|Read timed",
     "接続がタイムアウトしました。回線が混んでいるか、プロキシが止まっています。"),
    (r"Connection refused|Connection reset|Tunnel connection failed|Proxy|Remote end closed|Network is unreachable|Temporary failure|Name or service not known|getaddrinfo",
     "接続できませんでした。プロキシが止まっている可能性があります。"),
    (r"HTTP Error 429|Too Many Requests",
     "アクセスが多すぎて制限されました。しばらく待ってから試してください。"),
    (r"HTTP Error 403",
     "アクセスを拒否されました（403）。回線を替えて試してください。"),
    (r"HTTP Error 404",
     "ページが見つかりません。URLを確認してください。"),
    (r"Failed to extract any player response|player response",
     "YouTube から動画情報を取得できませんでした。このサーバーの接続が制限されているか、yt-dlp が古い可能性があります。"),
    (r"larger than max-filesize|File is larger",
     f"ファイルが大きすぎます（上限 {MAX_FILESIZE_MB}MB）。"),
    (r"Requested format is not available|No video formats found",
     "選んだ形式では保存できません。別の形式を試してください。"),
    (r"No space left",
     "サーバーの空き容量が足りません。しばらくしてから試してください。"),
    (r"ffmpeg|Postprocessing|Conversion failed",
     "変換に失敗しました。別の形式を試してください。"),
]


def _pick_friendly(texts: list[str]) -> str:
    """複数の試行のエラー・警告の中から、表の上のもの（原因として確からしいもの）を選んで日本語にする。"""
    for pattern, message in FRIENDLY_ERRORS:
        if any(re.search(pattern, t, re.I) for t in texts):
            return message
    return "ダウンロードに失敗しました。URLと形式を確認して、もう一度試してください。"


def _friendly_error(raw: str) -> str:
    return _pick_friendly([raw])


ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class _CollectLog:
    """yt-dlp の警告とエラーを集める（原因の診断に使う）。"""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def debug(self, msg: str) -> None:
        pass

    def info(self, msg: str) -> None:
        pass

    def warning(self, msg: str) -> None:
        self.lines.append(ANSI_RE.sub("", str(msg)).strip())

    def error(self, msg: str) -> None:
        self.lines.append(ANSI_RE.sub("", str(msg)).strip())


def _diag_header() -> str:
    return f"yt-dlp {yt_dlp.version.__version__} / deno: {'あり' if shutil.which('deno') else 'なし'} / ffmpeg: {'あり' if shutil.which('ffmpeg') else 'なし'}"


def _safe_filename(name: str) -> str:
    table = str.maketrans({"/": "／", "\\": "＼", ":": "：", "*": "＊", "?": "？",
                           '"': "”", "<": "＜", ">": "＞", "|": "｜"})
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).translate(table).strip(" .")
    return name[:150] or "download"


def _build_opts(fmt_key: str, job_dir: Path, progress_hook, pp_hook, proxy: str | None,
                client: str | None = None, log=None) -> dict:
    spec = ALL_FORMATS[fmt_key]
    is_audio = fmt_key in AUDIO_FORMATS
    thumb = bool(spec.get("thumb")) and EMBED_THUMBNAIL
    opts: dict = {
        "format": spec["format"],
        "outtmpl": str(job_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": False,
        "noprogress": True,
        "restrictfilenames": True,
        "socket_timeout": 15,
        "extractor_retries": 1,
        "retries": 3,
        "fragment_retries": 3,
        "max_filesize": MAX_FILESIZE_MB * 1024 * 1024,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "writethumbnail": thumb,
        "postprocessors": [],
    }
    if log is not None:
        opts["logger"] = log
    if proxy:
        opts["proxy"] = proxy
    if client and client != "default":
        opts["extractor_args"] = {"youtube": {"player_client": [client]}}
    pps = opts["postprocessors"]
    if is_audio:
        pps.append({"key": "FFmpegExtractAudio",
                    "preferredcodec": spec["codec"], "preferredquality": "192"})
    else:
        opts["merge_output_format"] = spec["container"]
    if thumb:
        pps.append({"key": "FFmpegThumbnailsConvertor", "format": "jpg", "when": "before_dl"})
    pps.append({"key": "FFmpegMetadata", "add_metadata": True})
    if thumb:
        pps.append({"key": "EmbedThumbnail"})
    return opts


# 動画自体の問題（回線を替えても直らない）と判断するメッセージ
# 動画自体の問題（接続方法を替えても直らない）と判断するメッセージ
FATAL_HINTS = (
    "Private video", "This video is private", "Unsupported URL",
    "has been terminated", "members-only", "Join this channel",
)
# クライアントによって出ることもあるので、2通り以上試してから諦めるメッセージ
SOFT_FATAL_HINTS = ("Video unavailable", "has been removed")

PLOCK = threading.Lock()
Route = tuple  # (プロキシ or None, クライアント名)
ROUTE_FAIL_UNTIL: dict[Route, float] = {}
ROUTE_STATE: dict[str, Route | None] = {"last_good": None}


def _is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host == "youtu.be" or host.endswith("youtube.com") or host.endswith("youtube-nocookie.com")


def _ordered_routes(url: str) -> list[Route]:
    """試す (プロキシ, クライアント) の順番を決める。
    直近で成功した組み合わせを先頭に、休止中のものは最後に回す。YouTube 以外ではクライアントは替えない。"""
    clients = YT_CLIENTS if _is_youtube(url) else ["default"]
    routes: list[Route] = [(p, c) for p in PROXIES for c in clients]
    now = time.time()
    with PLOCK:
        good = [r for r in routes if ROUTE_FAIL_UNTIL.get(r, 0) <= now]
        bad = [r for r in routes if ROUTE_FAIL_UNTIL.get(r, 0) > now]
        last = ROUTE_STATE["last_good"]
        if last in good:
            good.remove(last)
            good.insert(0, last)
    return (good + bad)[:MAX_ATTEMPTS]


def _mark_bad(route: Route) -> None:
    with PLOCK:
        ROUTE_FAIL_UNTIL[route] = time.time() + PROXY_COOLDOWN
        if ROUTE_STATE["last_good"] == route:
            ROUTE_STATE["last_good"] = None


def _mark_good(route: Route) -> None:
    with PLOCK:
        ROUTE_FAIL_UNTIL.pop(route, None)
        ROUTE_STATE["last_good"] = route


def _attempt(job_id: str, job: dict, job_dir: Path, route: Route) -> tuple[str, Path]:
    """1つの組み合わせでダウンロードを試す。失敗時は例外。"""
    proxy, client = route

    def progress_hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            if total:
                _update(job_id, progress=round(done / total * 100, 1), message="ダウンロード中…")
        elif d.get("status") == "finished":
            _update(job_id, message="変換中…")

    def pp_hook(d: dict) -> None:
        if d.get("status") == "started":
            _update(job_id, message="変換中…")

    ylog = _CollectLog()
    opts = _build_opts(job["format"], job_dir, progress_hook, pp_hook, proxy, client, ylog)
    try:
        title = _download(job_id, job, opts)
    except yt_dlp.utils.YoutubeDLError as e:
        e.ytlog = ylog.lines  # type: ignore[attr-defined]
        raise

    files = [p for p in job_dir.iterdir() if p.suffix.lower() in MEDIA_EXTS]
    if not files:
        raise UserError("変換後のファイルが見つかりませんでした（ファイルサイズ上限を超えた可能性があります）。")
    return title, max(files, key=lambda p: p.stat().st_size)


def _download(job_id: str, job: dict, opts: dict) -> str:
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(job["url"], download=False, process=False)
        if not info:
            raise UserError("動画の情報を取得できませんでした。")
        if info.get("_type") in ("playlist", "multi_video"):
            raise UserError("プレイリストには対応していません。動画の URL を指定してください。")
        if info.get("is_live"):
            raise UserError("ライブ配信には対応していません。")
        duration = info.get("duration")
        if duration and duration > MAX_DURATION:
            raise UserError(f"動画が長すぎます（上限 {MAX_DURATION // 60} 分）。")
        title = info.get("title") or "download"
        _update(job_id, title=title)
        ydl.process_ie_result(info, download=True)
    return title


def _run_job(job_id: str) -> None:
    with LOCK:
        job = dict(JOBS[job_id])
    job_dir = DOWNLOAD_DIR / job_id
    _update(job_id, status="running", message="動画の情報を取得しています…")

    try:
        routes = _ordered_routes(job["url"])
        last_err = "ダウンロードに失敗しました。"
        texts: list[str] = []   # 全試行のエラーと警告（原因の判定用）
        notes: list[str] = []   # 画面の「詳細」に出す記録

        def make_detail() -> str:
            return (_diag_header() + "\n" + "\n".join(notes))[:3000]
        for i, route in enumerate(routes, 1):
            shutil.rmtree(job_dir, ignore_errors=True)
            job_dir.mkdir(parents=True, exist_ok=True)
            msg = "動画の情報を取得しています…"
            if len(routes) > 1:
                msg += f"（{i}/{len(routes)}）"
            _update(job_id, progress=0.0, message=msg)
            try:
                title, out = _attempt(job_id, job, job_dir, route)
            except yt_dlp.utils.YoutubeDLError as e:
                last_err = _clean_ytdlp_error(e)
                warns = [w[:200] for w in getattr(e, "ytlog", []) if w][:4]
                texts.append(last_err)
                texts.extend(warns)
                label = f"({i}) {route[1]}/{'プロキシ' if route[0] else '直接'}"
                notes.append(f"{label}: {last_err[:250]}" + ("".join(f"\n    ! {w}" for w in warns)))
                fatal = any(h in last_err for h in FATAL_HINTS) or (
                    i >= 2 and any(h in last_err for h in SOFT_FATAL_HINTS))
                if fatal:
                    raise UserError(_pick_friendly(texts), detail=make_detail())
                log.warning("attempt %d (client=%s, proxy=%s) failed: %s",
                            i, route[1], bool(route[0]), last_err[:200])
                _mark_bad(route)
                continue
            _mark_good(route)
            log.info("ok via client=%s proxy=%s", route[1], bool(route[0]))
            base = job["custom_name"] or title
            _update(job_id, status="done", progress=100.0, message="完了しました。",
                    path=str(out), filename=_safe_filename(base) + out.suffix.lower(),
                    finished_at=time.time())
            return
        msg = _pick_friendly(texts or [last_err])
        if len(routes) > 1:
            msg += f"（{len(routes)}通りすべて失敗）"
        raise UserError(msg, detail=make_detail())
    except UserError as e:
        _update(job_id, status="error", message=str(e), detail=e.detail, finished_at=time.time())
        shutil.rmtree(job_dir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        log.exception("job %s failed", job_id)
        _update(job_id, status="error", message="内部エラーが発生しました。", finished_at=time.time())
        shutil.rmtree(job_dir, ignore_errors=True)


def _update_loop() -> None:
    """定期的に yt-dlp を更新し、新しい版が入ったら（ジョブがない時に）自分を再起動する。"""
    while True:
        time.sleep(UPDATE_INTERVAL)
        with LOCK:
            if any(j["status"] in ("queued", "running") for j in JOBS.values()):
                continue
        try:
            before = importlib_metadata.version("yt-dlp")
            subprocess.run([sys.executable, "-m", "pip", "install", "-q", "-U", "yt-dlp[default]"],
                           check=True, timeout=300, capture_output=True)
            after = importlib_metadata.version("yt-dlp")
        except Exception as e:  # noqa: BLE001
            log.warning("yt-dlp update check failed: %s", e)
            continue
        if after == before:
            continue
        with LOCK:
            if any(j["status"] in ("queued", "running") for j in JOBS.values()):
                continue  # 次回の周期に持ち越し
        log.info("yt-dlp updated %s -> %s, restarting", before, after)
        os.execv(sys.executable, [sys.executable, "-m", "uvicorn", "main:app",
                                  "--host", "0.0.0.0", "--port", os.getenv("PORT", "8000")])


def _ping_self(url: str) -> bool:
    req = urllib.request.Request(url, headers={"User-Agent": "ytdl-web-keepalive"})
    try:
        with urllib.request.urlopen(req, timeout=15):
            return True
    except Exception as e:  # noqa: BLE001
        log.warning("keep-alive failed: %s", e)
        return False


def _keep_alive_loop() -> None:
    """公開URLの /api/config に定期アクセスして、無料プランのスリープを防ぐ。"""
    url = KEEP_ALIVE_URL + "/api/config"
    while True:
        time.sleep(KEEP_ALIVE_INTERVAL)
        _ping_self(url)


def _cleanup_loop() -> None:
    while True:
        time.sleep(60)
        now = time.time()
        with LOCK:
            for jid, job in list(JOBS.items()):
                fin = job.get("finished_at")
                if not fin:
                    continue
                if now - fin > FILE_TTL and job["status"] == "done":
                    shutil.rmtree(DOWNLOAD_DIR / jid, ignore_errors=True)
                    job.update(status="expired", message="ファイルの保存期限が切れました。", path=None)
                if now - fin > FILE_TTL + 3600:
                    JOBS.pop(jid, None)


# ---------------------------------------------------------------- 入力チェック
def _client_ip(request: Request) -> str:
    if TRUST_PROXY:
        fwd = request.headers.get("x-forwarded-for")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _validate_url(url: str) -> None:
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise HTTPException(400, "http(s) から始まる URL を入力してください。")
    if ALLOW_PRIVATE_HOSTS:
        return
    try:
        addrs = socket.getaddrinfo(p.hostname, None)
    except socket.gaierror:
        raise HTTPException(400, "ホスト名を解決できませんでした。")
    for addr in addrs:
        ip = ipaddress.ip_address(addr[4][0].split("%")[0])
        if not ip.is_global:
            raise HTTPException(400, "このアドレスには接続できません。")


# ---------------------------------------------------------------- URL の正規化
# 短縮URL（リダイレクト先を辿って本来のURLに直す）
SHORT_HOSTS = {
    "bit.ly", "t.co", "tinyurl.com", "goo.gl", "ow.ly", "is.gd", "buff.ly", "lnkd.in",
    "t.ly", "cutt.ly", "rebrand.ly", "shorturl.at", "s.id", "x.gd", "amzn.to",
    "nico.ms", "b23.tv", "vm.tiktok.com", "vt.tiktok.com", "fb.watch", "on.soundcloud.com",
    "spoti.fi", "bitly.com", "tiny.cc", "v.gd", "j.mp",
}
YT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
YT_PATH_PREFIXES = {"shorts", "live", "embed", "v", "e"}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None  # 自動では辿らず、Location を自分で検証しながら辿る


def _normalize_youtube(url: str) -> str | None:
    """YouTube の各種URL（youtu.be / m. / music. / shorts / live / embed 等）を
    https://www.youtube.com/watch?v=ID に統一する。動画でなければ None。"""
    p = urlparse(url)
    host = (p.hostname or "").lower()
    parts = [x for x in p.path.split("/") if x]
    vid = None
    if host == "youtu.be":
        vid = parts[0] if parts else None
    elif host.endswith("youtube.com") or host.endswith("youtube-nocookie.com"):
        if parts and parts[0] == "watch":
            vid = (parse_qs(p.query).get("v") or [None])[0]
        elif len(parts) >= 2 and parts[0] in YT_PATH_PREFIXES:
            vid = parts[1]
    if vid and YT_ID_RE.match(vid):
        return f"https://www.youtube.com/watch?v={vid}"
    return None


def _expand_short_url(url: str) -> str:
    """短縮URLのリダイレクトを最大5回まで辿る。辿る先は毎回アドレスを検証する。"""
    opener = urllib.request.build_opener(_NoRedirect)
    for _ in range(5):
        host = (urlparse(url).hostname or "").lower()
        if host not in SHORT_HOSTS:
            return url
        _validate_url(url)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        try:
            with opener.open(req, timeout=6):
                return url
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location")
            if e.code in (301, 302, 303, 307, 308) and loc:
                url = urljoin(url, loc)
                continue
            return url
        except Exception:  # noqa: BLE001
            return url
    return url


def _prepare_url(raw: str) -> str:
    """入力欄の文字列から動画URLを取り出し、扱いやすい形に整える。"""
    text = raw.strip()
    m = re.search(r"https?://[A-Za-z0-9\-._~:/?#\[\]@!$&'()*+,;=%]+", text)
    if m:
        url = m.group(0)
    else:
        token = text.split()[0] if text.split() else ""
        if not re.match(r"^[\w-]+(\.[\w-]+)+([/?#]|$)", token):
            raise HTTPException(400, "URL を入力してください。")
        url = "https://" + token  # スキーム無し（youtu.be/xxxx など）を補う
    url = url.rstrip(".,)」』）>")
    url = _normalize_youtube(url) or url
    if (urlparse(url).hostname or "").lower() in SHORT_HOSTS:
        url = _expand_short_url(url)
        url = _normalize_youtube(url) or url
    return url


class JobRequest(BaseModel):
    url: str = Field(min_length=1, max_length=2048)
    format: str = "m4a"
    filename: str | None = Field(default=None, max_length=150)


# ---------------------------------------------------------------- アプリ
@asynccontextmanager
async def lifespan(_: FastAPI):
    logging.basicConfig(level=logging.INFO)
    shutil.rmtree(DOWNLOAD_DIR, ignore_errors=True)  # 起動時に残骸を掃除
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=_cleanup_loop, daemon=True).start()
    if AUTO_UPDATE:
        threading.Thread(target=_update_loop, daemon=True).start()
    if KEEP_ALIVE:
        if KEEP_ALIVE_URL:
            threading.Thread(target=_keep_alive_loop, daemon=True).start()
            log.info("keep-alive enabled: %s every %ss", KEEP_ALIVE_URL, KEEP_ALIVE_INTERVAL)
        else:
            log.warning("KEEP_ALIVE=1 ですが URL が不明です（KEEP_ALIVE_URL を設定してください）")
    log.info("proxies configured: %d", len(PROXIES))
    log.info("env: %s", _diag_header())
    yield


app = FastAPI(title="ytdl-web", lifespan=lifespan)


@app.get("/api/config")
def get_config():
    return {
        "formats": [{"key": k, "label": v["label"]} for k, v in ALL_FORMATS.items()],
        "ttl_minutes": FILE_TTL // 60,
        "max_duration_minutes": MAX_DURATION // 60,
        "ytdlp_version": yt_dlp.version.__version__,
        "proxy_count": sum(1 for p in PROXIES if p),
    }


@app.post("/api/jobs", status_code=201)
def create_job(req: JobRequest, request: Request):
    url = _prepare_url(req.url)
    if req.format not in ALL_FORMATS:
        raise HTTPException(400, "未対応のフォーマットです。")
    _validate_url(url)
    ip = _client_ip(request)
    with LOCK:
        active = sum(1 for j in JOBS.values()
                     if j["ip"] == ip and j["status"] in ("queued", "running"))
        if active >= MAX_JOBS_PER_IP:
            raise HTTPException(429, "同時に実行できる変換数の上限です。完了後にお試しください。")
        job_id = uuid.uuid4().hex
        JOBS[job_id] = {
            "id": job_id, "ip": ip, "url": url, "format": req.format,
            "custom_name": (req.filename or "").strip() or None,
            "status": "queued", "progress": 0.0, "message": "順番待ちです…",
            "title": None, "filename": None, "path": None, "finished_at": None, "detail": None,
        }
    EXECUTOR.submit(_run_job, job_id)
    return {"id": job_id}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
        if not job:
            raise HTTPException(404, "ジョブが見つかりません。")
        remaining = None
        if job["status"] == "done" and job["finished_at"]:
            remaining = max(0, int(job["finished_at"] + FILE_TTL - time.time()))
        return {k: job[k] for k in ("id", "status", "progress", "message", "title", "filename", "detail")} \
            | {"expires_in": remaining}


@app.get("/api/jobs/{job_id}/file")
def download(job_id: str):
    with LOCK:
        job = JOBS.get(job_id)
        path = job.get("path") if job else None
        filename = job.get("filename") if job else None
        status = job.get("status") if job else None
    if status != "done" or not path or not Path(path).is_file():
        raise HTTPException(404, "ファイルが存在しないか、保存期限が切れました。")
    return FileResponse(path, filename=filename)


# 画面ファイルを更新したとき、古い版がブラウザに残らないようにする
NO_CACHE = {"Cache-Control": "no-cache"}


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE_DIR / "index.html", headers=NO_CACHE)


@app.get("/style.css", include_in_schema=False)
def style():
    return FileResponse(BASE_DIR / "style.css", headers=NO_CACHE)


@app.get("/app.js", include_in_schema=False)
def script():
    return FileResponse(BASE_DIR / "app.js", headers=NO_CACHE)
