# syntax=docker/dockerfile:1

# Python 3.12 is deliberate: 3.13 removed `audioop` from the stdlib, which
# discord.py's voice/volume path depends on. Staying on 3.12 avoids that whole
# class of breakage.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# yt-dlp self-updates land here: inside the writable data volume, so the update
# works whatever uid the container runs as and persists across restarts. Python
# puts this ahead of the image's own site-packages.
ENV PYTHONUSERBASE=/app/data/.python

# ffmpeg   -> audio transcoding for voice
# libopus0 -> discord.py loads libopus via ctypes at runtime
# ca-certificates / curl -> HTTPS to YouTube et al.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      libopus0 \
      ca-certificates \
      curl \
      tini \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY docker/entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

COPY bot ./bot

# Run as a non-root user; /app/data is the mounted volume for the SQLite file.
RUN useradd --create-home --uid 10001 botuser \
 && mkdir -p /app/data \
 && chown -R botuser:botuser /app
USER botuser

VOLUME ["/app/data"]

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
CMD ["python", "-m", "bot"]
