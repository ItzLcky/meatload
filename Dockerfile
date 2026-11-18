# syntax=docker/dockerfile:1

# 3.13 dropped `audioop` from the stdlib, but discord.py's voice/volume path
# still works: on 3.13+ it pulls in `audioop-lts` as a dependency. The tag
# floats on purpose: CI's weekly rebuild then picks up new 3.13 patch releases
# and base-image security fixes. Stay on a stable Python: pre-release tags
# like 3.16-rc scan cleaner but are alphas, and sqlcipher3-binary has no wheels
# for them.
FROM python:3.13-slim-trixie

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
# ca-certificates -> HTTPS to YouTube et al.
# The upgrade pulls in Debian security fixes the base image hasn't caught up
# with yet; without it the image ships whatever openssl/perl the tag was cut on.
RUN apt-get update \
 && apt-get upgrade -y \
 && apt-get install -y --no-install-recommends \
      ffmpeg \
      libopus0 \
      ca-certificates \
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
