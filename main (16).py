"""yt-dlp の Web フロントエンド (最小構成)。

POST /api/jobs            変換ジョブを作成
GET  /api/jobs/{id}       進捗を取得
GET  /api/jobs/{id}/file  完成したファイルをダウンロード
"""
from __future__ import annotations

import ipaddress
import logging
import os
import re
import shutil
import socket
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

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
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip()             # 例: http://user:pass@host:port

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
    """利用者にそのまま見せてよいエラー。"""


def _update(job_id: str, **kw) -> None:
    with LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(kw)


def _clean_ytdlp_error(e: Exception) -> str:
    msg = re.sub(r"\x1b\[[0-9;]*m", "", str(e))
    msg = re.sub(r"^ERROR:\s*", "", msg).strip()
    return msg[:300] or "ダウンロードに失敗しました。"


def _safe_filename(name: str) -> str:
    table = str.maketrans({"/": "／", "\\": "＼", ":": "：", "*": "＊", "?": "？",
                           '"': "”", "<": "＜", ">": "＞", "|": "｜"})
    name = re.sub(r"[\x00-\x1f\x7f]", "", name).translate(table).strip(" .")
    return name[:150] or "download"


def _build_opts(fmt_key: str, job_dir: Path, progress_hook, pp_hook) -> dict:
    spec = ALL_FORMATS[fmt_key]
    is_audio = fmt_key in AUDIO_FORMATS
    thumb = bool(spec.get("thumb"))
    opts: dict = {
        "format": spec["format"],
        "outtmpl": str(job_dir / "%(id)s.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "restrictfilenames": True,
        "retries": 3,
        "fragment_retries": 3,
        "max_filesize": MAX_FILESIZE_MB * 1024 * 1024,
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [pp_hook],
        "writethumbnail": thumb,
        "postprocessors": [],
    }
    if YTDLP_PROXY:
        opts["proxy"] = YTDLP_PROXY
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


def _run_job(job_id: str) -> None:
    with LOCK:
        job = dict(JOBS[job_id])
    job_dir = DOWNLOAD_DIR / job_id
    _update(job_id, status="running", message="動画の情報を取得しています…")

    def progress_hook(d: dict) -> None:
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            done = d.get("downloaded_bytes") or 0
            if total:
                _update(job_id, progress=round(done / total * 100, 1),
                        message="ダウンロード中…")
        elif d.get("status") == "finished":
            _update(job_id, message="変換中…")

    def pp_hook(d: dict) -> None:
        if d.get("status") == "started":
            _update(job_id, message="変換中…")

    try:
        job_dir.mkdir(parents=True, exist_ok=True)
        opts = _build_opts(job["format"], job_dir, progress_hook, pp_hook)
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

        files = [p for p in job_dir.iterdir() if p.suffix.lower() in MEDIA_EXTS]
        if not files:
            raise UserError("変換後のファイルが見つかりませんでした（ファイルサイズ上限を超えた可能性があります）。")
        out = max(files, key=lambda p: p.stat().st_size)
        base = job["custom_name"] or title
        _update(job_id, status="done", progress=100.0, message="完了しました。",
                path=str(out), filename=_safe_filename(base) + out.suffix.lower(),
                finished_at=time.time())
    except UserError as e:
        _update(job_id, status="error", message=str(e), finished_at=time.time())
        shutil.rmtree(job_dir, ignore_errors=True)
    except yt_dlp.utils.DownloadError as e:
        _update(job_id, status="error", message=_clean_ytdlp_error(e), finished_at=time.time())
        shutil.rmtree(job_dir, ignore_errors=True)
    except Exception:  # noqa: BLE001
        log.exception("job %s failed", job_id)
        _update(job_id, status="error", message="内部エラーが発生しました。", finished_at=time.time())
        shutil.rmtree(job_dir, ignore_errors=True)


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
    yield


app = FastAPI(title="ytdl-web", lifespan=lifespan)


@app.get("/api/config")
def get_config():
    return {
        "formats": [{"key": k, "label": v["label"]} for k, v in ALL_FORMATS.items()],
        "ttl_minutes": FILE_TTL // 60,
        "max_duration_minutes": MAX_DURATION // 60,
        "ytdlp_version": yt_dlp.version.__version__,
    }


@app.post("/api/jobs", status_code=201)
def create_job(req: JobRequest, request: Request):
    url = req.url.strip()
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
            "title": None, "filename": None, "path": None, "finished_at": None,
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
        return {k: job[k] for k in ("id", "status", "progress", "message", "title", "filename")} \
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


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(BASE_DIR / "index.html")


@app.get("/style.css", include_in_schema=False)
def style():
    return FileResponse(BASE_DIR / "style.css")


@app.get("/app.js", include_in_schema=False)
def script():
    return FileResponse(BASE_DIR / "app.js")
