FROM python:3.12-slim

# ffmpeg: 変換 / deno: yt-dlp が YouTube の JS を解くために必要
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

RUN useradd -m app && python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1

WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && chown -R app:app /opt/venv

COPY main.py index.html style.css app.js entrypoint.sh ./
RUN mkdir -p /srv/downloads && chown -R app:app /srv && chmod +x entrypoint.sh

USER app
EXPOSE 8000
ENTRYPOINT ["./entrypoint.sh"]
