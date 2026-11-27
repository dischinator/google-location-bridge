# Pitfall: "python:3.13-slim" is a floating tag and currently points to Debian 13 (Trixie).
# A rebuild can silently pull a newer Debian base. If `playwright install --with-deps`
# fails after a rebuild (Playwright not supporting the Debian release yet), pin the base
# explicitly, e.g. python:3.13-slim-bookworm (Debian 12, known to work with Playwright).
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    TZ=Europe/Berlin

# Xvfb = virtual display, so Chromium can run "with a window" as before
# libatomic1 is required by Playwright's Node driver (missing in the slim image)
RUN apt-get update \
 && apt-get install -y --no-install-recommends xvfb tzdata libatomic1 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && playwright install --with-deps chromium \
 && rm -rf /var/lib/apt/lists/*

COPY location_bridge.py migrate_config.py entrypoint.sh ./
# Set permissions explicitly: COPY keeps the host's file modes (e.g. 600),
# which the unprivileged user could not read
RUN chmod 755 entrypoint.sh && chmod 644 location_bridge.py migrate_config.py

# Run as unprivileged user (UID 1000). The data folder on the host must belong to it:
#   chown -R 1000:1000 <data folder>
RUN useradd -u 1000 -M -d /tmp -s /usr/sbin/nologin app
USER app

# config.toml, curl.txt and the browser folder "profile" live in the volume
WORKDIR /data
VOLUME /data

ENTRYPOINT ["/app/entrypoint.sh"]
