#!/usr/bin/env sh
set -e

DATA_DIR="$(dirname "${DATABASE_PATH:-/app/data/bot.db}")"

# The data directory is a bind mount from the host, so its owner is whatever
# uid owns ./data there. If that doesn't match the uid this container runs as,
# every write fails — catch it here with an actionable message instead of
# letting SQLite raise "unable to open database file" 20 lines later.
mkdir -p "$DATA_DIR" 2>/dev/null || true
if ! touch "$DATA_DIR/.write-test" 2>/dev/null; then
    echo "ERROR: $DATA_DIR is not writable by uid $(id -u):$(id -g)." >&2
    echo "" >&2
    echo "On the host, run:  id -u    and    id -g" >&2
    echo "then set PUID and PGID to those values in .env and run:" >&2
    echo "  docker compose up -d" >&2
    echo "" >&2
    echo "Or hand the directory to this container's uid:" >&2
    echo "  sudo chown -R $(id -u):$(id -g) ./data" >&2
    exit 1
fi
rm -f "$DATA_DIR/.write-test"

# YouTube changes its player fairly often and yt-dlp ships fixes within hours.
# With YTDLP_AUTO_UPDATE=true, the newest yt-dlp is pulled on every start, so a
# plain `docker compose restart bot` is usually the entire fix.
#
# It installs into PYTHONUSERBASE, which lives inside the writable data volume.
# That works no matter which uid the container runs as, takes precedence over
# the version baked into the image, and survives restarts. A failure here must
# never stop the bot from booting.
if [ "${YTDLP_AUTO_UPDATE:-false}" = "true" ]; then
    echo "[entrypoint] updating yt-dlp..."
    mkdir -p "${PYTHONUSERBASE:-/app/data/.python}"
    PIP_ARGS="--user --upgrade --no-cache-dir --disable-pip-version-check --no-warn-script-location --quiet"
    # shellcheck disable=SC2086
    if pip install $PIP_ARGS yt-dlp 2>/dev/null \
        || pip install $PIP_ARGS --break-system-packages yt-dlp 2>/dev/null; then
        echo "[entrypoint] yt-dlp is now $(python -c 'import yt_dlp; print(yt_dlp.version.__version__)' 2>/dev/null || echo unknown)"
    else
        echo "[entrypoint] yt-dlp update failed (offline?), continuing with the bundled version" >&2
    fi
fi

exec "$@"
