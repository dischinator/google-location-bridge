FROM python:3.12-slim-bookworm

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
RUN chmod +x entrypoint.sh

# config.toml, curl.txt and the browser folder "profile" live in the volume
WORKDIR /data
VOLUME /data

ENTRYPOINT ["/app/entrypoint.sh"]
